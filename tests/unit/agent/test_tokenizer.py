from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from truecoder.agent.context import TiktokenTokenCounter
from truecoder.agent.tokenizer import (
    FALLBACK_ENCODING,
    approximate_tokens,
    configure_tokenizer_cache,
    default_tokenizer_cache_path,
    load_encoding,
    warm_tokenizer,
)

MEASURED_PROSE_TOKENS = 401
MEASURED_CODE_TOKENS = 2600
MEASURED_JAPANESE_TOKENS = 300


class CacheLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_the_cache_outlives_a_reboot(self):
        self.assertNotIn(
            tempfile.gettempdir(),
            str(default_tokenizer_cache_path()),
        )

    def test_the_cache_directory_is_published_to_tiktoken(self):
        target = self.root / "tokenizers"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TIKTOKEN_CACHE_DIR", None)
            os.environ.pop("DATA_GYM_CACHE_DIR", None)
            configure_tokenizer_cache(target)

            self.assertEqual(os.environ["TIKTOKEN_CACHE_DIR"], str(target))

        self.assertTrue(target.is_dir())

    def test_an_operator_choice_is_left_alone(self):
        with patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": "/somewhere"}):
            configure_tokenizer_cache(self.root / "ignored")

            self.assertEqual(os.environ["TIKTOKEN_CACHE_DIR"], "/somewhere")

        self.assertFalse((self.root / "ignored").exists())

    def test_an_unwritable_cache_directory_is_survivable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TIKTOKEN_CACHE_DIR", None)
            os.environ.pop("DATA_GYM_CACHE_DIR", None)
            with patch.object(Path, "mkdir", side_effect=OSError):
                configure_tokenizer_cache(self.root / "denied")

            self.assertNotIn("TIKTOKEN_CACHE_DIR", os.environ)


class LoadEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        isolated = patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": ""})
        isolated.start()
        self.addCleanup(isolated.stop)

    def test_an_unknown_model_falls_back_to_a_known_encoding(self):
        encoding = Mock()

        with (
            patch("tiktoken.encoding_for_model", side_effect=KeyError),
            patch("tiktoken.get_encoding", return_value=encoding) as get_encoding,
        ):
            self.assertIs(load_encoding("custom-model"), encoding)

        get_encoding.assert_called_once_with(FALLBACK_ENCODING)

    def test_an_unreachable_download_yields_no_encoding(self):
        with (
            patch("tiktoken.encoding_for_model", side_effect=KeyError),
            patch("tiktoken.get_encoding", side_effect=ConnectionError("offline")),
        ):
            self.assertIsNone(load_encoding("custom-model"))

    def test_a_failure_for_a_known_model_yields_no_encoding(self):
        with patch(
            "tiktoken.encoding_for_model",
            side_effect=ConnectionError("offline"),
        ):
            self.assertIsNone(load_encoding("gpt-4"))


class ApproximateTokenTests(unittest.TestCase):
    def test_empty_text_costs_nothing(self):
        self.assertEqual(approximate_tokens(""), 0)

    def test_any_text_costs_at_least_one_token(self):
        self.assertEqual(approximate_tokens("a"), 1)

    def test_the_estimate_does_not_undercount_english_prose(self):
        prose = "The quick brown fox jumps over the lazy dog. " * 40

        self.assertGreaterEqual(approximate_tokens(prose), MEASURED_PROSE_TOKENS)

    def test_the_estimate_does_not_undercount_dense_code(self):
        code = "\n".join(
            f"    self.assertEqual(result_{n}.value, expected_{n})" for n in range(200)
        )

        self.assertGreaterEqual(approximate_tokens(code), MEASURED_CODE_TOKENS)

    def test_the_estimate_does_not_undercount_non_latin_text(self):
        self.assertGreaterEqual(
            approximate_tokens("日本語のテキスト" * 50),
            MEASURED_JAPANESE_TOKENS,
        )


class LazyLoadingTests(unittest.TestCase):
    def test_building_a_counter_does_not_load_an_encoding(self):
        with patch(
            "truecoder.agent.context.load_encoding",
            side_effect=AssertionError("startup must not load an encoding"),
        ):
            TiktokenTokenCounter("gpt-4")

    def test_the_encoding_is_loaded_once_and_reused(self):
        encoding = Mock()
        encoding.encode.side_effect = lambda value: list(value)

        with patch(
            "truecoder.agent.context.load_encoding",
            return_value=encoding,
        ) as load:
            counter = TiktokenTokenCounter("gpt-4")
            counter.count_message({"role": "user", "content": "hi"})
            counter.count_message({"role": "user", "content": "there"})

        self.assertEqual(load.call_count, 1)

    def test_counting_survives_an_encoding_that_cannot_be_loaded(self):
        with patch("truecoder.agent.context.load_encoding", return_value=None):
            counter = TiktokenTokenCounter("gpt-4")
            tokens = counter.count_message({"role": "user", "content": "hello there"})

            self.assertTrue(counter.is_approximate)

        self.assertEqual(
            tokens,
            approximate_tokens("user")
            + approximate_tokens("hello there")
            + TiktokenTokenCounter.MESSAGE_OVERHEAD,
        )

    def test_a_loaded_encoding_is_reported_as_exact(self):
        encoding = Mock()
        encoding.encode.side_effect = lambda value: list(value)

        with patch("truecoder.agent.context.load_encoding", return_value=encoding):
            self.assertFalse(TiktokenTokenCounter("gpt-4").is_approximate)

    def test_concurrent_counting_loads_the_encoding_once(self):
        encoding = Mock()
        encoding.encode.side_effect = lambda value: list(value)
        contenders = 8
        gate = threading.Barrier(contenders)

        def slow_load(_model):
            time.sleep(0.05)
            return encoding

        with patch(
            "truecoder.agent.context.load_encoding", side_effect=slow_load
        ) as load:
            counter = TiktokenTokenCounter("gpt-4")

            def contend():
                gate.wait(timeout=5)
                counter.count_message({"role": "user", "content": "hi"})

            threads = [
                threading.Thread(target=contend, daemon=True) for _ in range(contenders)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        self.assertEqual(load.call_count, 1)

    def test_a_loader_that_throws_is_not_retried_on_every_count(self):
        with patch(
            "truecoder.agent.context.load_encoding",
            side_effect=RuntimeError("broken loader"),
        ) as load:
            counter = TiktokenTokenCounter("gpt-4")

            with self.assertRaises(RuntimeError):
                counter.count_message({"role": "user", "content": "hi"})

            self.assertEqual(
                counter.count_message({"role": "user", "content": "hi"}),
                approximate_tokens("user")
                + approximate_tokens("hi")
                + TiktokenTokenCounter.MESSAGE_OVERHEAD,
            )

        self.assertEqual(load.call_count, 1)


class WarmupTests(unittest.TestCase):
    def test_the_encoding_is_loaded_off_the_calling_thread(self):
        counter = TiktokenTokenCounter("gpt-4")
        loading = threading.Event()
        release = threading.Event()

        def blocking_load(_model):
            loading.set()
            release.wait(timeout=5)
            return Mock(encode=lambda value: list(value))

        with patch("truecoder.agent.context.load_encoding", side_effect=blocking_load):
            thread = warm_tokenizer(counter)

            assert thread is not None
            self.assertTrue(loading.wait(timeout=5))
            self.assertTrue(thread.is_alive())

            release.set()
            thread.join(timeout=5)

        self.assertFalse(counter.is_approximate)

    def test_the_warmup_thread_never_holds_the_process_open(self):
        with patch("truecoder.agent.context.load_encoding", return_value=None):
            thread = warm_tokenizer(TiktokenTokenCounter("gpt-4"))

            assert thread is not None
            self.assertTrue(thread.daemon)
            thread.join(timeout=5)

    def test_a_counter_with_nothing_to_prepare_is_left_alone(self):
        class EagerCounter:
            def count_message(self, message):
                return 1

        self.assertIsNone(warm_tokenizer(EagerCounter()))


if __name__ == "__main__":
    unittest.main()
