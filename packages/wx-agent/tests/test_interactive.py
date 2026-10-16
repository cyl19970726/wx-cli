"""Synthetic manual handoff tests; no desktop or WeChat process is used."""
import contextlib
import io
import json
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wx_agent import channel_agent as runner
from wx_agent import interactive as chat
from wx_agent import wechat_agent_ctl as ctl


def event(cursor, server_id, direction="incoming", kind="message_observed", text="hello"):
    return {"cursor": cursor, "server_id": server_id, "kind": kind, "direction": direction,
            "channel_id": "test-channel", "session_id": "test-session", "account": "test-account",
            "talker": "test@chatroom", "message": {"server_id": server_id, "talker": "test@chatroom",
                                             "sender": "member", "content": {"Text": text}}}


class FakeCommands:
    def __init__(self, events):
        self.binding = {"channel_id": "test-channel", "session_id": "test-session",
                        "account": "test-account", "talker": "test@chatroom",
                        "source": "/synthetic/test-account", "encrypted_snapshot": True}
        self.events = events
        self.cursor = 0
        self.outbox = {}
        self.acks = []
        self.sends = 0
        self.send_status = "ui_observed"
        self.fail_ack = False

    def __call__(self, _settings, section, *parts, data=None):
        action = parts[0]
        if section == "channel":
            if action == "status":
                return json.dumps({"binding": self.binding, "consumer_cursor": self.cursor,
                                   "latest_cursor": max([e["cursor"] for e in self.events], default=0)})
            if action == "sync":
                return '{"events_appended":0}'
            if action == "events":
                after = int(parts[parts.index("--after") + 1])
                limit = int(parts[parts.index("--limit") + 1])
                return "\n".join(json.dumps(e) for e in [e for e in self.events if e["cursor"] > after][:limit])
            if action == "ack":
                expected = int(parts[parts.index("--expected-cursor") + 1])
                cursor = int(parts[parts.index("--cursor") + 1])
                self.acks.append((expected, cursor))
                if self.fail_ack or expected != self.cursor:
                    raise chat.ChatError("command_failed", "CAS failed")
                self.cursor = cursor
                return json.dumps({"cursor": cursor})
        if section == "outbox":
            if action == "list":
                return json.dumps([{"request_id": key, "status": value["status"]}
                                   for key, value in self.outbox.items()])
            rid = parts[parts.index("--request-id") + 1]
            if action == "queue":
                self.outbox.setdefault(rid, {"request_id": rid, "text": data, "status": "queued"})
                return json.dumps({"request_id": rid, "status": self.outbox[rid]["status"]})
            if action == "show":
                return json.dumps(self.outbox[rid])
            if action == "send":
                self.sends += 1
                self.outbox[rid]["status"] = self.send_status
                return json.dumps({"request_id": rid, "status": self.send_status})
            if action == "recover-unsent":
                expected = int(parts[parts.index("--expected-attempt") + 1])
                if self.outbox[rid].get("attempt_number") != expected:
                    raise chat.ChatError("command_failed", "stale attempt")
                self.outbox[rid]["attempt_number"] = expected + 1
                self.outbox[rid]["status"] = self.send_status
                return json.dumps({"request_id": rid, "attempt_number": expected + 1,
                                   "status": self.send_status})
        raise AssertionError((section, parts))


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        self.settings = {"wx_cli": "/synthetic/wx-cli", "state_dir": str(self.state),
                         "consumer": "agent", "agent_command": ["/synthetic/agent"],
                         "sender_driver": "/synthetic/sender"}
        self.fake = FakeCommands([event(1, 101)])
        self.patch = mock.patch.object(chat, "_command", side_effect=self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.control = mock.patch.object(chat.ctl, "status", return_value={"alive": False})
        self.control.start()
        self.addCleanup(self.control.stop)
        self.runner_state = {"version": 1, "binding": self.fake.binding,
                             "consumer": "agent", "agent_command": self.settings["agent_command"],
                             "sender_driver": self.settings["sender_driver"], "started": True,
                             "start_after": 0, "last_cursor": 0, "pending": None}
        runner.save(self.state / runner.STATE_NAME, self.runner_state)

    def selected(self):
        chat.select(self.settings, "test@chatroom")

    def taken(self):
        self.selected()
        return chat.takeover(self.settings)

    def test_selection_binding_drift_and_read_pagination_without_ack(self):
        with self.assertRaisesRegex(chat.ChatError, "only the configured group"):
            chat.select(self.settings, "other@chatroom")
        self.fake.events = [event(i, i + 100) for i in range(1, 4)]
        self.selected()
        first = chat.read(self.settings, limit=2)
        self.assertEqual([e["cursor"] for e in first["events"]], [1, 2])
        self.assertTrue(first["has_more"])
        self.assertFalse(first["history_complete"])
        self.assertEqual(self.fake.cursor, 0)
        second = chat.read(self.settings, after=first["next_cursor"], limit=2)
        self.assertEqual([e["cursor"] for e in second["events"]], [3])
        self.fake.binding["talker"] = "changed@chatroom"
        with self.assertRaisesRegex(chat.ChatError, "selection or config changed"):
            chat.read(self.settings)

    def test_group_discovery_uses_bound_account_and_source(self):
        search = {"matches": [{"name": "Synthetic", "talker": "test@chatroom"},
                              {"name": "Other", "talker": "other@chatroom"}], "match_kind": "partial"}
        with mock.patch.object(chat.runner, "command", return_value=json.dumps(search)) as call:
            result = chat.groups(self.settings, "Synthetic", exact=True)
        self.assertEqual(result["matches"], search["matches"])
        self.assertEqual(call.call_args.args[0], [self.settings["wx_cli"], "groups", "find",
                       "--state-dir", str(self.state), "--account", "test-account",
                       "--source", "/synthetic/test-account", "--name", "Synthetic", "--exact"])

    def test_takeover_waits_for_verified_stop_and_rejects_runner_pending(self):
        self.selected()
        with mock.patch.object(chat.ctl, "status", return_value={"alive": True}):
            with self.assertRaisesRegex(chat.ChatError, "has not exited"):
                chat.takeover(self.settings, wait_seconds=0)
        self.assertFalse((self.state / chat.LEASE_NAME).exists())
        self.runner_state["pending"] = {"text": "saved"}
        runner.save(self.state / runner.STATE_NAME, self.runner_state)
        with self.assertRaisesRegex(chat.ChatError, "pending reply"):
            chat.takeover(self.settings)
        self.assertFalse((self.state / chat.LEASE_NAME).exists())

    def test_historical_uncertain_allowed_but_unprocessed_source_rejected(self):
        self.selected()
        old = chat._request_id(self.fake.binding, 90)
        self.fake.outbox[old] = {"request_id": old, "text": "old", "status": "indeterminate"}
        result = chat.takeover(self.settings)
        self.assertEqual(result["historical_uncertain_count"], 1)
        self.assertEqual(chat.status(self.settings)["historical_uncertain_count"], 1)
        (self.state / chat.LEASE_NAME).unlink()
        current = chat._request_id(self.fake.binding, 101)
        self.fake.outbox[current] = {"request_id": current, "text": "current", "status": "indeterminate"}
        with self.assertRaisesRegex(chat.ChatError, "unresolved request"):
            chat.takeover(self.settings)

    def test_queued_outbox_and_runner_lock_prevent_takeover(self):
        self.selected()
        request_id = chat._request_id(self.fake.binding, 90)
        self.fake.outbox[request_id] = {"request_id": request_id, "text": "old", "status": "queued"}
        with self.assertRaisesRegex(chat.ChatError, "unresolved request"):
            chat.takeover(self.settings)
        self.fake.outbox.clear()
        lock_path = self.state / runner.LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(chat.ChatError, "is busy"):
                chat.takeover(self.settings)
        finally:
            os.close(fd)

    def test_reply_order_idempotence_conflict_and_cas(self):
        self.fake.events = [event(1, 101), event(2, 102)]
        self.taken()
        with self.assertRaisesRegex(chat.ChatError, "earlier incoming"):
            chat.reply(self.settings, 2, "second")
        done = chat.reply(self.settings, 1, "first")
        self.assertEqual(done["status"], "ui_observed")
        self.assertEqual(self.fake.sends, 1)
        self.assertEqual(self.fake.acks, [(0, 1)])
        self.assertEqual(runner.load(self.state / runner.STATE_NAME)["last_cursor"], 1)
        again = chat.reply(self.settings, 1, "first")
        self.assertTrue(again["already_completed"])
        self.assertEqual(self.fake.sends, 1)
        with self.assertRaisesRegex(chat.ChatError, "different text") as conflict:
            chat.reply(self.settings, 1, "different")
        self.assertEqual(conflict.exception.details,
                         {"request_id": done["request_id"], "status": "ui_observed"})

    def test_recover_unsent_reply_uses_retained_intent_and_acks_only_terminal_result(self):
        self.taken()
        request_id = chat._request_id(self.fake.binding, 101)
        lease_path = self.state / chat.LEASE_NAME
        lease = runner.load(lease_path)
        lease["pending"] = {"cursor": 1, "previous_cursor": 0, "request_id": request_id,
                            "text": "same original reply", "phase": "queued"}
        runner.save(lease_path, lease)
        self.fake.outbox[request_id] = {"request_id": request_id, "text": "same original reply",
                                        "status": "indeterminate", "attempt_number": 1}
        self.fake.send_status = "ui_observed"
        result = chat.recover_unsent_reply(self.settings, 1, expected_attempt=1)
        self.assertEqual(result["status"], "ui_observed")
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(self.fake.acks, [(0, 1)])
        self.assertEqual(self.fake.sends, 0)
        self.assertEqual(self.fake.outbox[request_id]["attempt_number"], 2)
        self.assertIsNone(runner.load(lease_path)["pending"])

    def test_recover_unsent_reply_rejects_stale_attempt_and_nonterminal_result(self):
        self.taken()
        request_id = chat._request_id(self.fake.binding, 101)
        lease_path = self.state / chat.LEASE_NAME
        lease = runner.load(lease_path)
        lease["pending"] = {"cursor": 1, "previous_cursor": 0, "request_id": request_id,
                            "text": "same original reply", "phase": "queued"}
        runner.save(lease_path, lease)
        self.fake.outbox[request_id] = {"request_id": request_id, "text": "same original reply",
                                        "status": "failed", "attempt_number": 2}
        with self.assertRaisesRegex(chat.ChatError, "differs from the requested recovery"):
            chat.recover_unsent_reply(self.settings, 1, expected_attempt=1)
        self.assertEqual(self.fake.outbox[request_id]["attempt_number"], 2)
        self.assertEqual(self.fake.sends, 0)
        self.fake.outbox[request_id]["attempt_number"] = 1
        self.fake.send_status = "indeterminate"
        with self.assertRaisesRegex(chat.ChatError, "not terminal"):
            chat.recover_unsent_reply(self.settings, 1)
        pending = runner.load(lease_path)["pending"]
        self.assertEqual(pending["request_id"], request_id)
        self.assertEqual(self.fake.acks, [])

    def test_recovery_cli_requires_and_forwards_expected_attempt(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as missing:
            chat.main(["--config", "/synthetic/config.json", "recover-unsent-reply",
                       "--cursor", "1"])
        self.assertEqual(missing.exception.code, 2)
        self.assertIn("--expected-attempt", errors.getvalue())

        output = io.StringIO()
        with mock.patch.object(chat.ctl, "config", return_value=self.settings), \
             mock.patch.object(chat, "recover_unsent_reply", return_value={"status": "ui_observed"}) as recover, \
             contextlib.redirect_stdout(output):
            self.assertEqual(chat.main(["--config", "/synthetic/config.json", "recover-unsent-reply",
                                        "--cursor", "1", "--expected-attempt", "4"]), 0)
        recover.assert_called_once_with(self.settings, 1, 4)
        self.assertEqual(json.loads(output.getvalue()), {"status": "ui_observed"})

    def test_prior_automatic_terminal_reply_is_reported_without_new_send(self):
        self.taken()
        request_id = chat._request_id(self.fake.binding, 101)
        self.fake.outbox[request_id] = {"request_id": request_id, "text": "automatic",
                                        "status": "ui_observed"}
        result = chat.reply(self.settings, 1, "automatic")
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(self.fake.sends, 0)
        self.assertEqual(self.fake.cursor, 1)

    def test_unknown_direction_and_nontext_never_send(self):
        self.taken()
        self.fake.events[0]["direction"] = "unknown"
        with self.assertRaisesRegex(chat.ChatError, "direction is unknown"):
            chat.reply(self.settings, 1, "reply")
        self.assertEqual(self.fake.sends, 0)

    def test_uncertain_send_stays_pending_and_never_retries(self):
        self.taken()
        self.fake.send_status = "indeterminate"
        with self.assertRaisesRegex(chat.ChatError, "manual inspection"):
            chat.reply(self.settings, 1, "reply")
        self.assertEqual(self.fake.sends, 1)
        self.assertIsNotNone(runner.load(self.state / chat.LEASE_NAME)["pending"])
        with self.assertRaisesRegex(chat.ChatError, "manual inspection"):
            chat.reply(self.settings, 1, "reply")
        self.assertEqual(self.fake.sends, 1)
        with self.assertRaisesRegex(chat.ChatError, "manual reply is pending"):
            chat.handback(self.settings)

    def test_ack_failure_and_crash_recovery_keep_pending(self):
        self.taken()
        self.fake.fail_ack = True
        with self.assertRaisesRegex(chat.ChatError, "CAS failed"):
            chat.reply(self.settings, 1, "reply")
        self.assertIsNotNone(runner.load(self.state / chat.LEASE_NAME)["pending"])
        self.assertEqual(self.fake.sends, 1)
        self.fake.fail_ack = False
        self.fake.cursor = 1  # Simulate a committed CAS before the Python state save.
        result = chat.reply(self.settings, 1, "reply")
        self.assertTrue(result["already_completed"])
        self.assertEqual(self.fake.sends, 1)
        self.assertIsNone(runner.load(self.state / chat.LEASE_NAME)["pending"])

    def test_skip_only_next_incoming_without_outbox_or_gui(self):
        self.fake.events = [event(1, 101), event(2, 102)]
        self.taken()
        with self.assertRaisesRegex(chat.ChatError, "earlier incoming"):
            chat.skip(self.settings, 2, "No reply needed")
        result = chat.skip(self.settings, 1, "No reply needed")
        self.assertEqual(result, {"cursor": 1, "reason": "No reply needed",
                                  "status": "skipped", "already_completed": False})
        self.assertEqual(self.fake.acks, [(0, 1)])
        self.assertEqual(self.fake.outbox, {})
        self.assertEqual(self.fake.sends, 0)
        state = runner.load(self.state / runner.STATE_NAME)
        self.assertEqual(state["manual_skips"], [{"cursor": 1, "server_id": 101,
                                                   "reason": "No reply needed"}])
        self.assertEqual(state["last_cursor"], 1)
        self.assertIsNone(runner.load(self.state / chat.LEASE_NAME)["pending"])
        again = chat.skip(self.settings, 1, "No reply needed")
        self.assertTrue(again["already_completed"])
        self.assertEqual(self.fake.acks, [(0, 1)])
        with self.assertRaisesRegex(chat.ChatError, "different reason"):
            chat.skip(self.settings, 1, "Different")
        self.assertEqual(chat.reply(self.settings, 2, "Answer")["status"], "ui_observed")
        self.assertEqual(self.fake.acks, [(0, 1), (1, 2)])

    def test_skip_cas_failure_and_post_ack_recovery(self):
        self.taken()
        self.fake.fail_ack = True
        with self.assertRaisesRegex(chat.ChatError, "CAS failed"):
            chat.skip(self.settings, 1, "Acknowledged")
        pending = runner.load(self.state / chat.LEASE_NAME)["pending"]
        self.assertEqual(pending["kind"], "skip")
        self.assertEqual(self.fake.outbox, {})
        self.assertEqual(self.fake.sends, 0)
        self.fake.fail_ack = False
        self.fake.cursor = 1  # CAS committed, state save did not run.
        done = chat.skip(self.settings, 1, "Acknowledged")
        self.assertTrue(done["already_completed"])
        self.assertEqual(runner.load(self.state / runner.STATE_NAME)["last_cursor"], 1)
        self.assertIsNone(runner.load(self.state / chat.LEASE_NAME)["pending"])
        self.assertEqual(self.fake.acks, [(0, 1)])

    def test_skip_may_ack_prior_outgoing_but_not_prior_incoming(self):
        self.fake.events = [event(1, 101, direction="outgoing"), event(2, 102)]
        self.taken()
        self.assertEqual(chat.skip(self.settings, 2, "No response needed")["status"], "skipped")
        self.assertEqual(self.fake.acks, [(0, 2)])
        self.assertEqual(self.fake.outbox, {})

    def test_skip_cli_dispatch_and_pending_blocks_handback(self):
        self.taken()
        self.fake.fail_ack = True
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(chat.ctl, "config", return_value=self.settings), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(chat.main(["--config", "/synthetic/config.json", "skip",
                                        "--cursor", "1", "--reason", "Not actionable"]), 1)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "command_failed")
        with self.assertRaisesRegex(chat.ChatError, "manual skip is pending"):
            chat.handback(self.settings)
        self.fake.fail_ack = False
        output = io.StringIO()
        with mock.patch.object(chat.ctl, "config", return_value=self.settings), \
             contextlib.redirect_stdout(output):
            self.assertEqual(chat.main(["--config", "/synthetic/config.json", "skip",
                                        "--cursor", "1", "--reason", "Not actionable"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "skipped")

    def test_skip_and_reply_pending_never_override_each_other(self):
        self.taken()
        self.fake.send_status = "indeterminate"
        with self.assertRaisesRegex(chat.ChatError, "manual inspection"):
            chat.reply(self.settings, 1, "Answer")
        saved = runner.load(self.state / chat.LEASE_NAME)["pending"]
        with self.assertRaisesRegex(chat.ChatError, "manual reply is pending"):
            chat.skip(self.settings, 1, "No answer")
        self.assertEqual(runner.load(self.state / chat.LEASE_NAME)["pending"], saved)

    def test_reply_cannot_override_pending_skip(self):
        self.taken()
        self.fake.fail_ack = True
        with self.assertRaisesRegex(chat.ChatError, "CAS failed"):
            chat.skip(self.settings, 1, "No answer")
        saved = runner.load(self.state / chat.LEASE_NAME)["pending"]
        with self.assertRaisesRegex(chat.ChatError, "manual skip is pending"):
            chat.reply(self.settings, 1, "Answer")
        with self.assertRaisesRegex(chat.ChatError, "another manual skip is pending"):
            chat.skip(self.settings, 1, "Changed reason")
        self.assertEqual(runner.load(self.state / chat.LEASE_NAME)["pending"], saved)

    def test_skip_recovers_after_state_save_before_pending_clear(self):
        self.taken()
        chat.skip(self.settings, 1, "Reviewed")
        lease = runner.load(self.state / chat.LEASE_NAME)
        lease["pending"] = {"kind": "skip", "cursor": 1, "previous_cursor": 0,
                            "server_id": 101, "reason": "Reviewed"}
        runner.save(self.state / chat.LEASE_NAME, lease)
        result = chat.skip(self.settings, 1, "Reviewed")
        self.assertTrue(result["already_completed"])
        self.assertEqual(self.fake.acks, [(0, 1)])
        self.assertIsNone(runner.load(self.state / chat.LEASE_NAME)["pending"])

    def test_skip_rejects_existing_reply_and_survives_handback(self):
        self.taken()
        request_id = chat._request_id(self.fake.binding, 101)
        self.fake.outbox[request_id] = {"request_id": request_id, "text": "sent", "status": "ui_observed"}
        with self.assertRaisesRegex(chat.ChatError, "already has an outbox request"):
            chat.skip(self.settings, 1, "No answer")
        self.assertEqual(self.fake.cursor, 0)
        self.fake.outbox.clear()
        chat.skip(self.settings, 1, "No answer")
        self.assertFalse(chat.handback(self.settings)["alive"])
        self.assertEqual(self.fake.cursor, 1)
        self.assertEqual(runner.load(self.state / runner.STATE_NAME)["last_cursor"], 1)
        chat.takeover(self.settings)
        self.assertTrue(chat.skip(self.settings, 1, "No answer")["already_completed"])
        self.assertEqual(self.fake.acks, [(0, 1)])
        self.fake.outbox[request_id] = {"request_id": request_id, "text": "late",
                                        "status": "ui_observed"}
        with self.assertRaisesRegex(chat.ChatError, "already has an outbox request"):
            chat.skip(self.settings, 1, "No answer")

    def test_handback_phase_survives_start_failure_and_blocks_normal_start(self):
        self.taken()
        lease_path = self.state / chat.LEASE_NAME
        lease = runner.load(lease_path)
        lease["previously_alive"] = True
        runner.save(lease_path, lease)
        with self.assertRaisesRegex(ctl.ControlError, "interactive lease"):
            ctl.start(self.settings)
        started = {"ready": False}

        def start(_settings, handoff_token):
            if not started["ready"]:
                started["ready"] = True
                raise ctl.ControlError("start failed")
            runner.save(self.state / runner.HANDOFF_READY_NAME,
                        {"version": 1, "pid": 1234, "handoff_token": handoff_token})
            started["launched"] = True
            return {"started": True, "pid": 1234}

        with mock.patch.object(chat.ctl, "start", side_effect=start), \
             mock.patch.object(chat.ctl, "status", side_effect=lambda _settings: {"alive": started.get("launched", False), "pid": 1234 if started.get("launched", False) else None}):
            with self.assertRaisesRegex(ctl.ControlError, "start failed"):
                chat.handback(self.settings)
            self.assertEqual(runner.load(self.state / chat.LEASE_NAME)["phase"], "handback")
            result = chat.handback(self.settings)
        self.assertTrue(result["handed_back"])
        self.assertFalse((self.state / chat.LEASE_NAME).exists())

    def test_previously_stopped_handback_does_not_start_runner(self):
        self.taken()
        with mock.patch.object(chat.ctl, "start", side_effect=AssertionError("unexpected start")):
            result = chat.handback(self.settings)
        self.assertEqual(result, {"handed_back": True, "recovered": False, "alive": False})
        self.assertFalse((self.state / chat.LEASE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
