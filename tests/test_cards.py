import json
import unittest

import cards


def answer(spec, text="Here it is."):
    return f"{text}\n\n```card\n{json.dumps(spec)}\n```"


class SplitTest(unittest.TestCase):
    def test_answer_without_a_card_is_text_only(self):
        self.assertEqual(cards.split("just text\n```py\nx = 1\n```"), ("just text\n```py\nx = 1\n```", None))

    def test_trailing_card_is_split_off(self):
        text, spec = cards.split(answer([{"type": "divider"}]))
        self.assertEqual((text, spec), ("Here it is.", [{"type": "divider"}]))

    def test_single_component_object_is_a_list(self):
        self.assertEqual(cards.split(answer({"type": "divider"}))[1], [{"type": "divider"}])

    def test_card_that_is_not_json_is_an_error(self):
        with self.assertRaises(cards.CardError):
            cards.split("hi\n```card\n[{oops}\n```")


class BlocksTest(unittest.TestCase):
    def build(self, *spec, text="Answer"):
        return cards.blocks(text, list(spec))

    def test_text_becomes_sections_of_at_most_3000_characters(self):
        blocks = cards.text_blocks("a" * 2000 + "\n" + "b" * 2000 + "\n" + "c" * 7000)
        self.assertEqual([len(b["text"]["text"]) for b in blocks], [2000, 2000, 3000, 3000, 1000])

    def test_table_becomes_a_data_table_with_number_cells(self):
        [_, table] = self.build({"type": "table", "caption": "Slow", "columns": ["Query", "ms"],
                                 "rows": [["users", 120], ["orders", 95.5]]})
        self.assertEqual(table["type"], "data_table")
        self.assertEqual(table["rows"][0], [{"type": "raw_text", "text": "Query"}, {"type": "raw_text", "text": "ms"}])
        self.assertEqual(table["rows"][1][1], {"type": "raw_number", "value": 120, "text": "120"})
        self.assertEqual(table["page_size"], 2)

    def test_table_rows_must_match_the_columns(self):
        with self.assertRaisesRegex(cards.CardError, "2 cells"):
            self.build({"type": "table", "caption": "x", "columns": ["a", "b"], "rows": [["only one"]]})

    def test_bar_chart_pairs_values_with_categories(self):
        [_, chart] = self.build({"type": "chart", "chart": "bar", "title": "Errors", "categories": ["Mon", "Tue"],
                                 "series": [{"name": "errors", "values": [3, 5]}], "y_label": "Errors"})
        self.assertEqual(chart, {"type": "data_visualization", "title": "Errors", "chart": {
            "type": "bar", "series": [{"name": "errors", "data": [{"label": "Mon", "value": 3},
                                                                  {"label": "Tue", "value": 5}]}],
            "axis_config": {"categories": ["Mon", "Tue"], "y_label": "Errors"}}})

    def test_series_needs_one_value_per_category(self):
        with self.assertRaises(cards.CardError):
            self.build({"type": "chart", "chart": "line", "title": "t", "categories": ["a", "b"],
                        "series": [{"name": "s", "values": [1]}]})

    def test_pie_values_must_be_positive(self):
        with self.assertRaisesRegex(cards.CardError, "greater than 0"):
            self.build({"type": "chart", "chart": "pie", "title": "t", "segments": [{"label": "a", "value": 0}]})

    def test_slack_shows_at_most_two_charts(self):
        pie = {"type": "chart", "chart": "pie", "title": "t", "segments": [{"label": "a", "value": 1}]}
        with self.assertRaisesRegex(cards.CardError, "at most 2"):
            self.build(pie, pie, pie)

    def test_long_chart_title_is_refused(self):
        with self.assertRaisesRegex(cards.CardError, "50"):
            self.build({"type": "chart", "chart": "pie", "title": "t" * 51, "segments": [{"label": "a", "value": 1}]})

    def test_buttons_reply_or_link(self):
        [_, actions] = self.build({"type": "buttons", "buttons": [
            {"text": "Merge it", "reply": "merge PR #12", "style": "primary"},
            {"text": "Open PR", "url": "https://example.test/pr/12"}]})
        merge, link = actions["elements"]
        self.assertEqual((merge["action_id"], merge["value"], merge["style"]), ("claudino_reply_0_0", "merge PR #12", "primary"))
        self.assertEqual((link["action_id"], link["url"]), ("claudino_link_0_1", "https://example.test/pr/12"))
        self.assertNotIn("value", link)

    def test_fields_header_context_and_divider(self):
        blocks = self.build({"type": "header", "text": "Deploy"}, {"type": "fields", "fields": [["CI", "green"]]},
                            {"type": "context", "text": "now"}, {"type": "divider"})
        self.assertEqual([b["type"] for b in blocks], ["section", "header", "section", "context", "divider"])
        self.assertEqual(blocks[2]["fields"][0]["text"], "*CI*\ngreen")

    def test_unknown_component_is_an_error(self):
        with self.assertRaisesRegex(cards.CardError, "unknown"):
            self.build({"type": "video"})

    def test_summary_names_what_the_text_leaves_out(self):
        spec = [{"type": "table", "caption": "Slow", "rows": [[1], [2]]}, {"type": "buttons", "buttons": [{"text": "Go"}]}]
        self.assertEqual(cards.summary(spec), '[card: table "Slow" (2 rows); buttons: Go]')


if __name__ == "__main__":
    unittest.main()
