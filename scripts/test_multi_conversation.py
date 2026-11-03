#!/usr/bin/env python3
"""Synthetic two-channel integration against the compiled Rust coordinator."""
import base64
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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
if sys.argv[1:] == ['--recovery-capabilities']:
    if (root/'deny-recovery-capability').exists():
        print(json.dumps({'unsupported':True}))
    else:
        print(json.dumps({'protocol_version':1,'recovery_protocol_version':1,
                          'pre_submit_proof_required':True}))
    sys.exit(0)
if sys.argv[1:] == ['--abandon-capabilities']:
    capability={'protocol_version':1,'abandon_protocol_version':1,
                'image_before_prepare_only':True,'pre_submit_proof_required':True}
    if (root/'deny-abandon-capability').exists():
        capability['image_before_prepare_only']=False
    if (root/'malformed-abandon-capability').exists():
        capability['unexpected']=True
    if (root/'missing-abandon-capability').exists():
        capability={'unsupported':True}
    print(json.dumps(capability))
    sys.exit(0)
if sys.argv[1:] == ['--abandon-preflight']:
    (root/'abandon-payload-received').write_text('yes')
    candidate=json.load(sys.stdin)
    if (root/'missing-abandon-proof').exists():
        sys.exit(0)
    if (root/'deny-abandon-proof').exists():
        print(json.dumps({'protocol_version':1,'abandon_protocol_version':1,
                          'unsent_proven':False,'before_prepare':True}))
        sys.exit(0)
    if (root/'malformed-abandon-proof').exists():
        print(json.dumps({'protocol_version':1,'abandon_protocol_version':1,
                          'unsent_proven':True,'before_prepare':True,'extra':True}))
        sys.exit(0)
    identity=candidate['channel_id']+'\\0'+candidate['request_id']
    import hashlib
    tid='send-'+hashlib.sha256(identity.encode()).hexdigest()[:48]
    ui=root/'ui'
    report=json.loads((ui/(tid+'.client-result.json')).read_text())
    exact=set(candidate)=={'protocol_version','abandon_protocol_version','content_kind',
                           'request_id','attempt_number','channel_id','session_id','account','talker'}
    valid=(exact and candidate['protocol_version']==1 and candidate['abandon_protocol_version']==1
           and candidate['content_kind']=='image' and candidate['attempt_number']==1
           and report['id']==tid and report['request_id']==candidate['request_id']
           and report['stage']=='inspected' and report['status']=='indeterminate'
           and report['error']=='Search popup not enumerated by ScreenCaptureKit within six seconds'
           and (ui/(tid+'-inspect.result.json')).is_file()
           and (ui/(tid+'-focus.result.json')).is_file()
           and (ui/(tid+'-focus.focus-attempted.json')).stat().st_size==0)
    if not valid:
        sys.exit(3)
    print(json.dumps({'protocol_version':1,'abandon_protocol_version':1,
                      'unsent_proven':True,'before_prepare':True}))
    sys.exit(0)
if sys.argv[1:] == ['--dispatch-preflight']:
    os.fstat(int(os.environ['WX_DESKTOP_LOCK_FD']))
    print(json.dumps({'ready':not (root/'deny-preflight').exists()}))
    sys.exit(0)
if sys.argv[1:] == ['--recovery-preflight']:
    (root/'recovery-payload-received').write_text('yes')
    if (root/'deny-recovery-proof').exists():
        sys.exit(3)
    candidate=json.load(sys.stdin)
    previous=candidate['recovery_from_attempt']
    identity=candidate['channel_id']+'\\0'+candidate['request_id']
    import hashlib
    tid='send-'+hashlib.sha256(identity.encode()).hexdigest()[:48]
    if previous > 1:
        tid+='-a'+str(previous)
    ui=root/'ui'
    report=json.loads((ui/(tid+'.client-result.json')).read_text())
    names=((tid+'-inspect.inspect.job.json',tid+'-inspect.result.json'),
           (tid+'-focus.focus-group.job.json',tid+'-focus.result.json'),
           (tid+'.prepare-visual.job.json',tid+'.prepare-visual.result.json'),
           (tid+'.visual-state.json',))
    artifacts={name:json.loads((ui/name).read_text()) for group in names for name in group
               if (ui/name).is_file()}
    complete=(previous==1 and candidate['attempt_number']==2 and candidate['protocol_version']==1
              and report['stage']=='group_focused' and report['status']=='indeterminate'
              and report['request_id']==candidate['request_id'] and candidate['text']=='synthetic reply'
              and len(artifacts)==7 and artifacts[tid+'.visual-state.json']=={'id':tid,'stage':'prepare-attempted'}
              and artifacts[tid+'.prepare-visual.result.json']=={'id':tid,'ok':False,'error':'Cannot snapshot a clipboard item'}
              and report.get('error')==artifacts[tid+'.prepare-visual.result.json']['error']
              and all(artifacts[tid+suffix+'.result.json']=={'id':tid+suffix,'ok':True,'action':action}
                      for suffix,action in (('-inspect','inspect'),('-focus','focus-group')))
              and all('submit' not in path.name for path in ui.glob(tid+'*')))
    if not complete:
        sys.exit(3)
    print(json.dumps({'protocol_version':1,'recovery_protocol_version':1,'unsent_proven':True}))
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
            shutil.copy2(self.driver, channel["state"] / "conversation-sender")

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

    def test_nonblocking_python_send_freezes_real_intents_without_desktop_effect(self):
        from wx_agent import conversations
        channel = self.channels["group"]
        template = self.root / "sender-template.json"
        template.write_text(json.dumps({"ui_dir": str(self.ui)}))
        template.chmod(0o600)
        settings = {"wx_cli": str(BINARY), "sender_template": str(template)}
        entry = {"conversation_id": channel["id"], "state_dir": str(channel["state"]),
                 "channel_id": channel["binding"]["channel_id"],
                 "session_id": channel["binding"]["session_id"]}
        image = self.root / "nonblocking-source.png"
        image.write_bytes(PNG)
        media = conversations.sender.image_descriptor(image)
        # Only metadata lookup/calibration are synthetic. Intent freezing,
        # owner checks and dispatch persistence use the compiled Rust CLI.
        with mock.patch.object(conversations, "fresh", return_value=entry), \
                mock.patch.object(conversations, "channel_status", return_value={}), \
                mock.patch.object(conversations, "prepare_driver", return_value=self.driver):
            for index, (request, text, attachment) in enumerate((
                    ("nonblocking-text", "synthetic no-wait text", None),
                    ("nonblocking-image", "", media))):
                result = conversations.send_intent(
                    settings, entry, request, text, media=attachment,
                    owner="owner_group", expected_epoch=channel["epoch"],
                    scheduling={"batch_id": "nonblocking-pair", "part_index": index},
                    wait=False)
                self.assertEqual(result["status"], "pending")
                item = self.outbox("group", "show", "--request-id", request)
                self.assertEqual(item["attempt_number"], 0)
                self.assertEqual(item["part_index"], index)
            with self.assertRaises(conversations.ConversationError):
                conversations.send_intent(
                    settings, entry, "nonblocking-text", "conflicting replacement",
                    owner="owner_group", expected_epoch=channel["epoch"], wait=False)
        image.unlink()
        frozen = self.outbox("group", "show", "--request-id", "nonblocking-image")["media"]
        self.assertEqual(hashlib.sha256(Path(frozen["path"]).read_bytes()).hexdigest(), media["sha256"])
        self.assertEqual(len(self.coordinator("dispatch", "status")["dispatches"]), 2)
        self.assertFalse(self.log.exists(), "nonblocking enqueue must not invoke the driver")

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
        channel = self.channels["group"]
        self.coordinator("configure", "--conversation-id", channel["id"],
                         "--expected-epoch", channel["epoch"], "--owner", "owner_group",
                         "--input", "pull", "--reply-policy", "authorized")
        channel["epoch"] += 1
        (self.root / "deny-preflight").write_text("synthetic refusal")
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "group-restore-unattempted")
                         ["attempt_number"], 0)
        # Fixture for a crashed worker that left coordinator metadata unknown
        # before the sender recorded any attempt.
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE dispatch SET status='unknown',result='synthetic_abandoned_claim' "
                       "WHERE id=?", (queued["dispatch_id"],))
            db.execute("UPDATE surface SET active_dispatch=NULL")
        self.coordinator("dispatch", "restore-queued", "--dispatch-id", queued["dispatch_id"],
                         "--reason", "reviewed synthetic helper refusal")
        self.assertEqual(self.coordinator("dispatch", "status")["dispatches"][0]["status"],
                         "queued")
        # Simulate interruption after the outbox epoch commit but before the
        # dispatch journal catches up. Only this recorded transition resumes.
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE dispatch SET epoch=?,status='blocked' WHERE id=?",
                       (channel["epoch"] - 1, queued["dispatch_id"]))
            db.execute("INSERT INTO surface_review(reason,reviewed_at,unknown_count) VALUES(?,?,0)",
                       (f"restore-pending:{queued['dispatch_id']}:{channel['epoch'] - 1}:{channel['epoch']}", 1))
        self.coordinator("dispatch", "restore-queued", "--dispatch-id", queued["dispatch_id"],
                         "--reason", "resume exact committed recovery")
        (self.root / "deny-preflight").unlink()
        self.coordinator("dispatch", "pause", "--reason", "synthetic final recovery")
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

    def test_abandon_unsent_requires_pre_submit_evidence(self):
        from wx_agent import dispatch
        queued = self.queue("group", "unknown-pre-submit")
        dispatch.drain(BINARY, self.ui)
        channel = self.channels["group"]
        identity = hashlib.sha256((channel["binding"]["channel_id"] + "\0unknown-pre-submit").encode()).hexdigest()[:48]
        tid = "send-" + identity
        evidence = self.ui / (tid + ".client-result.json")
        report = {"id": tid, "request_id": "unknown-pre-submit",
            "attempt_number": 1, "channel_id": channel["binding"]["channel_id"],
            "session_id": channel["binding"]["session_id"], "status": "indeterminate",
            "stage": "group_focused", "error": "Cannot snapshot a clipboard item"}
        evidence.write_text(json.dumps(report))
        evidence.chmod(0o600)
        artifacts = {
            tid + "-inspect.inspect.job.json": {"id": tid + "-inspect", "action": "inspect"},
            tid + "-inspect.result.json": {"id": tid + "-inspect", "ok": True, "action": "inspect"},
            tid + "-focus.focus-group.job.json": {"id": tid + "-focus", "action": "focus-group"},
            tid + "-focus.result.json": {"id": tid + "-focus", "ok": True, "action": "focus-group"},
            tid + ".prepare-visual.job.json": {"id": tid, "action": "prepare-visual"},
            tid + ".prepare-visual.result.json": {"id": tid, "ok": False,
                                                   "error": "Cannot snapshot a clipboard item"},
            tid + ".visual-state.json": {"id": tid, "stage": "prepare-attempted"},
        }
        for name, value in artifacts.items():
            path = self.ui / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_attempt SET result_evidence=? WHERE request_id='unknown-pre-submit' AND attempt_number=1",
                       (str(evidence),))
            db.execute("UPDATE outgoing_intent SET result_evidence=? WHERE request_id='unknown-pre-submit'",
                       (str(evidence),))
        command = ("dispatch", "abandon-unsent", "--dispatch-id", queued["dispatch_id"],
                   "--expected-attempt", 1, "--reason", "reviewed complete pre-submit trace")
        (self.root / "deny-recovery-capability").write_text("legacy driver")
        self.coordinator(*command, okay=False)
        self.assertFalse((self.root / "recovery-payload-received").exists(),
                         "unknown driver must never receive the message payload")
        (self.root / "deny-recovery-capability").unlink()
        (self.root / "deny-recovery-proof").write_text("driver rejected proof")
        self.coordinator(*command, okay=False)
        self.assertEqual(self.outbox("group", "show", "--request-id", "unknown-pre-submit")
                         ["attempt_number"], 1)
        self.assertEqual(self.coordinator("dispatch", "status")["dispatches"][0]["status"],
                         "unknown")
        (self.root / "deny-recovery-proof").unlink()
        prepare_result = self.ui / (tid + ".prepare-visual.result.json")
        saved_result = prepare_result.read_bytes()
        prepare_result.unlink()
        self.coordinator(*command, okay=False)
        prepare_result.write_bytes(saved_result)
        prepare_result.chmod(0o600)
        report["error"] = "different native result"
        evidence.write_text(json.dumps(report))
        self.coordinator(*command, okay=False)
        report["error"] = "Cannot snapshot a clipboard item"
        evidence.write_text(json.dumps(report))
        visual = self.ui / (tid + ".visual-state.json")
        visual.write_text(json.dumps({"id": tid, "stage": "submit-attempted"}))
        self.coordinator(*command, okay=False)
        visual.write_text(json.dumps(artifacts[visual.name]))
        submit = self.ui / (tid + ".submit-visual.result.json")
        submit.write_text(json.dumps({"id": tid, "ok": True}))
        submit.chmod(0o600)
        self.coordinator(*command, okay=False)
        submit.unlink()
        report["stage"] = "prepared"
        evidence.write_text(json.dumps(report))
        self.coordinator(*command, okay=False)
        report["stage"] = "group_focused"
        evidence.write_text(json.dumps(report))
        outside = self.root / evidence.name
        outside.write_text(evidence.read_text())
        outside.chmod(0o600)
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_attempt SET result_evidence=? WHERE request_id='unknown-pre-submit' AND attempt_number=1",
                       (str(outside),))
            db.execute("UPDATE outgoing_intent SET result_evidence=? WHERE request_id='unknown-pre-submit'",
                       (str(outside),))
        self.coordinator(*command, okay=False)
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_attempt SET result_evidence=? WHERE request_id='unknown-pre-submit' AND attempt_number=1",
                       (str(evidence),))
            db.execute("UPDATE outgoing_intent SET result_evidence=? WHERE request_id='unknown-pre-submit'",
                       (str(evidence),))
        self.coordinator("dispatch", "abandon-unsent", "--dispatch-id", queued["dispatch_id"],
                         "--expected-attempt", 2, "--reason", "wrong attempt must fail", okay=False)
        self.coordinator("dispatch", "abandon-unsent", "--dispatch-id", queued["dispatch_id"],
                         "--expected-attempt", 1, "--reason", "reviewed pre-submit stop only")
        self.assertEqual(self.outbox("group", "show", "--request-id", "unknown-pre-submit")
                         ["status"], "indeterminate")
        self.assertEqual(self.coordinator("dispatch", "status")["dispatches"][0]["result"],
                         "abandoned_pre_submit")
        self.coordinator("dispatch", "review", "--reason", "synthetic pre-submit proof reviewed")
        image = self.queue("private", "unknown-image-abandon", image=True)
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("private", "show", "--request-id", "unknown-image-abandon")
                         ["attempt_number"], 1)
        self.coordinator("dispatch", "abandon-unsent", "--dispatch-id", image["dispatch_id"],
                         "--expected-attempt", 1, "--reason", "image has no complete proof protocol",
                         okay=False)
        self.assertEqual(self.outbox("private", "show", "--request-id", "unknown-image-abandon")
                         ["status"], "indeterminate")

    def test_image_abandon_requires_first_attempt_complete_pre_prepare_proof(self):
        from wx_agent import dispatch
        channel = self.channels["group"]
        request = "unknown-image-inspected"
        queued = self.queue("group", request, image=True)
        dispatch.drain(BINARY, self.ui)
        identity = hashlib.sha256((channel["binding"]["channel_id"] + "\0" + request).encode()).hexdigest()[:48]
        tid = "send-" + identity
        evidence = self.ui / (tid + ".client-result.json")
        report = {"id": tid, "request_id": request, "attempt_number": 1,
                  "channel_id": channel["binding"]["channel_id"],
                  "session_id": channel["binding"]["session_id"],
                  "status": "indeterminate", "stage": "inspected",
                  "error": "Search popup not enumerated by ScreenCaptureKit within six seconds"}
        evidence.write_text(json.dumps(report)); evidence.chmod(0o600)
        artifacts = {
            tid + "-inspect.inspect.job.json": {"id": tid + "-inspect", "action": "inspect"},
            tid + "-inspect.result.json": {"id": tid + "-inspect", "ok": True, "action": "inspect"},
            tid + "-focus.focus-group.job.json": {"id": tid + "-focus", "action": "focus-group"},
            tid + "-focus.result.json": {"id": tid + "-focus", "ok": False,
                                          "error": report["error"]},
        }
        for name, value in artifacts.items():
            path = self.ui / name
            path.write_text(json.dumps(value)); path.chmod(0o600)
        marker = self.ui / (tid + "-focus.focus-attempted.json")
        marker.touch(mode=0o600)
        for suffix in (
            "focus-initial.png", "before-search-click.png", "before-search-select.png",
            "before-search-paste.png", "before-search-copy.png", "search-copy-ready.png",
            "focus-search-result-main.png",
        ):
            path = self.ui / (tid + "-focus." + suffix)
            path.write_bytes(PNG); path.chmod(0o600)
        polls = self.ui / (tid + "-focus.focus-polls.json")
        polls.write_text(json.dumps([{"windowCount": 1, "popupPresent": False,
                                      "popupSelected": False}])); polls.chmod(0o600)
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_attempt SET result_evidence=? WHERE request_id=? AND attempt_number=1",
                       (str(evidence), request))
            db.execute("UPDATE outgoing_intent SET result_evidence=? WHERE request_id=?",
                       (str(evidence), request))
        command = ("dispatch", "abandon-unsent", "--dispatch-id", queued["dispatch_id"],
                   "--expected-attempt", 1, "--reason", "reviewed image before prepare only")
        def refused_with_file(name, content="flag"):
            path = self.root / name
            path.write_text(content)
            try:
                self.coordinator(*command, okay=False)
            finally:
                path.unlink()
        for name in ("missing-abandon-capability", "deny-abandon-capability",
                     "malformed-abandon-capability"):
            refused_with_file(name)
            self.assertFalse((self.root / "abandon-payload-received").exists())
        for name in ("missing-abandon-proof", "deny-abandon-proof", "malformed-abandon-proof"):
            refused_with_file(name)
        self.coordinator("dispatch", "abandon-unsent", "--dispatch-id", queued["dispatch_id"],
                         "--expected-attempt", 2, "--reason", "wrong image attempt", okay=False)
        for change in ({"stage": "prepared"}, {"media_prepared": {"spoof": True}},
                       {"media_submitted": {"spoof": True}}):
            altered = dict(report, **change)
            evidence.write_text(json.dumps(altered))
            self.coordinator(*command, okay=False)
        evidence.write_text(json.dumps(report))
        for name in (tid + ".prepare-visual.job.json", tid + ".visual-state.json",
                     tid + ".submit-visual.result.json", tid + "-a2.client-result.json"):
            path = self.ui / name
            path.write_text("{}"); path.chmod(0o600)
            try:
                self.coordinator(*command, okay=False)
            finally:
                path.unlink()
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_intent SET content_kind='text' WHERE request_id=?", (request,))
        self.coordinator(*command, okay=False)
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            db.execute("UPDATE outgoing_intent SET content_kind='image' WHERE request_id=?", (request,))
        before = self.outbox("group", "show", "--request-id", request)
        self.assertEqual((before["status"], before["attempt_number"]), ("indeterminate", 1))
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            attempts_before = db.execute("SELECT status,result_evidence FROM outgoing_attempt WHERE request_id=?",
                                         (request,)).fetchall()
        result = self.coordinator(*command)
        self.assertEqual((result["status"], result["outbox_status"], result["attempt_number"]),
                         ("done", "indeterminate", 1))
        self.assertEqual(self.outbox("group", "show", "--request-id", request), before)
        with sqlite3.connect(channel["state"] / "outgoing.sqlite3") as db:
            self.assertEqual(db.execute("SELECT status,result_evidence FROM outgoing_attempt WHERE request_id=?",
                                        (request,)).fetchall(), attempts_before)
        self.assertEqual(self.coordinator("dispatch", "status")["dispatches"][0]["result"],
                         "abandoned_pre_submit")
        self.coordinator("dispatch", "review", "--reason", "synthetic image proof reviewed")
        focus = self.coordinator("enqueue-focus", "--conversation-id", channel["id"],
                                 "--operation-id", "new-distinct-focus", "--expected-epoch", channel["epoch"])
        claimed = self.coordinator("dispatch", "claim", "--worker", "synthetic-focus-worker")
        self.assertEqual((claimed["dispatch_id"], claimed["operation"]),
                         (focus["dispatch_id"], "focus"))

    def test_reviewed_old_focus_does_not_block_new_send(self):
        from wx_agent import dispatch
        channel = self.channels["group"]
        queued = self.queue("group", "send-after-reviewed-focus")
        self.coordinator("configure", "--conversation-id", channel["id"],
                         "--expected-epoch", channel["epoch"], "--owner", "owner_group",
                         "--input", "pull", "--reply-policy", "authorized")
        channel["epoch"] += 1
        self.coordinator("dispatch", "restore-queued", "--dispatch-id", queued["dispatch_id"],
                         "--reason", "reviewed unattempted send after focus")
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("INSERT INTO dispatch(conversation_id,request_id,operation,channel_id,epoch,status,result,created_at,updated_at) VALUES(?,?,?,?,?,'blocked','focus_failed_requires_review',1,1)",
                       (channel["id"], "old-focus", "focus", channel["binding"]["channel_id"], channel["epoch"] - 1))
            db.execute("INSERT INTO dispatch(conversation_id,request_id,operation,channel_id,epoch,status,result,created_at,updated_at) VALUES(?,?,?,?,?,'unknown','helper_disappeared',1,1)",
                       (self.channels["private"]["id"], "unknown-focus", "focus", self.channels["private"]["binding"]["channel_id"], self.channels["private"]["epoch"]))
        other = self.queue("private", "send-after-unknown-focus")
        older = self.queue("group", "send-blocked-by-old-epoch")
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE dispatch SET status='blocked',result='owner_epoch_changed',epoch=? WHERE id=?",
                       (channel["epoch"] - 1, older["dispatch_id"]))
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("private", "show", "--request-id", "send-after-unknown-focus")
                         ["attempt_number"], 0)
        self.assertEqual(self.outbox("group", "show", "--request-id", "send-after-reviewed-focus")
                         ["attempt_number"], 1)
        self.assertEqual(self.outbox("group", "show", "--request-id", "send-blocked-by-old-epoch")
                         ["attempt_number"], 0)

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

    def test_archive_original_image_copy_rejection_preserves_history_and_blocks_caption(self):
        from wx_agent import macos_sender_driver as sender
        channel = self.channels["group"]
        request = "known-original-image-copy-rejection"
        parent = self.queue("group", request, image=True, batch_id="original-copy-rejected-batch", part_index=0)
        caption = self.queue("group", "caption-of-rejected-image", batch_id="original-copy-rejected-batch", part_index=1)
        out_path = channel["state"] / "outgoing.sqlite3"
        with sqlite3.connect(out_path) as db:
            media = dict(zip(("path", "sha256", "mime_type", "size_bytes"), db.execute(
                "SELECT media_path,media_sha256,media_mime_type,media_size_bytes FROM outgoing_intent WHERE request_id=?", (request,)).fetchone()))
        with sqlite3.connect(channel["state"] / "channel.sqlite3") as db:
            db.execute('UPDATE channel_binding SET encrypted_snapshot=1')
        binding = self.cli("channel", channel["state"], "status")["binding"]
        tid = "send-" + hashlib.sha256((binding["channel_id"] + "\0" + request).encode()).hexdigest()[:48]
        focus, inspect = tid + "-focus", tid + "-inspect"
        frame = {"x": 100, "y": 100, "width": 880, "height": 640}
        permissions = {"accessibility": True, "screenRecording": True, "postEvents": True}
        config = {"ui_dir": str(self.ui), "channel_state_dir": str(channel["state"]), "wx_cli": str(BINARY),
                  "binding": binding, "target_name": "Synthetic Group", "target_group": "Synthetic Group",
                  "conversation_kind": "group", "expected_pid": 42, "expected_draft": "",
                  "window_size": {"width": 880, "height": 640},
                  "conversation_list_rect": {"x": 60, "y": 55, "width": 240, "height": 570},
                  "search_point": {"x": 155, "y": 28}, "composer_rect": {"x": 320, "y": 500, "width": 400, "height": 80},
                  "button_point": {"x": 800, "y": 610}}
        prepare = dict(sender._prepare_command({"media": media}, config, 21, frame, focus), id=tid, action="prepare-visual")
        visual = dict(prepare, text="", stage="prepare-attempted"); visual.pop("action")
        artifacts = {
            tid + ".client-result.json": {"id": tid, "request_id": request, "attempt_number": 1,
                "channel_id": binding["channel_id"], "session_id": binding["session_id"],
                "stage": "group_focused", "status": "indeterminate", "error": "helper rejected action",
                "target_kind": "group", "focus_evidence_id": focus, "focus_category": "group-chats"},
            inspect + ".inspect.job.json": {"id": inspect, "action": "inspect"},
            inspect + ".result.json": {"id": inspect, "action": "inspect", "ok": True, "weChatPID": 42,
                "windows": [{"id": 21, "frame": frame}], "permissions": permissions, "session": {"onConsole": True, "locked": False}},
            focus + ".focus-group.job.json": {"id": focus, "action": "focus-group", "targetGroup": "Synthetic Group",
                "targetKind": "group", "expectedPID": 42, "expectedWindowID": 21, "expectedFrame": frame,
                "conversationListRect": config["conversation_list_rect"], "searchPoint": config["search_point"]},
            focus + ".result.json": {"id": focus, "action": "focus-group", "ok": True, "focused": True,
                "alreadySelected": False, "source": "search", "targetKind": "group", "focusEvidenceID": focus, "category": "group-chats", "permissions": permissions},
            focus + ".focus-evidence.json": {"id": focus, "targetGroup": "Synthetic Group", "targetKind": "group",
                "expectedPID": 42, "expectedWindowID": 21, "expectedFrame": frame, "source": "search", "category": "group-chats", "selectedAt": 1},
            tid + ".prepare-visual.job.json": prepare,
            tid + ".prepare-visual.result.json": {"id": tid, "ok": False, "permissions": permissions,
                "error": "Composer Copy did not replace image sentinel exactly once (types: public.file-url,public.tiff)"},
            tid + ".visual-state.json": visual}
        for name, value in artifacts.items(): sender.private_create(self.ui / name, value)
        evidence = self.ui / (tid + ".client-result.json")
        timestamp = int(time.time())
        # Seed only the synthetic historical attempt. No driver/send command is
        # used to manufacture the original failure or perform the archive.
        with sqlite3.connect(out_path) as db:
            db.execute('UPDATE managed_control SET gate=1')
            db.execute("UPDATE outgoing_intent SET status='indeterminate',attempt_number=1,result_evidence=? WHERE request_id=?", (str(evidence),request))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES (?,1,'indeterminate',?,?,?)", (request,str(evidence),timestamp-10,timestamp))
            db.execute('UPDATE managed_control SET gate=0')
        coord_path = self.ui / "coordinator.sqlite3"
        with sqlite3.connect(coord_path) as db:
            db.execute("UPDATE dispatch SET status='unknown',result='indeterminate' WHERE id=?", (parent["dispatch_id"],))
            db.execute("UPDATE surface SET paused_reason='original Copy rejection'")
        configured = self.coordinator("configure", "--conversation-id", channel["id"], "--expected-epoch", channel["epoch"],
            "--owner", "owner_group", "--input", "push", "--reply-policy", "authorized")
        channel["epoch"] = configured["epoch"]
        cfg = self.root / "image-rejection-proof-config.json"; sender.private_create(cfg, config)
        package_src = ROOT / "packages/wx-agent/src"
        proof_driver = channel["state"] / "conversation-sender"
        proof_driver.write_text("#!" + sys.executable + "\n" +
            "import json,sys\nfrom pathlib import Path\nfrom unittest import mock\n" +
            "sys.path.insert(0," + repr(str(package_src)) + ")\nfrom wx_agent import macos_sender_driver as driver\n" +
            "if sys.argv[1:]==['--image-rejection-capabilities']:\n    sys.exit(driver.main(sys.argv[1:]))\n" +
            "if sys.argv[1:]!=['--image-rejection-preflight']:\n    sys.exit(9)\n" +
            "config=json.loads(Path(" + repr(str(cfg)) + ").read_text())\nconfig['ui_dir']=Path(config['ui_dir'])\n" +
            "with mock.patch.object(driver,'_helper_is_live',return_value=False), mock.patch.object(driver,'bridge',side_effect=AssertionError('no GUI allowed')):\n" +
            "    try:\n        proof=driver.image_rejection_preflight(json.load(sys.stdin),config)\n" +
            "    except (driver.Refused,driver.Uncertain,OSError,ValueError,KeyError,TypeError):\n        sys.exit(3)\n" +
            "print(json.dumps(proof))\n")
        proof_driver.chmod(0o700)
        with sqlite3.connect(out_path) as db:
            original_intent = db.execute('SELECT * FROM outgoing_intent WHERE request_id=?',(request,)).fetchall()
            original_attempt = db.execute('SELECT * FROM outgoing_attempt WHERE request_id=?',(request,)).fetchall()
        before = {name:(self.ui / name).read_bytes() for name in artifacts}
        command = ("dispatch", "archive-image-copy-rejection", "--dispatch-id", parent["dispatch_id"],
                   "--expected-attempt", 1, "--reason", "reviewed exact original raster Copy rejection")
        self.coordinator("dispatch", "abandon-unsent", "--dispatch-id", parent["dispatch_id"], "--expected-attempt", 1,
                         "--reason", "old before-prepare protocol must still refuse", okay=False)
        self.coordinator("dispatch", "archive-image-copy-rejection", "--dispatch-id", parent["dispatch_id"],
                         "--expected-attempt", 2, "--reason", "later attempts cannot use first-attempt archive", okay=False)
        submit_marker = self.ui / (tid + ".submit-attempted.json")
        submit_marker.touch(mode=0o600)
        self.coordinator(*command, okay=False); submit_marker.unlink()
        prepare_path = self.ui / (tid + ".prepare-visual.job.json")
        wrong_media = dict(media, sha256="0" * 64)
        sender.private_update(prepare_path, dict(prepare, media=wrong_media))
        self.coordinator(*command, okay=False)
        prepare_path.write_bytes(before[prepare_path.name]); prepare_path.chmod(0o600)
        # N5-style successful native preparation/submission cannot use this archive.
        report_path = self.ui / (tid + ".client-result.json")
        sender.private_update(report_path, dict(artifacts[report_path.name], stage="submit_clicked", media_prepared={}))
        self.coordinator(*command, okay=False); report_path.write_bytes(before[report_path.name]); report_path.chmod(0o600)
        result = self.coordinator(*command)
        self.assertEqual(result["result"], "image_prepare_rejected")
        self.assertEqual(result["outbox_status"], "indeterminate")
        with sqlite3.connect(out_path) as db:
            self.assertEqual(db.execute('SELECT * FROM outgoing_intent WHERE request_id=?',(request,)).fetchall(),original_intent)
            self.assertEqual(db.execute('SELECT * FROM outgoing_attempt WHERE request_id=?',(request,)).fetchall(),original_attempt)
        self.assertEqual(before,{name:(self.ui / name).read_bytes() for name in artifacts})
        with sqlite3.connect(coord_path) as db:
            self.assertEqual(db.execute('SELECT epoch FROM dispatch WHERE id=?',(parent["dispatch_id"],)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT paused_reason FROM surface').fetchone()[0],'original Copy rejection')
        self.coordinator("dispatch", "restore-queued", "--dispatch-id", caption["dispatch_id"],
            "--reason", "reauthorizing cannot release rejected image caption")
        new = self.queue("group", "independent-after-copy-rejection", batch_id="new-independent-batch", part_index=0)
        self.assertEqual(self.coordinator("dispatch", "claim", "--worker", "synthetic-rejection-check")["status"],"paused")
        self.coordinator("dispatch", "review", "--reason", "independent synthetic desktop safety review")
        claimed = self.coordinator("dispatch", "claim", "--worker", "synthetic-rejection-check")
        self.assertEqual(claimed["dispatch_id"],new["dispatch_id"])
        with sqlite3.connect(coord_path) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',(caption["dispatch_id"],)).fetchone(),
                             ('blocked','batch_predecessor_not_sent'))
        self.assertEqual(self.outbox("group", "show", "--request-id", "caption-of-rejected-image")["attempt_number"],0)
        self.assertFalse(self.log.exists(),"archive and claim must never invoke a send driver")

    def test_batch_prerequisite_requires_successful_send_instead_of_merely_done(self):
        coord_path = self.ui / "coordinator.sqlite3"
        for index, (operation, result, successful) in enumerate((
                ("send", "abandoned_pre_submit", False),
                ("send", "image_prepare_rejected", False),
                ("send", "text_button_guard_rejected", False),
                ("send", "focus_completed", False),
                ("focus", "ui_observed", False),
                ("send", "ui_observed", True),
                ("send", "recipient_confirmed", True))):
            with self.subTest(operation=operation, result=result):
                batch = "synthetic-prerequisite-" + str(index)
                parent = self.queue("group", "prerequisite-" + str(index), batch_id=batch, part_index=0)
                child = self.queue("group", "dependent-caption-" + str(index), batch_id=batch, part_index=1)
                # Model historical dispatch outcomes without invoking a driver.
                with sqlite3.connect(coord_path) as db:
                    db.execute("UPDATE dispatch SET status='done',operation=?,result=? WHERE id=?",
                               (operation,result,parent["dispatch_id"]))
                independent = None
                if not successful:
                    independent = self.queue("group", "independent-batch-" + str(index),
                        batch_id="independent-prerequisite-" + str(index), part_index=0)
                claimed = self.coordinator("dispatch", "claim", "--worker", "synthetic-prerequisite-check")
                expected = child if successful else independent
                self.assertEqual(claimed["dispatch_id"],expected["dispatch_id"])
                if not successful:
                    with sqlite3.connect(coord_path) as db:
                        self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',
                            (child["dispatch_id"],)).fetchone(),('blocked','batch_predecessor_not_sent'))
                with sqlite3.connect(coord_path) as db:
                    db.execute("UPDATE dispatch SET status='done',result='ui_observed' WHERE id=?",(expected["dispatch_id"],))
                    db.execute('UPDATE surface SET active_dispatch=NULL')
        self.assertFalse(self.log.exists(),"prerequisite checks never execute a driver")

    def test_image_setup_refusal_keeps_zero_attempts_without_pausing_other_conversation(self):
        from wx_agent import dispatch
        group = self.channels["group"]
        image = self.queue("group", "setup-unavailable-image", image=True, batch_id="setup-unavailable-batch", part_index=0)
        self.queue("group", "caption-waits-for-image-setup", batch_id="setup-unavailable-batch", part_index=1)
        self.queue("private", "unrelated-text-after-image-setup")
        driver = group["state"] / "conversation-sender"
        touched = self.root / "image-desktop-or-send-touched"
        driver.write_text("#!" + sys.executable + "\n" +
            "import json,os,sys\nfrom pathlib import Path\n" +
            "if sys.argv[1:]==['--dispatch-preflight']:\n" +
            "    result=({'ready':False,'reason_code':'image_verification_unavailable','desktop_touched':False} if os.environ.get('WX_PREFLIGHT_CONTENT_KIND')=='image' else {'ready':True})\n" +
            "    print(json.dumps(result));sys.exit(0)\n" +
            "Path(" + repr(str(touched)) + ").write_text('unexpected desktop or send')\nsys.exit(9)\n")
        driver.chmod(0o700)
        before = self.outbox("group", "show", "--request-id", "setup-unavailable-image")
        result = dispatch.drain(BINARY, self.ui)
        self.assertEqual(result["results"][0]["status"],"blocked")
        self.assertEqual(result["results"][0]["result"],"image_verification_unavailable")
        self.assertFalse(result["results"][0]["surface_paused"])
        self.assertEqual(self.outbox("group", "show", "--request-id", "setup-unavailable-image"),before)
        self.assertEqual(self.outbox("group", "show", "--request-id", "caption-waits-for-image-setup")["attempt_number"],0)
        with sqlite3.connect(group["state"] / "outgoing.sqlite3") as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outgoing_attempt WHERE request_id=?',("setup-unavailable-image",)).fetchone()[0],0)
        status = self.coordinator("dispatch", "status")
        self.assertIsNone(status["paused_reason"])
        self.assertIsNone(status["active_dispatch"])
        saved = [row for row in status["dispatches"] if row["dispatch_id"]==image["dispatch_id"]][0]
        self.assertEqual((saved["status"],saved["result"]),("blocked","image_verification_unavailable"))
        self.assertEqual(self.outbox("private", "show", "--request-id", "unrelated-text-after-image-setup")["status"],"ui_observed")
        self.assertFalse(touched.exists())
        # A later drain cannot implicitly restore this blocked zero-attempt job.
        dispatch.drain(BINARY, self.ui)
        self.assertEqual(self.outbox("group", "show", "--request-id", "setup-unavailable-image"),before)
        self.assertFalse(touched.exists())

    def test_other_image_preflight_failure_still_pauses_shared_surface(self):
        from wx_agent import dispatch
        group = self.channels["group"]
        self.queue("group", "ordinary-image-readiness-failure", image=True)
        self.queue("private", "text-must-wait-for-desktop-review")
        driver = group["state"] / "conversation-sender"
        driver.write_text("#!" + sys.executable + "\nimport json,sys\n" +
            "if sys.argv[1:]==['--dispatch-preflight']:\n    print(json.dumps({'ready':False}));sys.exit(0)\nsys.exit(9)\n")
        driver.chmod(0o700)
        dispatch.drain(BINARY, self.ui)
        self.assertIsNotNone(self.coordinator("dispatch", "status")["paused_reason"])
        self.assertEqual(self.outbox("group", "show", "--request-id", "ordinary-image-readiness-failure")["attempt_number"],0)
        self.assertEqual(self.outbox("private", "show", "--request-id", "text-must-wait-for-desktop-review")["attempt_number"],0)

    def test_archive_private_text_button_rejection_keeps_original_caption_and_batch_history(self):
        from scripts.test_macos_sender_driver import TextButtonRejectionTests
        from wx_agent import macos_sender_driver as sender
        channel = self.channels["private"]
        batch = "original-private-image-caption"
        image = self.queue("private", "observed-parent-image", image=True, batch_id=batch, part_index=0)
        request = "original-private-caption-button-rejection"
        caption = self.queue("private", request, batch_id=batch, part_index=1)
        child = self.queue("private", "caption-dependent-followup", batch_id=batch, part_index=2)
        out_path = channel["state"] / "outgoing.sqlite3"
        with sqlite3.connect(out_path) as db:
            frozen_text = db.execute('SELECT text FROM outgoing_intent WHERE request_id=?',(request,)).fetchone()[0]
        # Model the already-observed image cursor and known original TEXT guard
        # failure as synthetic history; no send/GUI command creates this state.
        with sqlite3.connect(channel["state"] / "channel.sqlite3") as db:
            db.execute('UPDATE channel_binding SET encrypted_snapshot=1')
            latest = db.execute('SELECT COALESCE(MAX(cursor),0) FROM channel_events').fetchone()[0]
            for cursor in range(latest + 1, 15):
                message = {"sender": "synthetic_self" if cursor == 14 else "synthetic_member", "talker": channel["talker"],
                           "msg_type": 3 if cursor == 14 else 1, "create_time": int(time.time()) - 20,
                           "content": {"Image": {"md5": "a" * 32}} if cursor == 14 else {"Text": "fixture context"}}
                db.execute("INSERT INTO channel_events(kind,server_id,message_json) VALUES ('message_observed',?,?)",(1000+cursor,json.dumps(message)))
        binding = self.cli("channel",channel["state"],"status")["binding"]
        native_root = self.root / "text-button-native-fixture"; native_root.mkdir(mode=0o700)
        old_request, config, old_tid = TextButtonRejectionTests().fixture(native_root)
        tid = "send-" + hashlib.sha256((binding["channel_id"] + "\0" + request).encode()).hexdigest()[:48]
        mapping = {old_request["request_id"]: request, old_request["channel_id"]: binding["channel_id"],
                   old_request["session_id"]: binding["session_id"], old_request["account"]: binding["account"],
                   old_request["talker"]: binding["talker"], old_request["text"]: frozen_text}
        def rewrite(value):
            if isinstance(value, dict): return {k:rewrite(v) for k,v in value.items()}
            if isinstance(value, list): return [rewrite(v) for v in value]
            if isinstance(value, str): return mapping.get(value, value.replace(old_tid,tid))
            return value
        artifacts = []
        for path in native_root.iterdir():
            if not path.name.startswith(old_tid): continue
            target = self.ui / path.name.replace(old_tid,tid)
            if path.suffix == ".png":
                target.write_bytes(path.read_bytes()); target.chmod(0o600)
            else:
                sender.private_create(target,rewrite(json.loads(path.read_bytes())) if path.stat().st_size else {})
                if not path.stat().st_size: target.write_bytes(b'')
            artifacts.append(target)
        config.update(ui_dir=str(self.ui),channel_state_dir=str(channel["state"]),wx_cli=str(BINARY),binding=binding)
        cfg = self.root / "text-button-proof-config.json"; sender.private_create(cfg,config)
        evidence = self.ui / (tid + ".client-result.json")
        timestamp = int(time.time())
        with sqlite3.connect(out_path) as db:
            db.execute('UPDATE managed_control SET gate=1')
            db.execute("UPDATE outgoing_intent SET status='ui_observed',attempt_number=1,result_evidence='synthetic parent image cursor 14' WHERE request_id='observed-parent-image'")
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES ('observed-parent-image',1,'indeterminate','synthetic parent image cursor 14',?,?)",(timestamp-20,timestamp-18))
            db.execute("UPDATE outgoing_intent SET status='indeterminate',attempt_number=1,result_evidence=? WHERE request_id=?",(str(evidence),request))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,result_evidence,claimed_at,finished_at) VALUES (?,1,'indeterminate',?,?,?)",(request,str(evidence),timestamp-10,timestamp))
            db.execute('UPDATE managed_control SET gate=0')
        coord_path = self.ui / "coordinator.sqlite3"
        with sqlite3.connect(coord_path) as db:
            db.execute("UPDATE dispatch SET status='done',result='ui_observed' WHERE id=?",(image["dispatch_id"],))
            db.execute("UPDATE dispatch SET status='unknown',result='indeterminate',token='original-token',worker='original-worker',lease_until=? WHERE id=?",(timestamp-1,caption["dispatch_id"]))
            db.execute("UPDATE surface SET paused_reason='original private text button guard'")
        configured = self.coordinator("configure","--conversation-id",channel["id"],"--expected-epoch",channel["epoch"],
            "--owner","owner_private","--input","push","--reply-policy","authorized")
        channel["epoch"] = configured["epoch"]
        proof_driver = channel["state"] / "conversation-sender"
        proof_driver.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\nfrom unittest import mock\n" +
            "sys.path.insert(0," + repr(str(ROOT / "packages/wx-agent/src")) + ")\nfrom wx_agent import macos_sender_driver as driver\n" +
            "if sys.argv[1:]==['--text-button-rejection-capabilities']:\n    sys.exit(driver.main(sys.argv[1:]))\n" +
            "if sys.argv[1:]!=['--text-button-rejection-preflight']:\n    sys.exit(9)\n" +
            "config=json.loads(Path(" + repr(str(cfg)) + ").read_text());config['ui_dir']=Path(config['ui_dir'])\n" +
            "with mock.patch.object(driver,'_helper_is_live',return_value=False), mock.patch.object(driver,'bridge',side_effect=AssertionError('no GUI copy or send')):\n" +
            "    try:\n        proof=driver.text_button_rejection_preflight(json.load(sys.stdin),config)\n" +
            "    except (driver.Refused,driver.Uncertain,OSError,ValueError,KeyError,TypeError):\n        sys.exit(3)\nprint(json.dumps(proof))\n")
        proof_driver.chmod(0o700)
        with sqlite3.connect(out_path) as db:
            original_intents = db.execute('SELECT * FROM outgoing_intent ORDER BY request_id').fetchall()
            original_attempts = db.execute('SELECT * FROM outgoing_attempt ORDER BY request_id,attempt_number').fetchall()
        before = {p.name:p.read_bytes() for p in artifacts}
        command = ("dispatch","archive-text-button-rejection","--dispatch-id",caption["dispatch_id"],
                   "--expected-attempt",1,"--reason","reviewed exact first text button guard rejection")
        self.coordinator("dispatch","abandon-unsent","--dispatch-id",caption["dispatch_id"],"--expected-attempt",1,
                         "--reason","prepared text guard is outside existing abandon mode",okay=False)
        for suffix, update in ((".submit-visual.result.json", {"error":"Send button not found"}),
                               (".submit-visual.job.json", {"text":"changed frozen body"}),
                               (".visual-state.json", {"stage":"submit-attempted"})):
            path = self.ui / (tid + suffix)
            sender.private_update(path,dict(sender.private_read(path),**update))
            self.coordinator(*command,okay=False)
            path.write_bytes(before[path.name]);path.chmod(0o600)
        missing = self.ui / (tid + ".prepare-visual.result.json")
        missing.unlink(); self.coordinator(*command,okay=False)
        missing.write_bytes(before[missing.name]);missing.chmod(0o600)
        marker = self.ui / (tid + ".submit-attempted.json"); marker.touch(mode=0o600)
        self.coordinator(*command,okay=False);marker.unlink()
        extra = self.ui / (tid + "-a2.client-result.json");sender.private_create(extra,{})
        self.coordinator(*command,okay=False);extra.unlink()
        for name in ("executor.lock","sender.lock"):
            path = self.ui / name;path.touch(mode=0o600,exist_ok=True)
            with path.open() as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.coordinator(*command,okay=False)
        result = self.coordinator(*command)
        self.assertEqual((result["status"],result["result"],result["outbox_status"]),("done","text_button_guard_rejected","indeterminate"))
        self.coordinator(*command,okay=False)
        with sqlite3.connect(out_path) as db:
            self.assertEqual(db.execute('SELECT * FROM outgoing_intent ORDER BY request_id').fetchall(),original_intents)
            self.assertEqual(db.execute('SELECT * FROM outgoing_attempt ORDER BY request_id,attempt_number').fetchall(),original_attempts)
        self.assertEqual(before,{p.name:p.read_bytes() for p in artifacts})
        with sqlite3.connect(coord_path) as db:
            self.assertEqual(db.execute('SELECT epoch,token,worker,lease_until FROM dispatch WHERE id=?',(caption["dispatch_id"],)).fetchone(),
                             (1,'original-token','original-worker',timestamp-1))
            self.assertEqual(db.execute('SELECT paused_reason FROM surface').fetchone()[0],'original private text button guard')
        self.coordinator("dispatch","restore-queued","--dispatch-id",child["dispatch_id"],"--reason","review must not convert text archive into batch success")
        fresh = self.queue("private","new-independent-private-message",batch_id="new-private-business-message",part_index=0)
        self.assertEqual(self.coordinator("dispatch","claim","--worker","text-button-check")["status"],"paused")
        self.coordinator("dispatch","review","--reason","explicit synthetic private desktop review")
        claim = self.coordinator("dispatch","claim","--worker","text-button-check")
        self.assertEqual(claim["dispatch_id"],fresh["dispatch_id"])
        with sqlite3.connect(coord_path) as db:
            self.assertEqual(db.execute('SELECT status,result FROM dispatch WHERE id=?',(child["dispatch_id"],)).fetchone(),
                             ('blocked','batch_predecessor_not_sent'))
        self.assertFalse(self.log.exists(),"archiving and claiming must not execute a send driver")

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
