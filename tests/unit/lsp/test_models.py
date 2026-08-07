from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.lsp.models import (
    display_path,
    language_id_for,
    parse_diagnostics,
    parse_hover,
    parse_locations,
    parse_range,
    parse_symbols,
    path_to_uri,
    uri_to_path,
)

ROOT = Path("/workspace")


def _range(line: int = 1) -> dict:
    return {
        "start": {"line": line, "character": 2},
        "end": {"line": line, "character": 8},
    }


class UriTests(unittest.TestCase):
    def test_a_path_round_trips_through_a_uri(self):
        path = Path(__file__).resolve()

        self.assertEqual(uri_to_path(path_to_uri(path)), path)

    def test_a_percent_encoded_uri_is_decoded(self):
        self.assertEqual(
            uri_to_path("file:///workspace/my%20file.py"),
            Path("/workspace/my file.py"),
        )

    def test_a_non_file_uri_has_no_path(self):
        self.assertIsNone(uri_to_path("untitled:Untitled-1"))

    def test_a_workspace_uri_is_shown_relative(self):
        self.assertEqual(display_path("file:///workspace/src/a.py", ROOT), "src/a.py")

    def test_an_outside_uri_keeps_its_full_path(self):
        self.assertEqual(display_path("file:///elsewhere/a.py", ROOT), "/elsewhere/a.py")

    def test_an_unparseable_uri_is_returned_unchanged(self):
        self.assertEqual(display_path("untitled:x", ROOT), "untitled:x")


class LanguageIdTests(unittest.TestCase):
    def test_known_extensions_map_to_language_ids(self):
        self.assertEqual(language_id_for(Path("a.py")), "python")
        self.assertEqual(language_id_for(Path("a.TS")), "typescript")
        self.assertEqual(language_id_for(Path("a.rs")), "rust")

    def test_unknown_extensions_fall_back_to_plaintext(self):
        self.assertEqual(language_id_for(Path("a.zzz")), "plaintext")


class ParseRangeTests(unittest.TestCase):
    def test_a_range_is_parsed(self):
        parsed = parse_range(_range(4))

        self.assertEqual(parsed.start.line, 4)
        self.assertEqual(parsed.start.one_based_line, 5)
        self.assertEqual(parsed.end.character, 8)

    def test_a_missing_range_becomes_the_origin(self):
        parsed = parse_range(None)

        self.assertEqual((parsed.start.line, parsed.start.character), (0, 0))


class ParseLocationsTests(unittest.TestCase):
    def test_a_single_location_is_accepted(self):
        locations = parse_locations(
            {"uri": "file:///workspace/a.py", "range": _range()},
            ROOT,
        )

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].path, "a.py")

    def test_a_list_of_locations_is_accepted(self):
        payload = [
            {"uri": "file:///workspace/a.py", "range": _range()},
            {"uri": "file:///workspace/b.py", "range": _range()},
        ]

        self.assertEqual(len(parse_locations(payload, ROOT)), 2)

    def test_location_links_are_accepted(self):
        payload = [
            {
                "targetUri": "file:///workspace/a.py",
                "targetSelectionRange": _range(6),
                "targetRange": _range(5),
            }
        ]

        locations = parse_locations(payload, ROOT)

        self.assertEqual(locations[0].path, "a.py")
        self.assertEqual(locations[0].range.start.line, 6)

    def test_a_null_result_is_empty(self):
        self.assertEqual(parse_locations(None, ROOT), ())

    def test_entries_without_a_uri_are_skipped(self):
        self.assertEqual(parse_locations([{"range": _range()}], ROOT), ())

    def test_a_location_labels_its_line(self):
        locations = parse_locations(
            {"uri": "file:///workspace/a.py", "range": _range(9)},
            ROOT,
        )

        self.assertEqual(locations[0].label, "a.py:10")


class ParseSymbolsTests(unittest.TestCase):
    def test_flat_symbol_information_is_parsed(self):
        payload = [
            {
                "name": "parse",
                "kind": 12,
                "containerName": "Parser",
                "location": {"uri": "file:///workspace/a.py", "range": _range()},
            }
        ]

        symbols = parse_symbols(payload, ROOT)

        self.assertEqual(symbols[0].name, "parse")
        self.assertEqual(symbols[0].kind, "function")
        self.assertEqual(symbols[0].container, "Parser")
        self.assertEqual(symbols[0].location.path, "a.py")

    def test_hierarchical_document_symbols_are_flattened(self):
        payload = [
            {
                "name": "Parser",
                "kind": 5,
                "range": _range(1),
                "selectionRange": _range(1),
                "children": [
                    {
                        "name": "parse",
                        "kind": 6,
                        "range": _range(3),
                        "selectionRange": _range(3),
                    }
                ],
            }
        ]

        symbols = parse_symbols(payload, ROOT, default_uri="file:///workspace/a.py")

        self.assertEqual([s.name for s in symbols], ["Parser", "parse"])
        self.assertEqual(symbols[1].kind, "method")
        self.assertEqual(symbols[1].container, "Parser")
        self.assertEqual(symbols[1].location.path, "a.py")

    def test_an_unknown_kind_is_named(self):
        payload = [
            {
                "name": "x",
                "kind": 999,
                "location": {"uri": "file:///workspace/a.py", "range": _range()},
            }
        ]

        self.assertEqual(parse_symbols(payload, ROOT)[0].kind, "unknown")

    def test_a_non_list_payload_is_empty(self):
        self.assertEqual(parse_symbols({"name": "x"}, ROOT), ())

    def test_entries_without_a_name_are_skipped(self):
        payload = [{"kind": 12, "location": {"uri": "file:///a.py", "range": _range()}}]

        self.assertEqual(parse_symbols(payload, ROOT), ())


class ParseDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_are_parsed_with_severity_names(self):
        payload = [
            {"range": _range(2), "severity": 1, "message": "bad", "source": "pyright"}
        ]

        diagnostics = parse_diagnostics(payload, "file:///workspace/a.py", ROOT)

        self.assertEqual(diagnostics[0].severity, "error")
        self.assertEqual(diagnostics[0].message, "bad")
        self.assertEqual(diagnostics[0].source, "pyright")
        self.assertEqual(diagnostics[0].path, "a.py")

    def test_a_missing_severity_defaults_to_information(self):
        payload = [{"range": _range(), "message": "note"}]

        self.assertEqual(
            parse_diagnostics(payload, "file:///workspace/a.py", ROOT)[0].severity,
            "information",
        )

    def test_entries_without_a_message_are_skipped(self):
        self.assertEqual(
            parse_diagnostics([{"range": _range()}], "file:///a.py", ROOT),
            (),
        )


class ParseHoverTests(unittest.TestCase):
    def test_markup_content_is_read(self):
        payload = {"contents": {"kind": "markdown", "value": "def parse()"}}

        self.assertEqual(parse_hover(payload), "def parse()")

    def test_a_plain_string_is_read(self):
        self.assertEqual(parse_hover({"contents": "text"}), "text")

    def test_a_list_of_marked_strings_is_joined(self):
        payload = {"contents": [{"value": "one"}, {"value": "two"}]}

        self.assertEqual(parse_hover(payload), "one\n\ntwo")

    def test_a_null_hover_is_empty(self):
        self.assertEqual(parse_hover(None), "")

    def test_an_empty_object_is_empty(self):
        self.assertEqual(parse_hover({}), "")


if __name__ == "__main__":
    unittest.main()
