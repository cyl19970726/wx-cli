#!/usr/bin/env python3
"""Synthetic two-channel integration against the compiled Rust coordinator."""
import base64
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.test_local_channel import GROUP, TABLE, fixture


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/debug/wx-cli"
PRIVATE = "synthetic_friend"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def private_fixture(root):
    fixture(root)
    table = "Msg_" + hashlib.md5(PRIVATE.encode()).hexdigest()
    with sqlite3.connect(root / "contact/contact.db") as db:
        db.execute("UPDATE contact SET username=?,nick_name=?", (PRIVATE, "Synthetic Friend"))
        db.execute("DELETE FROM chat_room")
    with sqlite3.connect(root / "session/session.db") as db:
        db.execute("UPDATE SessionTable SET username=?", (PRIVATE,))
    with sqlite3.connect(root / "message/message_0.db") as db:
        db.execute(f"ALTER TABLE [{TABLE}] RENAME TO [{table}]")
    return table


class MultiConversationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-multigroup-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ui = self.root / "ui"
        self.ui.mkdir(mode=0o700)
        self.log = self.root / "driver-events.jsonl"
        self.marker = self.root / "driver-active"
        self.channels = {}
        for name, talker in (("group", GROUP), ("private", PRIVATE)):
            source = self.root / (name + "-source")
            state = self.root / (name + "-state")
            table = private_fixture(source) if name == "private" else (fixture(source) or TABLE)
            bind = self.cli("channel", state, "bind", "--account", "synthetic",
                            "--talker", talker, "--session", "session-" + name,
                            "--source", str(source))
            # Decrypted synthetic fixtures lack independent account identity.
            # Seed only that fixture field so inbox direction can be determined.
            with sqlite3.connect(state / "channel.sqlite3") as db:
                db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
            self.cli("channel", state, "sync")
            self.channels[name] = {"source": source, "state": state, "table": table,
                                   "talker": talker, "binding": bind,
                                   "id": "synthetic_" + name}
            enrollment = self.cli("coordinator", self.ui, "bind", "--conversation-id",
                                  "synthetic_" + name, "--channel-state-dir", str(state),
                                  "--owner", "owner_" + name, "--input", "push",
                                  "--reply-policy", "authorized")
            self.channels[name]["epoch"] = enrollment["epoch"]
        self.driver = self.root / "synthetic-driver"
        self.driver.write_text("""#!""" + sys.executable + """
import json, os, pathlib, sys, time
root=pathlib.Path(__file__).resolve().parent.parent
if sys.argv[1:] == ['--dispatch-preflight']:
    os.fstat(int(os.environ['WX_DESKTOP_LOCK_FD']))
    print(json.dumps({'ready':not (root/'deny-preflight').exists()}))
    sys.exit(0)
payload=json.load(sys.stdin)
log=root/'driver-events.jsonl'
marker=root/'driver-active'
def record(phase):
    with log.open('a') as file:
        file.write(json.dumps({'phase':phase,'request_id':payload['request_id'],
            'talker':payload['talker'],'media':bool(payload.get('media'))})+'\\n')
fd=int(os.environ['WX_DESKTOP_LOCK_FD'])
os.fstat(fd)
try:
    marker_fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
except FileExistsError:
    sys.exit(13)
try:
    record('begin')
    time.sleep(0.04)
    record('end')
finally:
    os.close(marker_fd)
    marker.unlink()
status=('indeterminate' if payload['request_id'].startswith('unknown') else
        'failed' if payload['request_id'].startswith('fail') else 'ui_observed')
print(json.dumps({'status':status,'evidence':'synthetic local observation'}))
""")
        self.driver.chmod(0o700)
        for channel in self.channels.values():
            os.link(self.driver, channel["state"] / "conversation-sender")

    def cli(self, section, state, *args, data=None, okay=True):
        command = [str(BINARY), section, "--state-dir", str(state), *map(str, args)]
        process = subprocess.run(command, input=data, text=True, capture_output=True, timeout=20)
        if okay:
            self.assertEqual(process.returncode, 0, f"{command[1:]}: {process.stderr}")
            lines = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            return lines[0]
        self.assertNotEqual(process.returncode, 0, f"{command[1:]} unexpectedly succeeded")
        return process.stderr

    def coordinator(self, *args, **kw):
        return self.cli("coordinator", self.ui, *args, **kw)

    def outbox(self, name, *args, **kw):
        return self.cli("outbox", self.channels[name]["state"], *args, **kw)

    def queue(self, name, request_id, image=False, batch_id=None, part_index=None,
              priority=None, expires_at=None):
        channel = self.channels[name]
        args = ("queue-image", "--image", str(self.root / "synthetic image.png")) if image else (
            "queue", "--text-stdin")
        if image:
            path = self.root / "synthetic image.png"
            path.write_bytes(PNG)
        schedule = []
        for key, value in (("batch-id", batch_id), ("part-index", part_index),
                           ("priority", priority), ("expires-at", expires_at)):
            if value is not None:
                schedule.extend(("--" + key, value))
        self.outbox(name, args[0], "--request-id", request_id,
                    "--channel-id", channel["binding"]["channel_id"],
                    "--session-id", channel["binding"]["session_id"],
                    "--owner", "owner_" + name, "--expected-epoch", channel["epoch"],
                    *args[1:], *schedule,
                    data=None if image else "synthetic reply")
        return self.coordinator("enqueue", "--conversation-id", channel["id"],
                                "--request-id", request_id, "--expected-epoch", channel["epoch"])

    def observe_self_source(self, name="group", server_id=140):
        channel = self.channels[name]
        with sqlite3.connect(channel["source"] / "message/message_0.db") as db:
            db.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (2,'synthetic_self')")
            db.execute(
                f"INSERT INTO [{channel['table']}] VALUES (40, ?, 1, 2, 1700000040, ?, NULL, 0)",
                (server_id, b"synthetic_self:\noutgoing"),
            )
        self.cli("channel", channel["state"], "sync")
        return 4

    def test_concurrent_mixed_queue_global_fifo_and_desktop_exclusion(self):
        from wx_agent import dispatch
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(self.queue, name, request, image)
                    for name, request, image in (("group", "group-text-a", False),
                                                 ("private", "private-image", True),
                                                 ("group", "group-text-b", False))]
            enqueued = [job.result() for job in jobs]
        order = [item["request_id"] for item in sorted(enqueued, key=lambda x: x["dispatch_id"])]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: dispatch.drain(BINARY, self.ui), range(2)))
        phases = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([item["request_id"] for item in phases if item["phase"] == "begin"], order)
        self.assertEqual([item["phase"] for item in phases], ["begin", "end"] * 3)
        self.assertEqual(sum(len(result["results"]) for result in results), 3)
        self.assertTrue(any(item["media"] for item in phases))
        for name, request in (("group", "group-text-a"), ("private", "private-image"),
                              ("group", "group-text-b")):
            item = self.outbox(name, "show", "--request-id", request)
            self.assertEqual(item["status"], "ui_observed")
            self.assertEqual(item["attempt_number"], 1)

    def test_busy_lock_gap_reconcile_unknown_and_review(self):
        from wx_agent import dispatch
        first = self.queue("group", "group-busy")
        fd = os.open(self.ui / "sender.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            dispatch.drain(BINARY, self.ui)
            self.assertEqual(self.outbox("group", "show", "--request-id", "group-busy")["attempt_number"], 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.coordinator("reconcile-outbox", "--conversation-id", "synthetic_group")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-busy")["attempt_number"], 1)
        # Simulate the crash gap: outbox intent committed but dispatch was not.
        channel = self.channels["private"]
        self.outbox("private", "queue", "--request-id", "private-gap",
                    "--channel-id", channel["binding"]["channel_id"],
                    "--session-id", channel["binding"]["session_id"],
                    "--owner", "owner_private", "--expected-epoch", channel["epoch"],
                    "--text-stdin", data="synthetic gap")
        rebuilt = self.coordinator("reconcile-outbox", "--conversation-id", "synthetic_private")
        self.assertEqual(rebuilt["created"], 1)
        self.queue("group", "unknown-group")
        self.queue("private", "private-after-unknown")
        before = len(self.log.read_text().splitlines())
        dispatch.drain(BINARY, self.ui)
        status = self.coordinator("dispatch", "status")
        self.assertIsNotNone(status["paused_reason"])
        self.assertEqual(self.outbox("group", "show", "--request-id", "unknown-group")["attempt_number"], 1)
        self.assertEqual(self.outbox("private", "show", "--request-id", "private-after-unknown")["attempt_number"], 0)
        self.coordinator("dispatch", "resume", okay=False)
        self.coordinator("dispatch", "review", "--reason", "synthetic independent review completed")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "unknown-group")["attempt_number"], 1)
        self.assertEqual(self.outbox("private", "show", "--request-id", "private-after-unknown")["attempt_number"], 1)
        self.assertGreater(len(self.log.read_text().splitlines()), before)

    def test_preflight_refusal_pauses_without_attempt(self):
        from wx_agent import dispatch
        self.queue("group", "group-not-ready")
        (self.root / "deny-preflight").write_text("synthetic refusal")
        dispatch.drain(BINARY, self.ui)
        self.assertFalse(self.log.exists(), "preflight must not run the effect driver")
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-not-ready")
                         ["attempt_number"], 0)
        self.assertIsNotNone(self.coordinator("dispatch", "status")["paused_reason"])
        (self.root / "deny-preflight").unlink()
        self.coordinator("dispatch", "review", "--reason", "synthetic preflight conditions inspected")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-not-ready")
                         ["attempt_number"], 1)

    def test_restore_queued_requires_review_and_zero_attempt(self):
        from wx_agent import dispatch
        queued = self.queue("group", "group-restore-unattempted")
        (self.root / "deny-preflight").write_text("synthetic refusal")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-restore-unattempted")
                         ["attempt_number"], 0)
        # Fixture for a crashed worker that left coordinator metadata unknown
        # before the sender recorded any attempt.
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE dispatch SET status='unknown',result='synthetic_abandoned_claim' "
                       "WHERE id=?", (queued["dispatch_id"],))
        self.coordinator("dispatch", "restore-queued", "--dispatch-id", queued["dispatch_id"],
                         "--reason", "reviewed synthetic helper refusal")
        self.assertEqual(self.coordinator("dispatch", "status")["dispatches"][0]["status"],
                         "queued")
        (self.root / "deny-preflight").unlink()
        self.coordinator("dispatch", "review", "--reason", "synthetic helper now ready")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-restore-unattempted")
                         ["attempt_number"], 1)
        self.coordinator("dispatch", "restore-queued", "--dispatch-id", queued["dispatch_id"],
                         "--reason", "cannot restore after an actual attempt", okay=False)

    def test_old_owner_orphan_is_not_reauthorized_by_reconcile(self):
        channel = self.channels["group"]
        self.outbox("group", "queue", "--request-id", "old-owner-orphan",
                    "--channel-id", channel["binding"]["channel_id"],
                    "--session-id", channel["binding"]["session_id"],
                    "--owner", "owner_group", "--expected-epoch", channel["epoch"],
                    "--text-stdin", data="synthetic old owner intent")
        self.coordinator("configure", "--conversation-id", channel["id"],
                         "--expected-epoch", channel["epoch"], "--owner", "new_owner",
                         "--input", "pull", "--reply-policy", "authorized")
        rebuilt = self.coordinator("reconcile-outbox", "--conversation-id", channel["id"])
        self.assertEqual(rebuilt["created"], 0)
        self.assertEqual(rebuilt["quarantined"], 1)
        self.assertEqual(self.outbox("group", "show", "--request-id", "old-owner-orphan")
                         ["attempt_number"], 0)
        self.assertFalse(any(item["request_id"] == "old-owner-orphan" for item in
                             self.coordinator("dispatch", "status")["dispatches"]))

    def test_rapid_incoming_events_form_one_frozen_delivery(self):
        group = self.channels["group"]
        with sqlite3.connect(group["source"] / "message/message_0.db") as db:
            for index in range(3):
                db.execute(
                    f"INSERT INTO [{group['table']}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)",
                    (40 + index, 140 + index, 1700000040 + index,
                     f"synthetic_member:\nrapid input {index}".encode()),
                )
        self.cli("channel", group["state"], "sync")
        reconciled = self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        self.assertEqual(reconciled["created"], 1)
        claim = self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                 "--owner", "owner_group")
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual((claim["cursor_start"], claim["cursor_end"]), (4, 6))
        self.assertEqual(self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                          "--owner", "owner_group")["status"], "empty")
        self.coordinator("inbox", "complete", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--decision", "skip")
        shown = self.coordinator("inbox", "show", "--delivery-id", claim["delivery_id"],
                                 "--owner", "owner_group")
        self.assertEqual(shown["status"], "skipped")
        self.assertEqual((shown["cursor_start"], shown["cursor_end"]), (4, 6))

    def test_unmatched_self_source_pauses_only_its_conversation(self):
        from wx_agent import dispatch
        group = self.channels["group"]
        self.queue("group", "group-after-manual")
        self.queue("private", "private-after-manual")
        self.observe_self_source()
        result = self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        self.assertTrue(result["possible_manual_outgoing"])
        self.assertEqual(self.coordinator("status", "--conversation-id", group["id"])
                         ["control"], "taken_over")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-after-manual")
                         ["attempt_number"], 0)
        self.assertEqual(self.outbox("private", "show", "--request-id", "private-after-manual")
                         ["attempt_number"], 1)

    def test_proven_self_outgoing_does_not_take_over_conversation(self):
        group = self.channels["group"]
        request = "group-proven-outgoing"
        self.queue("group", request)
        cursor = self.observe_self_source()
        report = self.ui / "synthetic-proven-send.json"
        binding=group["binding"]
        tid="send-"+hashlib.sha256((binding["channel_id"]+"\0"+request).encode()).hexdigest()[:48]
        report.write_text(json.dumps({"id":tid,"attempt_number":1,"request_id": request, "status": "ui_observed",
                                      "channel_id":binding["channel_id"],"session_id":binding["session_id"],"event_cursor": cursor}))
        report.chmod(0o600)
        with sqlite3.connect(group["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_intent SET status='ui_observed',attempt_number=1,"
                       "result_evidence=? WHERE request_id=?", (str(report), request))
        result = self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        self.assertFalse(result["possible_manual_outgoing"])
        status = self.coordinator("status", "--conversation-id", group["id"])
        self.assertEqual((status["control"], status["epoch"], status["watermark"]),
                         ("active", group["epoch"], cursor))

    def test_inflight_attempt_holds_inbox_watermark(self):
        group = self.channels["group"]
        request = "group-inflight-outgoing"
        self.queue("group", request)
        cursor = self.observe_self_source()
        with sqlite3.connect(group["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_intent SET status='attempt_recorded',attempt_number=1 "
                       "WHERE request_id=?", (request,))
        result = self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        self.assertFalse(result["possible_manual_outgoing"])
        self.assertEqual(result["watermark"], cursor - 1)
        status = self.coordinator("status", "--conversation-id", group["id"])
        self.assertEqual((status["control"], status["epoch"]), ("active", group["epoch"]))
        self.assertEqual(self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                          "--owner", "owner_group")["status"], "empty")

    def test_owner_session_ref_is_fenced_until_configure_epoch(self):
        group = self.channels["group"]
        for sequence, server in ((40, 140), (41, 141)):
            with sqlite3.connect(group["source"] / "message/message_0.db") as db:
                db.execute(
                    f"INSERT INTO [{group['table']}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)",
                    (sequence, server, 1700000000 + sequence, b"synthetic_member:\nnew input"),
                )
            self.cli("channel", group["state"], "sync")
            self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
            claim = self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                     "--owner", "owner_group")
            if sequence == 40:
                self.coordinator("inbox", "reserve-turn", "--delivery-id", claim["delivery_id"],
                                 "--token", claim["token"], "--session-ref", "codex:test:original")
                self.coordinator("inbox", "record-turn", "--delivery-id", claim["delivery_id"],
                                 "--token", claim["token"], "--thread-id", "original",
                                 "--turn-id", "original-turn")
                self.coordinator("inbox", "finish-turn", "--delivery-id", claim["delivery_id"],
                                 "--token", claim["token"], "--status", "completed")
                self.coordinator("inbox", "complete", "--delivery-id", claim["delivery_id"],
                                 "--token", claim["token"], "--decision", "skip")
            else:
                self.coordinator("inbox", "reserve-turn", "--delivery-id", claim["delivery_id"],
                                 "--token", claim["token"], "--session-ref", "codex:test:other",
                                 okay=False)
                self.assertEqual(self.coordinator("status", "--conversation-id", group["id"])
                                 ["owner_session_ref"], "codex:test:original")
                configured = self.coordinator("configure", "--conversation-id", group["id"],
                                              "--expected-epoch", group["epoch"],
                                              "--owner", "owner_group", "--input", "push",
                                              "--reply-policy", "authorized")
                self.assertEqual(configured["epoch"], group["epoch"] + 1)
                renewed = self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                            "--owner", "owner_group")
                self.assertEqual(renewed["delivery_id"], claim["delivery_id"])
                self.coordinator("inbox", "reserve-turn", "--delivery-id", renewed["delivery_id"],
                                 "--token", renewed["token"], "--session-ref", "codex:test:other")
                self.assertEqual(self.coordinator("status", "--conversation-id", group["id"])
                                 ["owner_session_ref"], "codex:test:other")
        self.coordinator("inbox","record-turn","--delivery-id",renewed["delivery_id"],"--token",renewed["token"],
                         "--thread-id","other","--turn-id","second-turn")
        self.coordinator("inbox","finish-turn","--delivery-id",renewed["delivery_id"],"--token",renewed["token"],"--status","completed")
        self.coordinator("inbox","complete","--delivery-id",renewed["delivery_id"],"--token",renewed["token"],"--decision","skip")
        private=self.channels["private"]
        with sqlite3.connect(private["source"]/"message/message_0.db") as db:
            db.execute(f"INSERT INTO [{private['table']}] VALUES (60,160,1,1,1700000060,?,NULL,0)",
                       (b"synthetic_member:\nseparate private conversation",))
        self.cli("channel",private["state"],"sync")
        self.coordinator("inbox","reconcile","--conversation-id",private["id"])
        separate=self.coordinator("inbox","claim","--conversation-id",private["id"],"--owner","owner_private")
        self.coordinator("inbox","reserve-turn","--delivery-id",separate["delivery_id"],"--token",separate["token"],
                         "--session-ref","codex:test:other",okay=False)
        self.coordinator("inbox","reserve-turn","--delivery-id",separate["delivery_id"],"--token",separate["token"],
                         "--session-ref","codex:test:original",okay=False)
        self.assertIsNone(self.coordinator("status","--conversation-id",private["id"])["owner_session_ref"])

    def test_real_events_inbox_claim_decisions_and_owner_fence(self):
        # Enrollment cut over the historical fixture. Add one incoming event to each source.
        for name, channel in self.channels.items():
            with sqlite3.connect(channel["source"] / "message/message_0.db") as db:
                db.execute(f"INSERT INTO [{channel['table']}] VALUES (30, 130, 1, 1, 1700000030, ?, NULL, 0)",
                           (b"synthetic_member:\nnew inbound",))
            self.cli("channel", channel["state"], "sync")
            event = self.cli("channel", channel["state"], "events", "--after", "3")
            self.assertEqual(event["kind"], "message_observed")
            self.assertEqual(event["server_id"], 130)
            self.assertEqual(self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])["created"], 1)
            self.assertEqual(self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])["created"], 0)
        group = self.channels["group"]
        claim = self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                 "--owner", "owner_group")
        shown = self.coordinator("inbox", "show", "--delivery-id", claim["delivery_id"],
                                 "--owner", "owner_group")
        self.assertEqual(shown["cursor_start"], 4)
        self.assertEqual(shown["status"], "claimed")
        self.assertEqual(self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                          "--owner", "owner_group")["status"], "empty")
        # Manual and push paths reference this same delivery; a transport
        # reservation does not generate a second inbox row or model turn.
        reserved = self.coordinator("inbox", "reserve-turn", "--delivery-id", claim["delivery_id"],
                                    "--token", claim["token"], "--session-ref", "codex:test:thread1")
        self.assertFalse(reserved["already_reserved"])
        repeated = self.coordinator("inbox", "reserve-turn", "--delivery-id", claim["delivery_id"],
                                    "--token", claim["token"], "--session-ref", "codex:test:thread1")
        self.assertTrue(repeated["already_reserved"])
        self.coordinator("inbox", "record-turn", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--thread-id", "thread1",
                         "--turn-id", "synthetic-turn1")
        self.coordinator("inbox", "finish-turn", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--status", "completed")
        self.coordinator("inbox", "complete", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--decision", "defer",
                         "--reason", "synthetic wait", "--condition", "next update")
        later = self.coordinator("inbox", "status", "--conversation-id", group["id"])
        self.assertEqual(later["deliveries"][0]["status"], "deferred")
        # A newer cursor does not discard an earlier deferred decision.
        with sqlite3.connect(group["source"] / "message/message_0.db") as db:
            db.execute(f"INSERT INTO [{group['table']}] VALUES (40, 140, 1, 1, 1700000040, ?, NULL, 0)",
                       (b"synthetic_member:\nsecond inbound",))
        self.cli("channel", group["state"], "sync")
        self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        second = self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                  "--owner", "owner_group")
        self.assertNotEqual(second["delivery_id"], claim["delivery_id"])
        self.coordinator("inbox", "complete", "--delivery-id", second["delivery_id"],
                         "--token", second["token"], "--decision", "draft",
                         "--reason", "synthetic draft", "--draft-stdin", data="synthetic draft text")
        statuses = [item["status"] for item in self.coordinator(
            "inbox", "status", "--conversation-id", group["id"])["deliveries"]]
        self.assertEqual(statuses, ["deferred", "draft"])
        epoch = group["epoch"]
        self.coordinator("takeover", "--conversation-id", group["id"], "--expected-epoch", epoch)
        self.coordinator("inbox", "resume", "--delivery-id", claim["delivery_id"],
                         "--owner", "owner_group", "--expected-epoch", epoch, okay=False)
        self.coordinator("handback", "--conversation-id", group["id"], "--expected-epoch", epoch + 1)
        self.coordinator("inbox", "resume", "--delivery-id", claim["delivery_id"],
                         "--owner", "owner_group", "--expected-epoch", epoch + 2)
        states = [item["status"] for item in self.coordinator(
            "inbox", "status", "--conversation-id", group["id"])["deliveries"]]
        self.assertEqual(states, ["pending", "draft"])
        private = self.channels["private"]
        private_claim = self.coordinator("inbox", "claim", "--conversation-id", private["id"],
                                         "--owner", "owner_private")
        self.coordinator("takeover", "--conversation-id", private["id"],
                         "--expected-epoch", private["epoch"])
        self.coordinator("inbox", "complete", "--delivery-id", private_claim["delivery_id"],
                         "--token", private_claim["token"], "--decision", "skip", okay=False)

    def test_image_event_keeps_attachment_reference_in_notification(self):
        group = self.channels["group"]
        with sqlite3.connect(group["source"] / "message/message_0.db") as db:
            db.execute(f"INSERT INTO [{group['table']}] VALUES (30, 130, 3, 1, 1700000030, ?, NULL, 0)",
                       (b"",))
        self.cli("channel", group["state"], "sync")
        event = self.cli("channel", group["state"], "events", "--after", "3")
        self.assertEqual(event["kind"], "message_observed")
        self.assertIn("Image", event["message"]["content"])
        self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        claim = self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                 "--owner", "owner_group")
        self.assertEqual(claim["cursor_start"], event["cursor"])
        self.assertTrue(claim.get("attachment_refs"),
                        "image notification must carry a source attachment reference")

    def test_out_of_order_text_image_batch_executes_part_zero_first(self):
        from wx_agent import dispatch
        self.queue("private", "batch-image-1", image=True, batch_id="synthetic-batch",
                   part_index=1)
        self.assertEqual(self.coordinator("dispatch", "claim", "--worker", "batch-check")
                         ["status"], "empty")
        self.queue("private", "batch-text-0", batch_id="synthetic-batch", part_index=0)
        dispatch.drain(BINARY, self.ui)
        begins = [json.loads(line) for line in self.log.read_text().splitlines()
                  if json.loads(line)["phase"] == "begin"]
        self.assertEqual([item["request_id"] for item in begins],
                         ["batch-text-0", "batch-image-1"])
        self.assertEqual([item["media"] for item in begins], [False, True])

    def test_failed_batch_predecessor_blocks_image_but_other_conversation_runs(self):
        from wx_agent import dispatch
        self.queue("private", "batch-image-after-failure", image=True,
                   batch_id="synthetic-failure-batch", part_index=1)
        self.queue("private", "fail-batch-text", batch_id="synthetic-failure-batch",
                   part_index=0)
        self.queue("group", "other-after-failure")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("private", "show", "--request-id", "fail-batch-text")
                         ["status"], "failed")
        self.assertEqual(self.outbox("private", "show", "--request-id", "batch-image-after-failure")
                         ["attempt_number"], 0)
        self.assertEqual(self.outbox("group", "show", "--request-id", "other-after-failure")
                         ["attempt_number"], 0)
        self.coordinator("dispatch", "review", "--reason", "synthetic failed predecessor reviewed")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "other-after-failure")
                         ["attempt_number"], 1)
        self.assertEqual(self.outbox("private", "show", "--request-id", "batch-image-after-failure")
                         ["attempt_number"], 0)

    def test_priority_expiry_and_same_id_scheduling_conflict(self):
        from wx_agent import dispatch
        self.queue("group", "priority-low", priority=2)
        self.queue("private", "priority-high", priority=0)
        dispatch.drain(BINARY, self.ui)
        begins = [json.loads(line)["request_id"] for line in self.log.read_text().splitlines()
                  if json.loads(line)["phase"] == "begin"]
        self.assertEqual(begins, ["priority-high", "priority-low"])
        self.queue("group", "expired-intent", expires_at=int(time.time()) - 1)
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "expired-intent")
                         ["attempt_number"], 0)
        self.assertFalse(any(json.loads(line)["request_id"] == "expired-intent"
                             for line in self.log.read_text().splitlines()))
        channel = self.channels["group"]
        self.outbox("group", "queue", "--request-id", "priority-low",
                    "--channel-id", channel["binding"]["channel_id"],
                    "--session-id", channel["binding"]["session_id"],
                    "--owner", "owner_group", "--expected-epoch", channel["epoch"],
                    "--priority", 0, "--text-stdin", data="synthetic reply", okay=False)

    def test_defer_requeues_on_due_or_new_information_draft_needs_explicit_cancel(self):
        group = self.channels["group"]

        def append(cursor, server_id):
            with sqlite3.connect(group["source"] / "message/message_0.db") as db:
                db.execute(f"INSERT INTO [{group['table']}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)",
                           (cursor * 10, server_id, 1700000000 + cursor * 10,
                            b"synthetic_member:\nsynthetic update"))
            self.cli("channel", group["state"], "sync")
            self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])

        def claim():
            return self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                    "--owner", "owner_group")

        def complete(item, decision, *extra, data=None):
            return self.coordinator("inbox", "complete", "--delivery-id", item["delivery_id"],
                                    "--token", item["token"], "--decision", decision,
                                    *extra, data=data)

        append(4, 130)
        first = claim()
        complete(first, "defer", "--reason", "synthetic due", "--due-at", int(time.time()) - 1)
        self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        due = claim()
        self.assertEqual(due["delivery_id"], first["delivery_id"])
        complete(due, "skip")
        append(5, 140)
        second = claim()
        complete(second, "defer", "--reason", "synthetic wait",
                 "--condition", "new_information")
        self.assertEqual(self.coordinator("inbox", "status", "--conversation-id", group["id"])
                         ["deliveries"][1]["status"], "deferred")
        append(6, 150)
        resumed = claim()
        self.assertEqual(resumed["delivery_id"], second["delivery_id"])
        complete(resumed, "skip")
        draft = claim()
        complete(draft, "draft", "--reason", "synthetic approval",
                 "--due-at", int(time.time()) - 1, "--draft-stdin",
                 data="synthetic draft only")
        self.coordinator("inbox", "reconcile", "--conversation-id", group["id"])
        shown = self.coordinator("inbox", "show", "--delivery-id", draft["delivery_id"],
                                 "--owner", "owner_group")
        self.assertEqual(shown["status"], "draft")
        self.assertEqual(self.coordinator("inbox", "claim", "--conversation-id", group["id"],
                                          "--owner", "owner_group")["status"], "empty")
        self.coordinator("inbox", "cancel", "--delivery-id", draft["delivery_id"],
                         "--owner", "owner_group", "--expected-epoch", group["epoch"],
                         "--reason", "synthetic cancellation")
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            history = db.execute("SELECT decision,draft_text FROM delivery_decision_history "
                                 "WHERE delivery_id=? ORDER BY id", (draft["delivery_id"],)).fetchall()
        self.assertEqual([entry[0] for entry in history], ["draft", "cancel"])
        self.assertEqual(history[0][1], "synthetic draft only")


if __name__ == "__main__":
    unittest.main()
