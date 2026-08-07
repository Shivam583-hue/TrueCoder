from __future__ import annotations

import unittest

from truecoder.web.extract import extract_plain_text, extract_text


class ExtractTextTests(unittest.TestCase):
    def test_visible_text_is_returned(self):
        document = extract_text("<html><body><p>Hello world</p></body></html>")

        self.assertEqual(document.text, "Hello world")
        self.assertFalse(document.truncated)

    def test_the_title_is_captured_separately(self):
        document = extract_text("<html><head><title>Docs</title></head><body>Body</body></html>")

        self.assertEqual(document.title, "Docs")
        self.assertEqual(document.text, "Body")

    def test_scripts_and_styles_are_discarded(self):
        markup = """
        <html><head><style>body { color: red; }</style></head>
        <body><script>alert('x')</script><p>Real content</p></body></html>
        """

        document = extract_text(markup)

        self.assertEqual(document.text, "Real content")

    def test_other_non_content_elements_are_discarded(self):
        markup = (
            "<body><noscript>enable js</noscript><svg><path/></svg>"
            "<iframe>frame</iframe><p>Kept</p></body>"
        )

        self.assertEqual(extract_text(markup).text, "Kept")

    def test_block_elements_become_line_breaks(self):
        markup = "<body><p>One</p><p>Two</p><div>Three</div></body>"

        self.assertEqual(extract_text(markup).text, "One\n\nTwo\n\nThree")

    def test_list_items_are_separated(self):
        markup = "<ul><li>alpha</li><li>beta</li></ul>"

        self.assertEqual(extract_text(markup).text, "alpha\n\nbeta")

    def test_inline_elements_do_not_break_lines(self):
        markup = "<p>Hello <strong>brave</strong> <em>world</em></p>"

        self.assertEqual(extract_text(markup).text, "Hello brave world")

    def test_a_line_break_splits_one_line(self):
        self.assertEqual(extract_text("<p>a<br/>b</p>").text, "a\nb")
        self.assertEqual(extract_text("<p>a<br>b</p>").text, "a\nb")

    def test_preformatted_whitespace_is_preserved(self):
        markup = "<pre>def f():\n    return {\n        'a': 1,\n    }\n</pre>"

        text = extract_text(markup).text

        self.assertIn("def f():\n    return {", text)
        self.assertIn("\n        'a': 1,", text)

    def test_whitespace_outside_pre_is_still_collapsed(self):
        markup = "<p>intro</p><pre>    indented</pre><p>not     kept</p>"

        text = extract_text(markup).text

        self.assertIn("\n    indented", text)
        self.assertIn("not kept", text)

    def test_entities_are_decoded(self):
        markup = "<p>5 &lt; 10 &amp;&amp; 10 &gt; 5 &mdash; ok&nbsp;then</p>"

        self.assertIn("5 < 10 && 10 > 5", extract_text(markup).text)

    def test_runs_of_whitespace_collapse(self):
        markup = "<p>lots     of\n\n\n\n     space</p>"

        self.assertEqual(extract_text(markup).text, "lots of space")

    def test_output_is_bounded(self):
        markup = "<p>" + ("word " * 5000) + "</p>"

        document = extract_text(markup, max_characters=100)

        self.assertLessEqual(len(document.text), 100)
        self.assertTrue(document.truncated)

    def test_content_within_the_bound_is_not_marked_truncated(self):
        document = extract_text("<p>short</p>", max_characters=100)

        self.assertFalse(document.truncated)

    def test_malformed_markup_still_yields_text(self):
        markup = "<p>unclosed <div><span>content</p></body"

        self.assertIn("content", extract_text(markup).text)

    def test_an_empty_document_yields_empty_text(self):
        document = extract_text("")

        self.assertEqual(document.text, "")
        self.assertEqual(document.title, "")

    def test_a_document_without_a_title_reports_an_empty_title(self):
        self.assertEqual(extract_text("<body><p>x</p></body>").title, "")

    def test_non_string_markup_is_rejected(self):
        with self.assertRaises(TypeError):
            extract_text(b"<p>x</p>")  # type: ignore[arg-type]

    def test_an_invalid_bound_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_text("<p>x</p>", max_characters=0)


class ExtractPlainTextTests(unittest.TestCase):
    def test_plain_text_is_tidied_but_kept(self):
        document = extract_plain_text("line one\r\nline two\n\n\n\nline three")

        self.assertEqual(document.text, "line one\nline two\n\nline three")
        self.assertEqual(document.title, "")

    def test_plain_text_is_bounded(self):
        document = extract_plain_text("x" * 500, max_characters=50)

        self.assertEqual(len(document.text), 50)
        self.assertTrue(document.truncated)

    def test_markup_in_plain_text_is_left_alone(self):
        document = extract_plain_text("<p>not parsed</p>")

        self.assertEqual(document.text, "<p>not parsed</p>")

    def test_non_string_input_is_rejected(self):
        with self.assertRaises(TypeError):
            extract_plain_text(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
