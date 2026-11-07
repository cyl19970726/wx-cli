"""Compiled metadata-only reply completion; synthetic journals, no sender."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest

from scripts.test_local_channel import fixture, GROUP

BINARY = Path(__file__).resolve().parents[1] / "target/debug/wx-cli"
CID = "a" * 32


class ObservedReplyCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli required")

    def call(self, section, state, *args, okay=True, data=None):
        p = subprocess.run([str(BINARY), section, "--state-dir", str(state), *map(str, args)],
                           input=data, text=True, capture_output=True, timeout=15)
        if okay:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return p.stderr

    def co(self, *args, **kwargs):
        return self.call("coordinator", self.ui, *args, **kwargs)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-observed-reply-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state, self.source, self.ui = [self.root / x for x in ("state", "source", "ui")]
        self.ui.mkdir(mode=0o700)
        fixture(self.source)
        self.binding = self.call("channel", self.state, "bind", "--account", "synthetic",
                                 "--talker", GROUP, "--session", "synthetic-session", "--source", self.source)
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.call("channel", self.state, "sync")
        self.co("bind", "--conversation-id", CID, "--channel-state-dir", self.state,
                "--owner", "synthetic-owner", "--input", "push", "--reply-policy", "authorized")
        event = {"sort_seq": 100, "server_id": 900, "msg_type": 1, "sub_type": 0,
                 "sender": "synthetic_member", "talker": GROUP, "create_time": int(time.time()),
                 "content": {"Text": "Question to the agent?"}, "status": 0}
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("INSERT INTO channel_events(kind,server_id,message_json) VALUES('message_observed',900,?)",
                       (json.dumps(event),))
        self.co("inbox", "reconcile", "--conversation-id", CID)
        self.claim = self.co("inbox", "claim", "--conversation-id", CID, "--owner", "synthetic-owner", "--expected-epoch", 1)
        self.delivery = self.claim["delivery_id"]
        self.rid = "app_a.reply-" + CID + "-" + str(self.delivery)
        self.co("inbox", "reserve-turn", "--delivery-id", self.delivery, "--token", self.claim["token"],
                "--session-ref", "codex:synthetic:original")
        self.co("inbox", "record-turn", "--delivery-id", self.delivery, "--token", self.claim["token"],
                "--thread-id", "original", "--turn-id", "original-turn")
        self.call("outbox", self.state, "queue", "--request-id", self.rid, "--channel-id", self.binding["channel_id"],
                  "--session-id", "synthetic-session", "--owner", "synthetic-owner", "--expected-epoch", 1,
                  "--publisher", "app_a", "--text-stdin", data="A verified answer.")
        self.out = self.state / "outgoing.sqlite3"
        self.positive()

    def positive(self, status="ui_observed"):
        now = int(time.time())
        with sqlite3.connect(self.out) as db:
            db.execute("UPDATE managed_control SET gate=1")
            db.execute("UPDATE outgoing_intent SET status=?,attempt_number=1,result_evidence='synthetic-native-proof'", (status,))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES(?,1,'ui_observed','synthetic-native-proof',?,?)",
                       (self.rid, now, now))
            db.execute("UPDATE managed_control SET gate=0")

    def complete(self, okay=True, publisher="app_a", rid=None, token=None):
        return self.co("inbox", "complete-observed-reply", "--delivery-id", self.delivery,
                       "--token", token or self.claim["token"], "--publisher", publisher,
                       "--request-id", rid or self.rid, "--reason", "Verified original outgoing answer", okay=okay)

    def attempt_rows(self):
        with sqlite3.connect(self.out) as db:
            return db.execute("SELECT * FROM outgoing_attempt").fetchall()

    def test_positive_completion_never_queues_or_changes_an_attempt(self):
        before = self.attempt_rows()
        result = self.complete()
        self.assertEqual(result["status"], "replied")
        shown = self.co("inbox", "show", "--delivery-id", self.delivery, "--owner", "synthetic-owner")
        self.assertEqual(shown["request_id"], self.rid)
        self.assertEqual(shown["turn_state"], "recorded")
        self.assertEqual(self.attempt_rows(), before)
        self.assertEqual(self.co("dispatch", "status")["dispatches"], [])
        self.co("inbox", "finish-turn", "--delivery-id", self.delivery,
                "--token", self.claim["token"], "--status", "completed")

    def test_existing_complete_still_refuses_an_already_attempted_intent(self):
        self.co("inbox", "complete", "--delivery-id", self.delivery, "--token", self.claim["token"],
                "--decision", "reply", "--request-id", self.rid, okay=False)
        self.assertEqual(self.complete()["status"], "replied")

    def test_other_publisher_or_other_delivery_key_refuses(self):
        self.complete(publisher="app_b", okay=False)
        self.complete(rid=self.rid + "-different", okay=False)
        self.complete(rid="app_a.reply-" + CID + "-999", okay=False)

    def test_pending_unknown_and_unfinished_attempts_are_not_positive(self):
        for status in ("managed_queued", "attempt_recorded", "indeterminate", "failed"):
            with sqlite3.connect(self.out) as db:
                db.execute("UPDATE managed_control SET gate=1")
                db.execute("UPDATE outgoing_intent SET status=?", (status,))
                db.execute("UPDATE managed_control SET gate=0")
            self.complete(okay=False)

    def test_indeterminate_attempt_requires_an_immutable_late_observation(self):
        with sqlite3.connect(self.out) as db:
            db.execute("UPDATE managed_control SET gate=1")
            db.execute("UPDATE outgoing_attempt SET status='indeterminate'")
            db.execute("UPDATE managed_control SET gate=0")
        self.complete(okay=False)

    def test_second_attempt_and_wrong_token_refuse(self):
        self.complete(token="another-lease", okay=False)
        with sqlite3.connect(self.out) as db:
            db.execute("UPDATE managed_control SET gate=1")
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES(?,2,'ui_observed','different',1,2)", (self.rid,))
            db.execute("UPDATE managed_control SET gate=0")
        self.complete(okay=False)

    def test_generation_change_retains_pending_without_adopting_reply(self):
        self.co("configure", "--conversation-id", CID, "--owner", "synthetic-owner", "--expected-epoch", 1,
                "--input", "push", "--reply-policy", "authorized")
        self.complete(okay=False)


if __name__ == "__main__":
    unittest.main()
