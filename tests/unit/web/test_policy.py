from __future__ import annotations

import unittest

from truecoder.web.policy import (
    MAX_URL_LENGTH,
    UrlPolicyError,
    address_refusal,
    normalize_url,
    require_public_address,
)


class NormalizeUrlTests(unittest.TestCase):
    def _code(self, raw: str) -> str:
        with self.assertRaises(UrlPolicyError) as caught:
            normalize_url(raw)
        return caught.exception.code

    def test_an_https_url_is_accepted(self):
        target = normalize_url("https://example.com/docs")

        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.host, "example.com")
        self.assertEqual(target.port, 443)

    def test_http_defaults_to_port_80(self):
        self.assertEqual(normalize_url("http://example.com").port, 80)

    def test_an_explicit_port_is_kept(self):
        self.assertEqual(normalize_url("https://example.com:8443/x").port, 8443)

    def test_the_scheme_and_host_are_lowercased(self):
        target = normalize_url("HTTPS://Example.COM/Path")

        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.host, "example.com")
        self.assertEqual(target.url, "https://Example.COM/Path")

    def test_a_trailing_dot_is_removed_from_the_host(self):
        self.assertEqual(normalize_url("https://example.com./x").host, "example.com")

    def test_an_empty_path_becomes_a_slash(self):
        self.assertEqual(normalize_url("https://example.com").url, "https://example.com/")

    def test_a_fragment_is_dropped(self):
        self.assertEqual(
            normalize_url("https://example.com/a#section").url,
            "https://example.com/a",
        )

    def test_a_query_is_preserved(self):
        self.assertEqual(
            normalize_url("https://example.com/s?q=1&r=2").url,
            "https://example.com/s?q=1&r=2",
        )

    def test_non_http_schemes_are_refused(self):
        for raw in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/",
            "data:text/html,hello",
            "javascript:alert(1)",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self._code(raw), "unsupported_scheme")

    def test_credentials_in_the_url_are_refused(self):
        self.assertEqual(self._code("https://user:pass@example.com/"), "credentials_in_url")
        self.assertEqual(self._code("https://user@example.com/"), "credentials_in_url")

    def test_a_missing_host_is_refused(self):
        self.assertEqual(self._code("https:///path"), "invalid_url")

    def test_a_relative_url_is_refused(self):
        self.assertEqual(self._code("/docs/index.html"), "unsupported_scheme")

    def test_an_empty_url_is_refused(self):
        self.assertEqual(self._code("   "), "invalid_url")

    def test_an_oversized_url_is_refused(self):
        raw = "https://example.com/" + "a" * MAX_URL_LENGTH

        self.assertEqual(self._code(raw), "url_too_long")

    def test_whitespace_and_control_characters_are_refused(self):
        for raw in (
            "https://example.com/a b",
            "https://example.com/a\nb",
            "https://exa\tmple.com/",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self._code(raw), "invalid_url")

    def test_a_non_string_url_is_refused(self):
        self.assertEqual(self._code(None), "invalid_url")  # type: ignore[arg-type]

    def test_an_invalid_port_is_refused(self):
        self.assertEqual(self._code("https://example.com:99999/"), "invalid_url")
        self.assertEqual(self._code("https://example.com:notaport/"), "invalid_url")

    def test_the_origin_identifies_scheme_host_and_port(self):
        self.assertEqual(
            normalize_url("https://example.com/x").origin,
            ("https", "example.com", 443),
        )


class AddressPolicyTests(unittest.TestCase):
    def test_public_addresses_are_allowed(self):
        for address in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888"):
            with self.subTest(address=address):
                self.assertIsNone(address_refusal(address))

    def test_loopback_is_refused(self):
        for address in ("127.0.0.1", "127.1.2.3", "::1"):
            with self.subTest(address=address):
                self.assertEqual(address_refusal(address), "address_not_public")

    def test_private_ranges_are_refused(self):
        for address in ("10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.1"):
            with self.subTest(address=address):
                self.assertEqual(address_refusal(address), "address_not_public")

    def test_cloud_metadata_is_refused(self):
        for address in ("169.254.169.254", "169.254.170.2", "fd00:ec2::254"):
            with self.subTest(address=address):
                self.assertEqual(address_refusal(address), "address_not_public")

    def test_carrier_grade_nat_is_refused(self):
        self.assertEqual(address_refusal("100.64.0.1"), "address_not_public")

    def test_unspecified_and_broadcast_are_refused(self):
        for address in ("0.0.0.0", "255.255.255.255", "::"):
            with self.subTest(address=address):
                self.assertEqual(address_refusal(address), "address_not_public")

    def test_multicast_and_reserved_are_refused(self):
        for address in ("224.0.0.1", "239.1.1.1", "240.0.0.1", "ff02::1"):
            with self.subTest(address=address):
                self.assertEqual(address_refusal(address), "address_not_public")

    def test_link_local_ipv6_is_refused(self):
        self.assertEqual(address_refusal("fe80::1"), "address_not_public")

    def test_unique_local_ipv6_is_refused(self):
        self.assertEqual(address_refusal("fc00::1"), "address_not_public")

    def test_ipv4_mapped_loopback_is_refused(self):
        self.assertEqual(address_refusal("::ffff:127.0.0.1"), "address_not_public")

    def test_ipv4_mapped_metadata_is_refused(self):
        self.assertEqual(address_refusal("::ffff:169.254.169.254"), "address_not_public")

    def test_a_mapped_public_address_is_allowed(self):
        self.assertIsNone(address_refusal("::ffff:8.8.8.8"))

    def test_six_to_four_wrapping_a_private_address_is_refused(self):
        self.assertEqual(address_refusal("2002:7f00:1::"), "address_not_public")

    def test_documentation_ranges_are_refused(self):
        for address in ("192.0.2.1", "198.51.100.1", "203.0.113.1", "2001:db8::1"):
            with self.subTest(address=address):
                self.assertEqual(address_refusal(address), "address_not_public")

    def test_a_malformed_address_is_refused(self):
        self.assertEqual(address_refusal("not-an-address"), "invalid_address")

    def test_require_public_address_raises_for_refused_addresses(self):
        with self.assertRaises(UrlPolicyError) as caught:
            require_public_address("127.0.0.1")

        self.assertEqual(caught.exception.code, "address_not_public")

    def test_require_public_address_raises_for_malformed_addresses(self):
        with self.assertRaises(UrlPolicyError) as caught:
            require_public_address("nonsense")

        self.assertEqual(caught.exception.code, "invalid_address")

    def test_require_public_address_accepts_a_public_address(self):
        self.assertIsNone(require_public_address("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
