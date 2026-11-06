import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "slack_probe", Path(__file__).resolve().parent.parent / "scripts" / "slack-probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class SlackProbeTest(unittest.TestCase):
    def test_values_stay_strings_so_timestamps_keep_their_digits(self):
        self.assertEqual(probe.params(["thread_ts=1700000000.000100", "status=processing"]),
                         {"thread_ts": "1700000000.000100", "status": "processing"})

    def test_empty_value_clears(self):
        self.assertEqual(probe.params(["status="]), {"status": ""})

    def test_lists_and_objects_are_sent_as_json(self):
        self.assertEqual(probe.params(['loading_messages=["one","two"]']), {"loading_messages": ["one", "two"]})

    def test_pair_without_a_value_is_refused(self):
        with self.assertRaises(SystemExit):
            probe.params(["status"])


if __name__ == "__main__":
    unittest.main()
