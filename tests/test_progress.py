import time
import unittest
from unittest import mock

from slack_sdk.errors import SlackApiError

import progress


def turn(*content):
    return {"type": "assistant", "message": {"content": list(content)}}


def tool(id_, name="Bash", **args):
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


def result(id_, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": id_, "is_error": error}]}}


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.client = mock.Mock()
        self.client.chat_startStream.side_effect = lambda **kw: {"ts": f"{len(self.client.chat_startStream.call_args_list)}.0"}
        self.p = progress.Progress(self.client, "C1", "1.0", "UALICE", "T1", clock=lambda: self.now)

    def sent(self):
        calls = (self.client.chat_startStream.call_args_list + self.client.chat_appendStream.call_args_list
                 + self.client.chat_stopStream.call_args_list)
        return [c for call in calls for c in call.kwargs.get("chunks", [])]

    def test_first_flush_starts_the_stream_in_the_thread(self):
        self.p.on_event(turn({"type": "text", "text": "Looking"}, tool("t1", command="ls")))
        self.p.flush()
        kwargs = self.client.chat_startStream.call_args.kwargs
        self.assertEqual((kwargs["channel"], kwargs["thread_ts"], kwargs["recipient_team_id"]), ("C1", "1.0", "T1"))
        self.assertEqual(kwargs["chunks"], [{"type": "markdown_text", "text": "Looking\n\n"},
                                            {"type": "task_update", "id": "t1", "title": "Run `ls`", "status": "in_progress"}])

    def test_later_flushes_append_and_close_stops(self):
        self.p.on_event(turn(tool("t1", command="ls")))
        self.p.flush()
        self.p.on_event(result("t1", error=True))
        self.p.flush()
        self.assertEqual(self.client.chat_appendStream.call_args.kwargs["chunks"][0]["status"], "error")
        self.p.close()
        self.client.chat_stopStream.assert_called_once_with(channel="C1", ts="1.0")

    def test_text_only_turns_are_left_for_the_answer(self):
        self.p.on_event(turn({"type": "text", "text": "Final answer"}))
        self.p.flush()
        self.client.chat_startStream.assert_not_called()

    def test_subagent_events_and_hidden_tools_are_skipped(self):
        self.p.on_event({**turn(tool("t2")), "parent_tool_use_id": "t1"})
        self.p.on_event(turn(tool("t3", name="TodoWrite")))
        self.p.flush()
        self.client.chat_startStream.assert_not_called()

    def test_only_the_latest_update_of_a_task_is_sent(self):
        self.p.on_event(turn(tool("t1", command="ls")))
        self.p.on_event(result("t1"))
        self.p.flush()
        self.assertEqual(self.sent(), [{"type": "task_update", "id": "t1", "title": "Run `ls`", "status": "complete"}])

    def test_stream_moves_on_before_slacks_ten_minutes(self):
        self.p.on_event(turn(tool("t1", command="make test")))
        self.p.flush()
        self.now = progress.ROTATE_SECONDS + 1
        self.p.on_event(turn(tool("t2", command="ls")))
        self.p.flush()
        self.client.chat_stopStream.assert_called_once_with(channel="C1", ts="1.0")
        self.assertEqual([c["id"] for c in self.client.chat_startStream.call_args.kwargs["chunks"]], ["t1", "t2"])
        self.assertEqual(self.p.ts, "2.0")

    def test_stream_moves_on_before_the_text_cap(self):
        self.p.on_event(turn({"type": "text", "text": "a" * 6000}, tool("t1")))
        self.p.flush()
        self.p.on_event(turn({"type": "text", "text": "b" * 6000}, tool("t2")))
        self.p.flush()
        self.assertEqual(self.client.chat_startStream.call_count, 2)

    def test_close_completes_or_fails_open_tasks(self):
        self.p.on_event(turn(tool("t1", command="ls")))
        self.p.flush()
        self.p.close(ok=False)
        self.assertEqual(self.client.chat_stopStream.call_args.kwargs["chunks"][0]["status"], "error")

    def test_delete_removes_every_stream_message_and_survives_errors(self):
        self.p.on_event(turn(tool("t1")))
        self.p.flush()
        self.now = progress.ROTATE_SECONDS + 1
        self.p.on_event(turn(tool("t2")))
        self.p.flush()
        self.p.close()
        self.client.chat_delete.side_effect = [SlackApiError("no", {"error": "message_not_found"}), None]
        self.p.delete()
        self.assertEqual([c.kwargs for c in self.client.chat_delete.call_args_list],
                         [{"channel": "C1", "ts": "1.0"}, {"channel": "C1", "ts": "2.0"}])

    def test_slack_errors_turn_the_stream_off(self):
        self.client.chat_startStream.side_effect = SlackApiError("no", {"error": "channel_type_not_supported"})
        self.p.on_event(turn(tool("t1")))
        self.p.flush()
        self.p.on_event(turn(tool("t2")))
        self.p.flush()
        self.p.close()
        self.assertEqual(self.client.chat_startStream.call_count, 1)
        self.client.chat_stopStream.assert_not_called()

    def test_background_flusher_sends_without_new_events(self):
        with mock.patch.object(progress, "FLUSH_SECONDS", 0.01):
            self.p.start()
            self.p.on_event(turn(tool("t1", command="sleep 300")))
            for _ in range(200):
                if self.client.chat_startStream.called:
                    break
                time.sleep(0.01)
            self.p.close()
        self.assertTrue(self.client.chat_startStream.called)




class TitleTest(unittest.TestCase):
    def test_titles(self):
        cases = [
            ("Bash", {"command": "pytest -q", "description": "Run the tests"}, "Run the tests"),
            ("Read", {"file_path": "/home/x/relay.py"}, "Read relay.py"),
            ("Edit", {"file_path": "/home/x/cards.py"}, "Edit cards.py"),
            ("Grep", {"pattern": "def post"}, "Search for `def post`"),
            ("WebFetch", {"url": "https://docs.slack.dev/reference"}, "Read docs.slack.dev"),
            ("Task", {"description": "find callers"}, "Delegate: find callers"),
            ("mcp__github__create_pull_request", {}, "github: create pull request"),
        ]
        for name, args, title in cases:
            self.assertEqual(progress.task_title(name, args), title)

    def test_tokens_in_commands_are_redacted(self):
        title = progress.task_title("Bash", {"command": "curl -H 'Authorization: Bearer ghp_abcdefghijklmnop123456' x"})
        self.assertNotIn("ghp_", title)
        self.assertIn("[redacted]", progress.redact("key a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"))
        self.assertEqual(progress.redact("/home/facundo/claudino-conversion-branch"), "/home/facundo/claudino-conversion-branch")

    def test_long_titles_are_cut(self):
        self.assertEqual(len(progress.task_title("Bash", {"description": "x" * 200})), progress.TITLE_LIMIT)


if __name__ == "__main__":
    unittest.main()
