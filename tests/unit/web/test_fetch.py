from __future__ import annotations

import unittest

import httpx

from truecoder.web.fetch import WebFetcher, WebFetchError
from truecoder.web.policy import UrlPolicyError

PUBLIC = ("93.184.216.34",)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )


def _html(body: str, content_type: str = "text/html; charset=utf-8"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    return handler


class RecordingResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int):
        self.calls.append((host, port))
        return self.mapping.get(host, PUBLIC)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def _fetch(self, url: str, handler, **kwargs):
        resolver = kwargs.pop("resolver", None) or RecordingResolver()
        fetcher = WebFetcher(_client(handler), resolver=resolver, **kwargs)
        try:
            return await fetcher.fetch(url)
        finally:
            await fetcher.aclose()

    async def test_html_is_returned_as_readable_text(self):
        page = await self._fetch(
            "https://example.com/docs",
            _html("<html><head><title>Docs</title></head><body><p>Hello</p></body></html>"),
        )

        self.assertEqual(page.title, "Docs")
        self.assertEqual(page.text, "Hello")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.content_type, "text/html")
        self.assertEqual(page.final_url, "https://example.com/docs")

    async def test_plain_text_is_returned_verbatim(self):
        page = await self._fetch(
            "https://example.com/robots.txt",
            _html("User-agent: *\nDisallow:", content_type="text/plain"),
        )

        self.assertEqual(page.text, "User-agent: *\nDisallow:")
        self.assertEqual(page.title, "")

    async def test_json_is_supported(self):
        page = await self._fetch(
            "https://example.com/api",
            _html('{"ok": true}', content_type="application/json"),
        )

        self.assertEqual(page.text, '{"ok": true}')

    async def test_binary_content_is_refused(self):
        with self.assertRaises(WebFetchError) as caught:
            await self._fetch(
                "https://example.com/image.png",
                _html("binary", content_type="image/png"),
            )

        self.assertEqual(caught.exception.code, "unsupported_content_type")

    async def test_an_http_error_status_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="missing")

        with self.assertRaises(WebFetchError) as caught:
            await self._fetch("https://example.com/gone", handler)

        self.assertEqual(caught.exception.code, "http_error")

    async def test_the_response_body_is_bounded(self):
        page = await self._fetch(
            "https://example.com/big",
            _html("<p>" + ("x" * 100_000) + "</p>"),
            max_response_bytes=1024,
        )

        self.assertTrue(page.truncated)
        self.assertLessEqual(len(page.text), 1024)

    async def test_the_extracted_text_is_bounded(self):
        page = await self._fetch(
            "https://example.com/big",
            _html("<p>" + ("word " * 5000) + "</p>"),
            max_characters=200,
        )

        self.assertTrue(page.truncated)
        self.assertLessEqual(len(page.text), 200)

    async def test_the_request_is_pinned_to_the_resolved_address(self):
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host, request.headers.get("Host")))
            return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

        await self._fetch("https://example.com/", handler)

        self.assertEqual(seen, [("93.184.216.34", "example.com")])

    async def test_a_public_host_is_resolved_before_connecting(self):
        resolver = RecordingResolver()

        await self._fetch(
            "https://example.com/",
            _html("ok", content_type="text/plain"),
            resolver=resolver,
        )

        self.assertEqual(resolver.calls, [("example.com", 443)])


class SsrfTests(unittest.IsolatedAsyncioTestCase):
    async def _fetch(self, url: str, resolver=None, handler=None):
        handler = handler or _html("ok", content_type="text/plain")
        fetcher = WebFetcher(
            _client(handler),
            resolver=resolver or RecordingResolver(),
        )
        try:
            return await fetcher.fetch(url)
        finally:
            await fetcher.aclose()

    async def test_a_host_resolving_to_loopback_is_refused(self):
        resolver = RecordingResolver({"evil.test": ("127.0.0.1",)})

        with self.assertRaises(UrlPolicyError) as caught:
            await self._fetch("https://evil.test/", resolver)

        self.assertEqual(caught.exception.code, "address_not_public")

    async def test_a_host_resolving_to_cloud_metadata_is_refused(self):
        resolver = RecordingResolver({"evil.test": ("169.254.169.254",)})

        with self.assertRaises(UrlPolicyError) as caught:
            await self._fetch("https://evil.test/", resolver)

        self.assertEqual(caught.exception.code, "address_not_public")

    async def test_one_bad_record_among_good_ones_refuses_the_whole_host(self):
        resolver = RecordingResolver({"evil.test": ("93.184.216.34", "127.0.0.1")})

        with self.assertRaises(UrlPolicyError) as caught:
            await self._fetch("https://evil.test/", resolver)

        self.assertEqual(caught.exception.code, "address_not_public")

    async def test_a_private_range_is_refused(self):
        resolver = RecordingResolver({"intranet.test": ("10.1.2.3",)})

        with self.assertRaises(UrlPolicyError):
            await self._fetch("https://intranet.test/", resolver)

    async def test_a_literal_loopback_url_is_refused(self):
        with self.assertRaises(UrlPolicyError):
            await self._fetch(
                "http://127.0.0.1:8080/admin",
                RecordingResolver({"127.0.0.1": ("127.0.0.1",)}),
            )

    async def test_a_file_url_is_refused(self):
        with self.assertRaises(UrlPolicyError) as caught:
            await self._fetch("file:///etc/passwd")

        self.assertEqual(caught.exception.code, "unsupported_scheme")

    async def test_a_dns_failure_is_reported(self):
        def failing(host: str, port: int):
            raise OSError("no such host")

        with self.assertRaises(UrlPolicyError) as caught:
            await self._fetch("https://nope.test/", failing)

        self.assertEqual(caught.exception.code, "dns_failure")


class RedirectTests(unittest.IsolatedAsyncioTestCase):
    def _chain(self, hops: dict[str, str]):
        def handler(request: httpx.Request) -> httpx.Response:
            location = hops.get(str(request.headers.get("Host")))
            if location is not None:
                return httpx.Response(302, headers={"location": location})
            return httpx.Response(
                200,
                text="landed",
                headers={"content-type": "text/plain"},
            )

        return handler

    async def _fetch(self, url: str, handler, resolver=None, **kwargs):
        fetcher = WebFetcher(
            _client(handler),
            resolver=resolver or RecordingResolver(),
            **kwargs,
        )
        try:
            return await fetcher.fetch(url)
        finally:
            await fetcher.aclose()

    async def test_a_redirect_is_followed(self):
        page = await self._fetch(
            "https://start.test/",
            self._chain({"start.test": "https://end.test/final"}),
        )

        self.assertEqual(page.text, "landed")
        self.assertEqual(page.final_url, "https://end.test/final")
        self.assertEqual(page.url, "https://start.test/")
        self.assertEqual(page.redirects, ("https://end.test/final",))

    async def test_a_redirect_into_a_private_address_is_refused(self):
        resolver = RecordingResolver({"internal.test": ("169.254.169.254",)})

        with self.assertRaises(UrlPolicyError) as caught:
            await self._fetch(
                "https://start.test/",
                self._chain({"start.test": "http://internal.test/latest/meta-data/"}),
                resolver=resolver,
            )

        self.assertEqual(caught.exception.code, "address_not_public")

    async def test_a_redirect_to_a_refused_scheme_is_reported(self):
        with self.assertRaises(WebFetchError) as caught:
            await self._fetch(
                "https://start.test/",
                self._chain({"start.test": "file:///etc/passwd"}),
            )

        self.assertEqual(caught.exception.code, "unsupported_scheme")

    async def test_a_redirect_loop_is_bounded(self):
        with self.assertRaises(WebFetchError) as caught:
            await self._fetch(
                "https://loop.test/",
                self._chain({"loop.test": "https://loop.test/again"}),
                max_redirects=2,
            )

        self.assertEqual(caught.exception.code, "too_many_redirects")

    async def test_a_relative_redirect_resolves_against_the_current_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/finish"})
            return httpx.Response(
                200,
                text="done",
                headers={"content-type": "text/plain"},
            )

        page = await self._fetch("https://example.com/start", handler)

        self.assertEqual(page.final_url, "https://example.com/finish")


class FetcherConstructionTests(unittest.TestCase):
    def test_invalid_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            WebFetcher(max_response_bytes=0)
        with self.assertRaises(ValueError):
            WebFetcher(max_redirects=-1)
        with self.assertRaises(ValueError):
            WebFetcher(timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
