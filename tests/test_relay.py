import time
import unittest
from unittest import mock

from slack_sdk.errors import SlackApiError

from tests.helpers import RELAY_USER_ID, load_relay

ALICE, BOB, OUTSIDER = "UALICE", "UBOB", "UOUTSIDER"
CLAUDE_TAG = "UCLAUDE"
ALERTS, SENTRY = "CALERTS", "USENTRY"


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.r = load_relay()
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
        run_now = lambda target, daemon: mock.Mock(start=target)
        with mock.patch.object(self.r, "pick_emoji", return_value="not_an_emoji"), \
                mock.patch.object(self.r.threading, "Thread", side_effect=run_now):
            self.r.ack("C1", "1.0", "hi")
        names = [call.kwargs["name"] for call in self.client.reactions_add.call_args_list]
        self.assertEqual(names, ["not_an_emoji", "eyes"])


if __name__ == "__main__":
    unittest.main()
