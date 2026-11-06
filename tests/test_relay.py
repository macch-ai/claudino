import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from slack_sdk.errors import SlackApiError

from tests.helpers import RELAY_USER_ID, load_relay

ALICE, BOB, OUTSIDER = "UALICE", "UBOB", "UOUTSIDER"
CLAUDE_TAG = "UCLAUDE"
ALERTS, SENTRY = "CALERTS", "USENTRY"


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.r = load_relay()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, path in (("RUNS_FILE", self.tmp / "runs.json"), ("FILES_DIR", self.tmp / "files")):
            patcher = mock.patch.object(self.r, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.r.busy.clear()
        self.r.active_threads.clear()
        self.client = self.r.app.client
        self.client.conversations_replies.return_value = {"messages": []}
        patcher = mock.patch.multiple(
            self.r.config, ALLOWED_USERS={ALICE, BOB}, ALERT_CHANNEL=ALERTS, ALERT_BOT_USER=SENTRY,
            ALERT_ROUTINE_URL="https://example.test/fire", CLAUDE_APP_USER=CLAUDE_TAG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def msg(self, text, channel="C1", ts="2.0", thread_ts=None, user=ALICE):
        m = {"text": text, "channel": channel, "ts": ts, "user": user}
        if thread_ts:
            m["thread_ts"] = thread_ts
        return m


class AddressedToClaudinoTest(RelayTest):
    def test_direct_mention_counts_in_any_channel(self):
        self.assertTrue(self.r.addressed_to_claudino(self.msg(f"<@{RELAY_USER_ID}> hi")))

    def test_claude_app_mention_counts_only_in_the_alert_channel(self):
        self.assertTrue(self.r.addressed_to_claudino(self.msg(f"<@{CLAUDE_TAG}> look", channel=ALERTS)))
        self.assertFalse(self.r.addressed_to_claudino(self.msg(f"<@{CLAUDE_TAG}> look")))

    def test_top_level_message_without_mention_is_ignored(self):
        self.assertFalse(self.r.addressed_to_claudino(self.msg("hi")))

    def test_thread_reply_without_mention_where_claudino_took_part(self):
        self.r.active_threads.add(("C1", "1.0"))
        self.assertTrue(self.r.addressed_to_claudino(self.msg("and that?", thread_ts="1.0")))

    def test_thread_participation_is_read_from_slack(self):
        self.client.conversations_replies.return_value = {"messages": [{"user": "UX", "text": f"<@{RELAY_USER_ID}> start"}]}
        self.assertTrue(self.r.addressed_to_claudino(self.msg("go on", thread_ts="1.0")))
        self.assertIn(("C1", "1.0"), self.r.active_threads)

    def test_thread_without_claudino_is_ignored(self):
        self.client.conversations_replies.return_value = {"messages": [{"user": "UX", "text": "hi"}]}
        self.assertFalse(self.r.addressed_to_claudino(self.msg("go on", thread_ts="1.0")))

    def test_reply_mentioning_someone_else_is_not_for_claudino(self):
        self.r.active_threads.add(("C1", "1.0"))
        self.assertFalse(self.r.addressed_to_claudino(self.msg(f"<@{BOB}> look at this", thread_ts="1.0")))

    def test_sent_using_claude_footer_is_not_someone_else(self):
        self.r.active_threads.add(("C1", "1.0"))
        text = f"ok\n*Sent using* <@{CLAUDE_TAG}|Claude>"
        self.assertTrue(self.r.addressed_to_claudino(self.msg(text, thread_ts="1.0")))


class OnMessageTest(RelayTest):
    def test_alerts_are_off_without_alert_settings(self):
        with mock.patch.object(self.r.config, "ALERT_ROUTINE_URL", ""):
            self.assertFalse(self.r.is_sentry_alert({"channel": ALERTS, "user": SENTRY, "ts": "5.0"}))

    def test_own_bot_messages_are_recognised(self):
        self.assertTrue(self.r.is_from_claudino({"bot_id": "BRELAY"}))
        self.assertFalse(self.r.is_from_claudino({"bot_id": "BOTHER"}))

    def test_sentry_alert_is_handed_over(self):
        with mock.patch.object(self.r, "hand_over") as hand_over:
            self.r.on_message({"channel": ALERTS, "user": SENTRY, "ts": "5.0"})
        hand_over.assert_called_once_with("5.0")

    def test_sentry_thread_reply_is_not_an_alert(self):
        self.assertFalse(self.r.is_sentry_alert({"user": SENTRY, "ts": "6.0", "thread_ts": "5.0"}))

    def test_allowed_user_mention_is_dispatched(self):
        with mock.patch.object(self.r, "dispatch") as dispatch:
            self.r.on_message(self.msg(f"<@{RELAY_USER_ID}> hi", ts="1.0"))
        dispatch.assert_called_once()

    def test_outsider_mention_is_ignored(self):
        with mock.patch.object(self.r, "dispatch") as dispatch:
            self.r.on_message(self.msg(f"<@{RELAY_USER_ID}> hi", ts="1.0", user=OUTSIDER))
        dispatch.assert_not_called()

    def test_bot_messages_and_edits_are_ignored(self):
        with mock.patch.object(self.r, "dispatch") as dispatch:
            self.r.on_message({**self.msg(f"<@{RELAY_USER_ID}> hi"), "bot_id": "B1"})
            self.r.on_message({**self.msg(f"<@{RELAY_USER_ID}> hi"), "subtype": "message_changed"})
        dispatch.assert_not_called()

    def test_claudino_reply_ends_the_run(self):
        with mock.patch.object(self.r, "reply_done") as reply_done:
            self.r.on_message({"channel": "C1", "user": RELAY_USER_ID, "bot_id": "B1", "ts": "3.0", "thread_ts": "1.0"})
        reply_done.assert_called_once_with("1.0")


class QueueTest(RelayTest):
    def setUp(self):
        super().setUp()
        self.start = mock.patch.object(self.r, "start").start()
        mock.patch.object(self.r, "ack").start()
        self.update_loading = mock.patch.object(self.r, "update_loading").start()
        self.addCleanup(mock.patch.stopall)

    def dispatch(self, ts, thread_ts=None, user=ALICE):
        message = {"channel": "C1", "ts": ts, "user": user}
        if thread_ts:
            message["thread_ts"] = thread_ts
        self.r.dispatch(message)

    def test_first_request_starts_a_run(self):
        self.dispatch("100.0")
        self.start.assert_called_once_with("C1", "100.0", [("100.0", ALICE)])
        self.assertIn("100.0", self.r.busy)

    def test_requests_during_a_run_are_queued(self):
        self.dispatch("100.0")
        self.dispatch("101.0", "100.0", BOB)
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.r.busy["100.0"]["pending"], [("101.0", BOB)])

    def test_every_request_gets_its_own_loading_lines(self):
        self.r.loading_messages["100.0"] = ["Old request"]
        self.dispatch("100.0")
        self.assertNotIn("100.0", self.r.loading_messages)
        self.dispatch("101.0", "100.0", BOB)
        self.assertEqual(self.update_loading.call_count, 2)

    def test_reply_drops_requests_the_run_already_saw(self):
        self.dispatch("100.0")
        self.dispatch("101.0", "100.0")
        self.r.busy["100.0"]["seen_until"] = 101.5
        self.start.reset_mock()
        self.r.reply_done("100.0")
        self.start.assert_not_called()
        self.assertNotIn("100.0", self.r.busy)

    def test_reply_starts_requests_that_arrived_later(self):
        self.dispatch("100.0")
        self.dispatch("200.0", "100.0")
        self.r.busy["100.0"]["seen_until"] = 150.0
        self.start.reset_mock()
        self.r.reply_done("100.0")
        self.start.assert_called_once_with("C1", "100.0", [("200.0", ALICE)])
        self.assertIn("100.0", self.r.busy)

    def test_stale_run_does_not_block_the_thread(self):
        self.dispatch("100.0")
        self.r.busy["100.0"]["since"] = time.time() - self.r.BUSY_TIMEOUT_SECONDS - 1
        self.dispatch("101.0", "100.0")
        self.assertEqual(self.start.call_count, 2)

    def test_reply_without_a_run_is_harmless(self):
        self.r.reply_done("999.0")
        self.start.assert_not_called()


class ThreadPromptTest(RelayTest):
    MESSAGES = [
        {"ts": "1.0", "user": ALICE, "text": f"<@{RELAY_USER_ID}> hi"},
        {"ts": "2.0", "user": RELAY_USER_ID, "bot_id": "B1", "text": "hi!"},
        {"ts": "3.0", "user": ALICE, "text": "and now?"},
    ]

    def setUp(self):
        super().setUp()
        self.client.conversations_replies.return_value = {"messages": self.MESSAGES}

    def prompt(self, state):
        with mock.patch.object(self.r.claude_cli, "get", return_value=state):
            return self.r.thread_prompt("C1", "1.0", [("3.0", ALICE)], "C1:1.0")

    def test_first_turn_sends_the_whole_thread(self):
        prompt, last_seen = self.prompt({})
        self.assertIn("Thread so far", prompt)
        self.assertIn("claudino (you): hi!", prompt)
        self.assertEqual(last_seen, "3.0")

    def test_later_turns_send_only_new_messages(self):
        prompt, _ = self.prompt({"seen_ts": "2.0"})
        self.assertIn("New messages", prompt)
        self.assertIn("and now?", prompt)
        self.assertNotIn("hi!", prompt)


class ForwardFollowUpTest(RelayTest):
    """Follow-up turns in claudino's thread keep up with the forwarded message's thread."""
    FORWARD = {"ts": "10.0", "user": ALICE, "text": f"<@{RELAY_USER_ID}> why?", "attachments": [
        {"author_name": "Carol", "text": "deploy broke", "channel_id": "CSRC", "ts": "5.5",
         "from_url": "https://x.slack.com/archives/CSRC/p5500000?thread_ts=5.0&cid=CSRC"}]}
    SOURCE = [{"ts": "5.0", "user": BOB, "text": "starting deploy"},
              {"ts": "5.5", "user": "UCAROL", "text": "deploy broke"}]
    THREAD = [FORWARD, {"ts": "11.0", "user": RELAY_USER_ID, "bot_id": "B1", "text": "a bad migration"},
              {"ts": "12.0", "user": ALICE, "text": "fixed now?"}]

    def prompt(self, source, seen_ts):
        def replies(channel, ts, limit):
            return {"messages": source if channel == "CSRC" else self.THREAD}
        self.client.conversations_replies.side_effect = replies
        with mock.patch.object(self.r.claude_cli, "get", return_value={"seen_ts": seen_ts} if seen_ts else {}):
            return self.r.thread_prompt("C1", "10.0", [("12.0", ALICE)], "C1:10.0")[0]

    def test_first_turn_has_the_forwarded_thread(self):
        prompt = self.prompt(self.SOURCE, None)
        self.assertIn("starting deploy", prompt)
        self.assertIn("(the forwarded message): deploy broke", prompt)

    def test_follow_up_gets_new_replies_in_the_forwarded_thread(self):
        source = self.SOURCE + [{"ts": "11.5", "user": BOB, "text": "rolled back"}]
        prompt = self.prompt(source, "11.0")
        self.assertIn("fixed now?", prompt)
        self.assertIn("[new messages in the thread of the forwarded message 5.5 in <#CSRC>]", prompt)
        self.assertIn("rolled back", prompt)
        self.assertNotIn("starting deploy", prompt)  # the session already saw it

    def test_follow_up_without_new_forwarded_replies_adds_nothing(self):
        prompt = self.prompt(self.SOURCE, "11.0")
        self.assertNotIn("forwarded message", prompt)

    def test_new_session_after_a_failure_gets_the_whole_forwarded_thread_again(self):
        prompt = self.prompt(self.SOURCE, "0")
        self.assertIn("Thread so far", prompt)
        self.assertIn("starting deploy", prompt)


class RenderTest(RelayTest):
    def test_forwarded_message_text_is_included(self):
        out = self.r.render({"ts": "1.0", "text": "look at this", "attachments": [{"author_name": "Carol", "text": "hi team"}]})
        self.assertIn("look at this", out)
        self.assertIn("[forwarded/shared message from Carol]: hi team", out)

    def test_files_are_listed_with_their_path(self):
        with mock.patch.object(self.r, "file_note", return_value="[attached file x.png]") as note:
            out = self.r.render({"ts": "1.0", "text": "", "files": [{"id": "F1"}]})
        self.assertIn("[attached file x.png]", out)
        note.assert_called_once()

    def forward(self, **extra):
        return {"ts": "9.0", "text": "", "attachments": [
            {"author_name": "Carol", "text": "deploy broke", "channel_id": "CSRC", "ts": "5.5", **extra}]}

    def test_forwarded_reply_brings_its_thread(self):
        self.client.conversations_replies.return_value = {"messages": [
            {"ts": "5.0", "user": BOB, "text": "starting deploy"},
            {"ts": "5.5", "user": "UCAROL", "text": "deploy broke"},
            {"ts": "6.0", "user": BOB, "text": "rolling back"}]}
        url = "https://x.slack.com/archives/CSRC/p5500000?thread_ts=5.0&cid=CSRC"
        out = self.r.render(self.forward(from_url=url))
        self.client.conversations_replies.assert_called_once_with(channel="CSRC", ts="5.0", limit=200)
        self.assertIn("[thread of the forwarded message in <#CSRC>, 3 messages]", out)
        self.assertIn("starting deploy", out)
        self.assertIn("[5.5] <@UCAROL> (the forwarded message): deploy broke", out)
        self.assertIn("rolling back", out)

    def test_forwarded_thread_parent_brings_its_replies(self):
        self.client.conversations_replies.return_value = {"messages": [
            {"ts": "5.5", "user": "UCAROL", "text": "deploy broke"}, {"ts": "6.0", "user": BOB, "text": "on it"}]}
        out = self.r.render(self.forward())
        self.client.conversations_replies.assert_called_once_with(channel="CSRC", ts="5.5", limit=200)
        self.assertIn("on it", out)

    def test_forwarded_lone_message_adds_nothing(self):
        self.client.conversations_replies.return_value = {"messages": [{"ts": "5.5", "user": "UCAROL", "text": "deploy broke"}]}
        self.assertNotIn("thread of the forwarded message", self.r.render(self.forward()))

    def test_unreadable_source_thread_is_reported(self):
        self.client.conversations_replies.side_effect = SlackApiError("no", {"error": "not_in_channel"})
        out = self.r.render(self.forward(thread_ts="5.0"))
        self.assertIn("could not read it (not_in_channel)", out)
        self.assertIn("[forwarded/shared message from Carol]: deploy broke", out)

    def test_forwards_inside_the_source_thread_are_not_followed(self):
        nested = {"ts": "6.0", "user": BOB, "text": "fyi",
                  "attachments": [{"text": "older", "channel_id": "COTHER", "ts": "1.0"}]}
        self.client.conversations_replies.return_value = {"messages": [{"ts": "5.5", "user": "UCAROL", "text": "x"}, nested]}
        out = self.r.render(self.forward())
        self.assertEqual(self.client.conversations_replies.call_count, 1)
        self.assertIn("[forwarded/shared message from ]: older", out)


class EmojiTest(RelayTest):
    def pick(self, stdout):
        with mock.patch.object(self.r, "custom_emoji", return_value=[]), \
                mock.patch.object(self.r.subprocess, "run", return_value=mock.Mock(stdout=stdout)):
            return self.r.pick_emoji("Halloween is coming")

    def test_uses_the_emoji_haiku_picks(self):
        self.assertEqual(self.pick('{"result": ":tada:"}'), "tada")

    def test_falls_back_to_eyes_on_a_bad_answer(self):
        self.assertEqual(self.pick('{"result": "I would pick a party emoji!"}'), "eyes")
        self.assertEqual(self.pick("not json"), "eyes")

    def test_custom_emoji_are_offered(self):
        with mock.patch.object(self.r, "custom_emoji", return_value=["visto", "ghost-slackbot"]):
            prompt = self.r.emoji_prompt("hi")
        self.assertIn("ghost-slackbot", prompt)
        self.assertTrue(prompt.endswith("hi"))

    def test_unknown_emoji_falls_back_to_eyes(self):
        self.client.reactions_add.side_effect = [SlackApiError("bad", {"error": "invalid_name"}), {"ok": True}]
        def run_now(target, daemon):
            return mock.Mock(start=target)

        with mock.patch.object(self.r, "pick_emoji", return_value="not_an_emoji"), \
                mock.patch.object(self.r.threading, "Thread", side_effect=run_now):
            self.r.ack("C1", "1.0", "hi")
        names = [call.kwargs["name"] for call in self.client.reactions_add.call_args_list]
        self.assertEqual(names, ["not_an_emoji", "eyes"])


class StatusTest(RelayTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.multiple(self.r.config, STATUS="is working on it...", LOADING_MESSAGES=["Warming up"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.r.loading_messages.clear()
        self.r.status_wake.clear()
        self.calls = []
        self.client.assistant_threads_setStatus.side_effect = (
            lambda **kw: self.calls.append(("status", kw["status"], kw["loading_messages"])))
        self.client.chat_postMessage.side_effect = lambda **kw: self.calls.append(("post", kw["text"]))

    def wait_for(self, condition):
        for _ in range(200):
            if condition():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def work(self, run=None):
        with mock.patch.object(self.r, "thread_prompt", return_value=("prompt", "3.0")), \
                mock.patch.object(self.r.claude_cli, "run", side_effect=run or (lambda key, prompt: ("done!", False))), \
                mock.patch.object(self.r.claude_cli, "update"):
            self.r.work("C1", "1.0", [("1.0", ALICE)])

    def test_status_shows_while_working_and_stops_before_the_answer(self):
        def run(key, prompt):
            self.wait_for(lambda: self.calls)
            return "done!", False
        self.work(run)
        self.assertEqual(self.calls, [("status", "is working on it...", ["Warming up"]), ("post", "done!"),
                                      ("status", "", None)])
        kwargs = self.client.assistant_threads_setStatus.call_args.kwargs
        self.assertEqual((kwargs["channel_id"], kwargs["thread_ts"]), ("C1", "1.0"))
        self.assertNotIn("1.0", self.r.status_wake)

    def test_haiku_lines_replace_the_fallback_right_away(self):
        with mock.patch.object(self.r, "pick_loading_messages", return_value=["Reading the thread", "Fixing it"]), \
                mock.patch.object(self.r, "STATUS_REFRESH_SECONDS", 30):
            stop = self.r.show_working("C1", "1.0")
            self.wait_for(lambda: self.calls)
            self.r.update_loading("1.0", "fix the bug")
            self.wait_for(lambda: len(self.calls) == 2)
            stop()
        self.assertEqual([c[2] for c in self.calls], [["Warming up"], ["Reading the thread", "Fixing it"]])

    def test_lines_ready_after_the_answer_do_not_bring_the_status_back(self):
        self.work()
        with mock.patch.object(self.r, "pick_loading_messages", return_value=["Late"]):
            self.r.update_loading("1.0", "hi")
            self.wait_for(lambda: "1.0" in self.r.loading_messages)
        self.assertEqual([c[1] for c in self.calls if c[0] == "status"], ["is working on it...", ""])

    def test_status_off_never_calls_slack(self):
        with mock.patch.object(self.r.config, "STATUS", ""), \
                mock.patch.object(self.r, "pick_loading_messages") as pick:
            self.r.update_loading("1.0", "hi")
            self.work()
        pick.assert_not_called()
        self.client.assistant_threads_setStatus.assert_not_called()
        self.assertEqual(self.calls, [("post", "done!")])

    def test_failed_post_clears_the_status(self):
        self.client.chat_postMessage.side_effect = SlackApiError("bad", {"error": "channel_not_found"})
        with mock.patch.object(self.r, "reply_done") as reply_done:
            self.work()
        reply_done.assert_called_once_with("1.0")
        self.assertEqual(self.client.assistant_threads_setStatus.call_args.kwargs["status"], "")

    def test_stopped_run_says_who_stopped_it_and_clears_the_status(self):
        self.r.busy["1.0"] = {"since": time.time(), "channel": "C1", "pending": [], "stopped_by": BOB}
        self.addCleanup(self.r.busy.clear)

        def run(key, prompt):
            raise self.r.claude_cli.Stopped()
        self.work(run)
        self.assertEqual(self.calls[1:], [("post", f"Stopped by <@{BOB}>."), ("status", "", None)])

    def test_unsupported_channel_stops_refreshing(self):
        self.client.assistant_threads_setStatus.side_effect = SlackApiError(
            "bad", {"error": "method_not_supported_for_channel_type"})
        stop = self.r.show_working("C1", "1.0")
        self.wait_for(lambda: self.client.assistant_threads_setStatus.call_count)
        stop()
        self.assertEqual(self.client.assistant_threads_setStatus.call_count, 1)


class LoadingMessagesTest(RelayTest):
    def pick(self, result):
        with mock.patch.object(self.r, "haiku", return_value=result):
            return self.r.pick_loading_messages("arreglá el bug del login")

    def test_parses_a_json_array(self):
        self.assertEqual(self.pick('["Leyendo el hilo", "Buscando el bug"]'), ["Leyendo el hilo", "Buscando el bug"])

    def test_strips_code_fences_and_caps_lines(self):
        lines = self.pick("```json\n" + str(["x" * 80] * 12).replace("'", '"') + "\n```")
        self.assertEqual(len(lines), 10)
        self.assertEqual(len(lines[0]), 60)

    def test_bad_answer_gives_none(self):
        self.assertIsNone(self.pick("Sure! Here are some lines"))
        self.assertIsNone(self.pick("[]"))


if __name__ == "__main__":
    unittest.main()


class StopTest(RelayTest):
    def setUp(self):
        super().setUp()
        self.stop = mock.patch.object(self.r.claude_cli, "stop").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.r.busy.clear)
        self.r.busy["1.0"] = {"since": time.time(), "channel": "C1", "pending": [("2.0", BOB)]}

    def test_stop_kills_the_run_and_drops_its_queue(self):
        self.r.on_session_stopped({"channel": "C1", "thread_ts": "1.0", "user": ALICE})
        self.stop.assert_called_once_with("C1:1.0")
        self.assertEqual(self.r.busy["1.0"]["pending"], [])
        self.assertEqual(self.r.busy["1.0"]["stopped_by"], ALICE)

    def test_outsiders_cannot_stop(self):
        self.r.on_session_stopped({"channel": "C1", "thread_ts": "1.0", "user": "UOUTSIDER"})
        self.stop.assert_not_called()
        self.assertEqual(self.r.busy["1.0"]["pending"], [("2.0", BOB)])

    def test_stop_without_a_run_clears_the_status(self):
        with mock.patch.object(self.r.config, "STATUS", "is working on it..."):
            self.r.on_session_stopped({"channel": "C1", "thread_ts": "9.0", "user": ALICE})
        self.stop.assert_not_called()
        self.assertEqual(self.client.assistant_threads_setStatus.call_args.kwargs["status"], "")


class ThreadMessagesTest(RelayTest):
    def test_follows_every_page_and_skips_the_repeated_parent(self):
        parent = {"ts": "1.0", "text": "parent"}
        self.client.conversations_replies.side_effect = [
            {"messages": [parent, {"ts": "2.0"}], "response_metadata": {"next_cursor": "c2"}},
            {"messages": [parent, {"ts": "3.0"}], "response_metadata": {"next_cursor": ""}},
        ]
        self.assertEqual([m["ts"] for m in self.r.thread_messages("C1", "1.0")], ["1.0", "2.0", "3.0"])
        self.assertEqual(self.client.conversations_replies.call_args.kwargs["cursor"], "c2")


class RunsFileTest(RelayTest):
    def setUp(self):
        super().setUp()
        self.start = mock.patch.object(self.r, "start").start()
        mock.patch.object(self.r, "ack").start()
        mock.patch.object(self.r, "update_loading").start()
        self.addCleanup(mock.patch.stopall)

    def saved(self):
        return json.loads(self.r.RUNS_FILE.read_text())

    def test_busy_threads_are_mirrored_to_disk(self):
        self.r.dispatch({"channel": "C1", "ts": "1.0", "user": ALICE})
        self.r.dispatch({"channel": "C1", "ts": "2.0", "thread_ts": "1.0", "user": BOB})
        self.assertEqual(self.saved()["1.0"]["requests"], [["1.0", ALICE]])
        self.assertEqual(self.saved()["1.0"]["pending"], [["2.0", BOB]])
        self.r.busy["1.0"]["seen_until"] = 5.0
        self.r.reply_done("1.0")
        self.assertEqual(self.saved(), {})

    def test_restart_resumes_runs_and_their_queues(self):
        self.r.RUNS_FILE.write_text(json.dumps({
            "1.0": {"since": time.time(), "channel": "C1", "requests": [["1.0", ALICE]], "pending": [["2.0", BOB]]},
            "5.0": {"since": time.time(), "channel": "C1", "requests": [["5.0", ALICE]], "pending": [],
                    "answered": True},
            "9.0": {"since": time.time() - self.r.BUSY_TIMEOUT_SECONDS - 1, "channel": "C1",
                    "requests": [["9.0", ALICE]], "pending": []},
        }))
        self.r.resume_runs()
        self.start.assert_called_once_with("C1", "1.0", [("1.0", ALICE), ("2.0", BOB)])
        self.assertTrue(self.r.busy["1.0"]["resumed"])
        self.assertEqual(list(self.saved()), ["1.0"])

    def test_resumed_run_is_told_it_was_cut_off(self):
        self.client.conversations_replies.return_value = {"messages": [{"ts": "1.0", "user": ALICE, "text": "hi"}]}
        with mock.patch.object(self.r.claude_cli, "get", return_value={}):
            prompt, _ = self.r.thread_prompt("C1", "1.0", [("1.0", ALICE)], "C1:1.0", resumed=True)
        self.assertIn(self.r.RESUMED_NOTE, prompt)


class RunSlotsTest(RelayTest):
    def test_runs_past_the_cap_wait_for_a_free_slot(self):
        slots = mock.patch.object(self.r, "run_slots", threading.BoundedSemaphore(1)).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.r, "show_working", return_value=lambda: None).start()
        mock.patch.object(self.r, "post").start()
        release, running, seen = threading.Event(), [], []

        def session(*args):
            running.append(1)
            seen.append(len(running))
            release.wait(5)
            running.pop()
            return "ok"
        mock.patch.object(self.r, "run_session", side_effect=session).start()
        threads = [threading.Thread(target=self.r.work, args=("C1", ts, [(ts, ALICE)])) for ts in ("1.0", "2.0")]
        for t in threads:
            t.start()
        time.sleep(0.2)
        self.assertEqual(seen, [1])
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(seen, [1, 1])
        self.assertTrue(slots.acquire(blocking=False))


class PruneFilesTest(RelayTest):
    def test_old_files_are_deleted_and_new_ones_kept(self):
        self.r.FILES_DIR.mkdir()
        old, new = self.r.FILES_DIR / "old.png", self.r.FILES_DIR / "new.png"
        old.write_text("x")
        new.write_text("x")
        long_ago = time.time() - 15 * 86400
        os.utime(old, (long_ago, long_ago))
        with mock.patch.object(self.r.config, "FILES_KEEP_DAYS", 14):
            self.r.prune_files()
        self.assertEqual(sorted(p.name for p in self.r.FILES_DIR.iterdir()), ["new.png"])

    def test_zero_keeps_everything(self):
        self.r.FILES_DIR.mkdir()
        old = self.r.FILES_DIR / "old.png"
        old.write_text("x")
        os.utime(old, (0, 0))
        with mock.patch.object(self.r.config, "FILES_KEEP_DAYS", 0):
            self.r.prune_files()
        self.assertTrue(old.exists())
