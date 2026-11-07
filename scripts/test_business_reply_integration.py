#!/usr/bin/env python3
"""Synthetic native queue replies through compiled Rust and the real scoped Gateway.

No provider, desktop driver, live account, SSH, or production runtime is used.
"""
import hashlib
import json
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
import unittest

from scripts import test_gateway_integration as gateway_fixture
from scripts.test_queue_snapshot_integration import FakeNative
from wx_agent.queued_snapshot import QueuedSnapshotPlugin, read_private, reader_command


class BusinessReplyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_fixture.GatewayIntegrationTests.setUpClass()

    def setUp(self):
        # Reuse setup, never inherit or run the older test methods.
        self.gateway = gateway_fixture.GatewayIntegrationTests(
            "test_two_apps_queue_and_one_inbox_turn_without_gui_or_provider")
        self.gateway.setUp()
        self.addCleanup(self.gateway.doCleanups)
        self.fixture = self.gateway.fixture
        self.root = self.gateway.root
        self.ui = self.fixture.ui
        self.cids = self.fixture.cids
        self.session_id = "original-business-session"
        self.session_ref = "codex:" + socket.gethostname() + ":" + self.session_id
        self.client = self.gateway.clients["app_a"]
        self.native = FakeNative(self.root, self.session_id)
        self.runtime = self.root / "business-reply-runtime"
        self.runtime.mkdir(mode=0o700)
        self.reader = self.root / "wx-agent-queue-session"
        self.reader.write_text("#!" + sys.executable + "\nfrom wx_agent.queued_snapshot import main\nraise SystemExit(main())\n")
        self.reader.chmod(0o700)
        app_config = self.root / "app_a" / "application.json"
        app = json.loads(app_config.read_text())
        second = self.cids["app_b"]
        app["conversations"][second] = {"state_dir": str(self.fixture.clients["app_b"].conversations[second]["state_dir"]),
                                         "owner": "synthetic_app_b_owner", "expected_epoch": 1}
        app_config.write_text(json.dumps(app))
        app_config.chmod(0o600)
        policy = json.loads(self.gateway.policy.read_text())
        for cid in self.cids.values():
            policy["applications"]["app_a"]["session_bindings"][cid] = {
                "session_ref": self.session_ref, "thread_id": self.session_id}
            self.fixture.cli("coordinator", self.ui, "inbox", "authorize-shared-session",
                             "--session-ref", self.session_ref, "--conversation-id", cid,
                             "--owner", "synthetic_app_a_owner" if cid == self.cids["app_a"] else "synthetic_app_b_owner",
                             "--expected-epoch", "1", "--reason", "synthetic two target grant")
        self.gateway.policy.write_text(json.dumps(policy))
        self.gateway.policy.chmod(0o600)
        self.base = self.root / "business-base.json"
        self.base.write_text(json.dumps({"version": 1, "allow_shared_session": True,
            "conversation_config": str(self.root / "app_a" / "conversations-config.json"),
            "wx_agent": str(self.fixture.agent), "codex": str(self.reader),
            "gateway_config": str(self.root / "app_a" / "gateway-client.json"),
            "gateway_client": str(self.root / "synthetic-gateway"),
            "bindings": [{"session_id": self.session_id, "conversation_id": cid,
                          "owner": "synthetic_app_a_owner" if app == "app_a" else "synthetic_app_b_owner",
                          "expected_epoch": 1, "project": str(self.root),
                          "agent_host": socket.gethostname()} for app, cid in self.cids.items()]}))
        self.base.chmod(0o600)
        self.config = self.root / "business-queue.json"
        self.configure("authorized", 1700000050)

    def configure(self, policy, not_before=None, proposal_mode=None):
        config = {"version": 1, "base_config": str(self.base), "runtime_dir": str(self.runtime),
                  "snapshot_cli": str(self.reader), "reply_policy": policy}
        if not_before is not None:
            config["reply_not_before"] = not_before
        if proposal_mode is not None:
            config["proposal_mode"] = proposal_mode
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)

    def plugin(self):
        return QueuedSnapshotPlugin(self.config, native_factory=lambda _: self.native)

    def seed(self, app, *, stamp=1700000100, outgoing=False):
        cid = self.cids[app]
        state = Path(self.fixture.clients[app].conversations[cid]["state_dir"])
        source = self.root / app / "synthetic" / "message" / "message_0.db"
        talker = "synthetic_group@chatroom" if app == "app_a" else "synthetic_friend"
        table = "Msg_" + hashlib.md5(talker.encode()).hexdigest()
        with sqlite3.connect(source) as db:
            db.execute(f"INSERT INTO [{table}] VALUES (30, 130, 1, ?, ?, ?, NULL, 0)",
                       (1, stamp, b"synthetic_member:\nQuestion to us?"))
        with sqlite3.connect(state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.fixture.cli("channel", state, "sync")
        created = self.fixture.cli("coordinator", self.ui, "inbox", "reconcile", "--conversation-id", cid)
        if outgoing:
            # A synthetic frozen row changes direction after the inbox batch was
            # made, so the reply validator must reject it from canonical reads.
            with sqlite3.connect(state / "channel.sqlite3") as db:
                row = db.execute("SELECT cursor,message_json FROM channel_events WHERE server_id=130 ORDER BY cursor DESC LIMIT 1").fetchone()
                message = json.loads(row[1])
                message["sender"] = "synthetic_self"
                db.execute("UPDATE channel_events SET message_json=? WHERE cursor=?",
                           (json.dumps(message), row[0]))
        return created["created"]

    def note_for(self, cid):
        return next(read_private(path) for path in self.runtime.glob("*.transport.json")
                    if read_private(path)["conversation_id"] == cid)

    def native_final(self, note, decision, source_cursors=None):
        queued = next(item for item in self.native.queue_calls
                      if item["clientUserMessageId"] == note["client_id"])
        items = [{"type": "userMessage", "clientId": note["client_id"], "content": queued["input"]}]
        offset = 0
        while True:
            command = reader_command(self.reader, note, offset)
            done = subprocess.run(shlex.split(command), cwd=self.root, env=self.fixture.env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(done.returncode, 0, done.stderr)
            chunk = json.loads(done.stdout)
            items.append({"type": "commandExecution", "command": command, "cwd": str(self.root),
                          "status": "completed", "exitCode": 0, "aggregatedOutput": done.stdout})
            offset = chunk["next_offset"]
            if not chunk["has_more"]:
                break
        answer = {"protocol_version": 1, "ref": note["ref"], "snapshot_sha256": note["snapshot_sha256"],
                  "decision": decision, "reason": "synthetic addressed question reviewed"}
        if decision == "reply":
            answer.update(text="Known synthetic business fact.", addressed_to_us=True,
                          source_cursors=source_cursors)
        if note.get("proposal_mode") == "reader-cli":
            from wx_agent.queue_proposal import proposal_prefix, proposal_delimiter
            from wx_agent.queued_snapshot import PROPOSAL_READY
            semantics = {key: value for key, value in answer.items()
                         if key not in ("protocol_version", "ref", "snapshot_sha256")}
            delimiter = proposal_delimiter(note)
            command = (proposal_prefix(self.reader, note) + " <<'" + delimiter + "'\n"
                       + json.dumps(semantics, ensure_ascii=False, separators=(",", ":")) + "\n" + delimiter)
            done = subprocess.run(["/bin/zsh", "-c", command], cwd=self.root, env=self.fixture.env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(done.returncode, 0, done.stderr)
            items.append({"type": "commandExecution", "command": command, "cwd": str(self.root),
                          "status": "completed", "exitCode": 0, "aggregatedOutput": done.stdout})
            final_text = PROPOSAL_READY
        else:
            final_text = json.dumps(answer)
        items.append({"type": "agentMessage", "phase": "final_answer", "text": final_text})
        stamp = int(time.time())
        self.native.thread["turns"].append({"id": "native-" + note["ref"][:16], "status": "completed",
             "startedAt": stamp, "completedAt": stamp, "items": items})

    def result(self, cid, delivery):
        return self.client.request("result", {"conversation_id": cid,
            "request_id": "reply-" + cid + "-" + str(delivery)})

    def mark_first_attempt_observed(self, app, delivery):
        cid = self.cids[app]
        state = Path(self.fixture.clients[app].conversations[cid]["state_dir"])
        request = "app_a.reply-" + cid + "-" + str(delivery)
        now = int(time.time())
        with sqlite3.connect(state / "outgoing.sqlite3") as db:
            db.execute("UPDATE managed_control SET gate=1")
            db.execute("UPDATE outgoing_intent SET status='ui_observed',attempt_number=1,updated_at=? WHERE request_id=?",
                       (now, request))
            db.execute("INSERT INTO outgoing_attempt(request_id,attempt_number,status,claimed_at,finished_at) "
                       "VALUES (?,1,'ui_observed',?,?)", (request, now, now))
            db.execute("UPDATE managed_control SET gate=0")
        self.assertEqual((self.result(cid, delivery)["status"], self.result(cid, delivery)["attempt_number"]),
                         ("ui_observed", 1))

    def test_authorized_reply_waits_on_original_key_then_completes_once(self):
        cid = self.cids["app_a"]
        self.assertEqual(self.seed("app_a"), 1)
        first = self.plugin().notify_once()
        self.assertEqual(first["notifications"][0]["status"], "queued_for_host", first)
        note = self.note_for(cid)
        payload = read_private(Path(note["snapshot_file"]))["payload"]
        source = payload["events"][-1]["cursor"]
        self.native_final(note, "reply", [source])
        calls = []
        worker = self.plugin()
        original = worker.client.request
        def counted(method, params):
            if method in ("send_text", "complete_observed_reply"):
                calls.append((method, dict(params)))
            return original(method, params)
        worker.client.request = counted
        pending = worker.reconcile()
        self.assertEqual(pending["notifications"][0]["reconciliations"][0]["status"], "outgoing_reply_pending", pending)
        self.assertEqual([method for method, _ in calls], ["send_text"])
        delivery = note["delivery_id"]
        self.assertEqual((self.result(cid, delivery)["status"], self.result(cid, delivery)["attempt_number"]),
                         ("managed_queued", 0))
        again = self.plugin().reconcile()
        self.assertEqual(again["notifications"][0]["reconciliations"][0]["status"], "outgoing_reply_pending", again)
        self.assertEqual(len(list(self.runtime.glob("*.reply-attempt.json"))), 1)
        self.mark_first_attempt_observed("app_a", delivery)
        finished = self.plugin().reconcile()
        self.assertEqual(finished["notifications"][0]["reconciliations"][0]["status"], "completed", finished)
        shown = self.client.request("inbox_show", {"conversation_id": cid, "delivery_id": delivery})
        self.assertEqual((shown["status"], shown["turn_state"], shown["request_id"]),
                         ("replied", "completed", "app_a.reply-" + cid + "-" + str(delivery)))
        with sqlite3.connect(Path(self.fixture.clients["app_a"].conversations[cid]["state_dir"]) / "outgoing.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outgoing_intent WHERE request_id=?",
                         (shown["request_id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM outgoing_attempt WHERE request_id=?",
                         (shown["request_id"],)).fetchone()[0], 1)
        self.assertFalse((self.root / "driver-events.jsonl").exists())

    def test_reader_cli_receipt_binds_identity_without_final_digest_copy(self):
        self.configure("authorized", 1700000050, proposal_mode="reader-cli")
        cid = self.cids["app_a"]
        self.assertEqual(self.seed("app_a"), 1)
        queued = self.plugin().notify_once()
        self.assertEqual(queued["notifications"][0]["status"], "queued_for_host", queued)
        note = self.note_for(cid)
        self.assertEqual(note["proposal_mode"], "reader-cli")
        payload = read_private(Path(note["snapshot_file"]))["payload"]
        self.assertEqual(payload["reply_constraints"], {
            "reply_not_before": 1700000050,
            "eligible_source_cursors": [payload["events"][-1]["cursor"]]})
        self.assertEqual(len(payload["events"]), payload["cursor_end"] - payload["cursor_start"] + 1)
        self.native_final(note, "reply", [payload["events"][-1]["cursor"]])
        self.assertEqual(self.native.thread["turns"][-1]["items"][-1]["text"], "WXCLI_PROPOSAL_READY")
        pending = self.plugin().reconcile()
        self.assertEqual(pending["notifications"][0]["reconciliations"][0]["status"], "outgoing_reply_pending", pending)
        self.mark_first_attempt_observed("app_a", note["delivery_id"])
        finished = self.plugin().reconcile()
        self.assertEqual(finished["notifications"][0]["reconciliations"][0]["status"], "completed", finished)

    def test_observe_and_old_or_outgoing_sources_never_create_intent(self):
        for mode in ("observe", "old", "outgoing"):
            with self.subTest(mode=mode):
                case = BusinessReplyIntegrationTests()
                case.setUp()
                try:
                    if mode == "observe":
                        case.configure("observe")
                    self.assertEqual(case.seed("app_a", stamp=1700000030 if mode == "old" else 1700000100,
                                               outgoing=mode == "outgoing"), 1)
                    case.plugin().notify_once()
                    note = case.note_for(case.cids["app_a"])
                    payload = read_private(Path(note["snapshot_file"]))["payload"]
                    if mode == "observe":
                        self.assertNotIn("reply_constraints", payload)
                    else:
                        self.assertEqual(payload["reply_constraints"]["eligible_source_cursors"], [])
                    case.native_final(note, "reply", [payload["events"][-1]["cursor"]])
                    result = case.plugin().reconcile()
                    self.assertEqual(result["notifications"][0]["status"], "requires_inspection", result)
                    with sqlite3.connect(Path(case.fixture.clients["app_a"].conversations[case.cids["app_a"]]["state_dir"]) / "outgoing.sqlite3") as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM outgoing_intent").fetchone()[0], 0)
                finally:
                    case.doCleanups()

    def test_two_explicit_targets_share_native_session_third_scope_denied(self):
        for app in ("app_a", "app_b"):
            self.assertEqual(self.seed(app), 1)
        first = self.plugin().notify_once()
        self.assertEqual(first["notifications"][0]["status"], "queued_for_host", first)
        first_note = self.note_for(self.cids["app_a"])
        self.native_final(first_note, "skip")
        finished = self.plugin().reconcile()
        self.assertEqual(finished["notifications"][0]["reconciliations"][0]["status"], "completed", finished)
        second = self.plugin().notify_once()
        self.assertEqual(next(row["status"] for row in second["notifications"]
                              if row["conversation_id"] == self.cids["app_b"]), "queued_for_host", second)
        notes = [read_private(path) for path in self.runtime.glob("*.transport.json")]
        self.assertEqual({note["thread_id"] for note in notes}, {self.session_id})
        self.assertEqual({note["session_ref"] for note in notes}, {self.session_ref})
        self.assertEqual(len(self.native.queue_calls), 2)
        self.assertEqual({note["conversation_id"] for note in notes}, set(self.cids.values()))
        with self.assertRaises(Exception):
            self.client.request("binding", {"conversation_id": "c" * 32})
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shared_session_scope WHERE session_ref=?",
                                        (self.session_ref,)).fetchone()[0], 2)

    def test_active_second_route_blocks_new_first_route_across_cycles(self):
        second_cid = self.cids["app_b"]
        first_cid = self.cids["app_a"]
        self.assertEqual(self.seed("app_b"), 1)
        first_cycle = self.plugin().notify_once()
        self.assertEqual([row["status"] for row in first_cycle["notifications"]],
                         ["queued_for_host", "original_transport_pending"], first_cycle)
        second_note = self.note_for(second_cid)
        second_shown = self.client.request("inbox_show", {"conversation_id": second_cid,
                                                          "delivery_id": second_note["delivery_id"]})
        self.assertEqual((second_shown["status"], second_shown["turn_state"]), ("claimed", "reserved"))
        self.assertEqual(len(self.native.queue_calls), 1)

        self.assertEqual(self.seed("app_a"), 1)
        self.native_final(second_note, "skip")
        next_cycle = self.plugin().notify_once()
        self.assertEqual([row["conversation_id"] for row in next_cycle["notifications"]],
                         [second_cid, first_cid], next_cycle)
        self.assertEqual([row["status"] for row in next_cycle["notifications"]],
                         ["original_transport_pending", "original_transport_pending"], next_cycle)
        self.assertEqual(next_cycle["notifications"][0]["reconciliations"][0]["status"], "completed")
        self.assertEqual(len(self.native.queue_calls), 1)
        pending = self.client.request("inbox_status", {"conversation_id": first_cid})["deliveries"]
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["status"], pending[0]["turn_state"]), ("pending", None))
        self.assertEqual(len(list(self.runtime.glob("*.transport.json"))), 1)
        self.assertEqual(self.note_for(second_cid), second_note)
        later = self.plugin().notify_once()
        self.assertEqual(later["notifications"][0]["status"], "queued_for_host", later)
        self.assertEqual(len(self.native.queue_calls), 2)

    def test_oldest_shared_backlog_routes_second_before_first(self):
        second_cid = self.cids["app_b"]
        first_cid = self.cids["app_a"]
        self.assertEqual(self.seed("app_b"), 1)
        self.assertEqual(self.seed("app_a"), 1)
        queued = self.plugin().notify_once()
        self.assertEqual([row["conversation_id"] for row in queued["notifications"]],
                         [second_cid, first_cid], queued)
        self.assertEqual([row["status"] for row in queued["notifications"]],
                         ["queued_for_host", "original_transport_pending"], queued)
        self.assertEqual(len(self.native.queue_calls), 1)
        self.assertEqual(self.note_for(second_cid)["thread_id"], self.session_id)
        first_rows = self.client.request("inbox_status", {"conversation_id": first_cid})["deliveries"]
        self.assertEqual((first_rows[0]["status"], first_rows[0]["turn_state"]), ("pending", None))


if __name__ == "__main__":
    unittest.main()
