#!/usr/bin/env python3
"""Queued snapshot through synthetic source, actual Gateway and compiled Rust.

The only fake is the native thread/queue surface. No host, network or GUI runs.
"""
import copy
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
from scripts.test_local_channel import TABLE
from wx_agent.queued_snapshot import QueuedSnapshotPlugin, read_private, reader_command


class FakeNative:
    def __init__(self, project, session_id):
        self.thread = {"id": session_id, "cwd": str(project), "turns": []}
        self.queue_calls = []
        self.unknown_receipt = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def request(self, method, params):
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.thread)}
        if method == "thread/queue/add":
            self.queue_calls.append(copy.deepcopy(params))
            if self.unknown_receipt:
                raise RuntimeError("synthetic unknown queue receipt")
            return {"queuedSubmission": {"clientUserMessageId": params["clientUserMessageId"]}}
        raise AssertionError(method)


class SnapshotGatewayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_fixture.GatewayIntegrationTests.setUpClass()

    def setUp(self):
        self.gateway = gateway_fixture.GatewayIntegrationTests("test_two_apps_queue_and_one_inbox_turn_without_gui_or_provider")
        self.gateway.setUp()
        self.addCleanup(self.gateway.doCleanups)
        self.root = self.gateway.root
        self.app = "app_a"
        self.cid = self.gateway.fixture.cids[self.app]
        self.session_id = "snapshot-synthetic-session"
        self.runtime = self.root / "snapshot-runtime"
        self.runtime.mkdir(mode=0o700)
        self.reader = self.root / "wx-agent-queue-session"
        self.reader.write_text("#!" + sys.executable + "\n"
                               "from wx_agent.queued_snapshot import main\n"
                               "raise SystemExit(main())\n")
        self.reader.chmod(0o700)
        grant = json.loads(self.gateway.policy.read_text())
        grant["applications"][self.app]["session_bindings"][self.cid] = {
            "session_ref": "codex:" + socket.gethostname() + ":" + self.session_id,
            "thread_id": self.session_id}
        self.gateway.policy.write_text(json.dumps(grant))
        self.base = self.root / "snapshot-base.json"
        self.base.write_text(json.dumps({
            "version": 1,
            "conversation_config": str(self.root / self.app / "conversations-config.json"),
            "wx_agent": str(self.gateway.fixture.agent),
            "codex": str(self.reader),
            "gateway_config": str(self.root / self.app / "gateway-client.json"),
            "gateway_client": str(self.root / "synthetic-gateway"),
            "bindings": [{"session_id": self.session_id, "conversation_id": self.cid,
                          "owner": "synthetic_app_a_owner", "expected_epoch": 1,
                          "project": str(self.root), "agent_host": socket.gethostname()}]}))
        self.base.chmod(0o600)
        self.config = self.root / "snapshot-queue.json"
        self.config.write_text(json.dumps({"version": 1, "base_config": str(self.base),
                                           "runtime_dir": str(self.runtime),
                                           "snapshot_cli": str(self.reader)}))
        self.config.chmod(0o600)
        self.native = FakeNative(self.root, self.session_id)
        self.plugin = self.new_plugin()

    def new_plugin(self):
        return QueuedSnapshotPlugin(self.config, native_factory=lambda _: self.native)

    def seed_frozen_delivery(self):
        source = self.root / self.app / "synthetic" / "message" / "message_0.db"
        with sqlite3.connect(source) as db:
            for number in range(110):
                body = "合成🙂" * 3000 if number == 0 else "synthetic incoming"
                db.execute(f"INSERT INTO [{TABLE}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)",
                           (30 + number, 300 + number, 1700000030 + number,
                            ("synthetic_member:\n" + body).encode()))
        state = self.gateway.fixture.clients[self.app].conversations[self.cid]["state_dir"]
        with sqlite3.connect(state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.gateway.fixture.cli("channel", state, "sync")
        result = self.gateway.fixture.cli("coordinator", self.gateway.fixture.ui, "inbox",
                                          "reconcile", "--conversation-id", self.cid)
        self.assertEqual(result["created"], 1)
        return state

    def execute_reader_turn(self, note, decision="skip"):
        self.assertEqual(len(self.native.queue_calls), 1)
        items = [{"type": "userMessage", "clientId": note["client_id"],
                  "content": self.native.queue_calls[0]["input"]}]
        offset = 0
        chunks = []
        while True:
            command = reader_command(self.reader, note, offset)
            done = subprocess.run(shlex.split(command), capture_output=True, text=True,
                                  env=self.gateway.fixture.env, timeout=20, cwd=self.root)
            self.assertEqual(done.returncode, 0, done.stderr)
            chunk = json.loads(done.stdout)
            chunks.append(chunk)
            self.assertEqual(chunk["ref"], note["ref"])
            items.append({"type": "commandExecution", "command": command,
                          "cwd": str(self.root), "status": "completed", "exitCode": 0,
                          "aggregatedOutput": done.stdout})
            offset = chunk["next_offset"]
            if not chunk["has_more"]:
                break
        answer = {"protocol_version": 1, "ref": note["ref"],
                  "snapshot_sha256": note["snapshot_sha256"], "decision": decision,
                  "reason": "synthetic reviewed frozen input"}
        items.append({"type": "agentMessage", "phase": "final_answer",
                      "text": json.dumps(answer)})
        stamp = int(time.time())
        self.native.thread["turns"] = [{"id": "synthetic-native-turn", "status": "completed",
                                        "startedAt": stamp, "completedAt": stamp,
                                        "items": items}]
        return chunks

    def test_actual_gateway_rust_paginated_snapshot_skip_and_finish(self):
        self.seed_frozen_delivery()
        pages = []
        original_request = self.plugin.client.request
        def count_actual_pages(method, params):
            if method == "events":
                pages.append((params["after"], params["limit"]))
            return original_request(method, params)
        self.plugin.client.request = count_actual_pages
        result = self.plugin.notify_once()
        self.assertEqual(result["notifications"][0]["status"], "queued_for_host", result)
        notes = list(self.runtime.glob("*.transport.json"))
        self.assertEqual(len(notes), 1)
        note = read_private(notes[0])
        self.assertEqual(note["conversation_id"], self.cid)
        shown = self.gateway.clients[self.app].request("inbox_show", {
            "conversation_id": self.cid, "delivery_id": note["delivery_id"]})
        self.assertEqual(shown["turn_state"], "reserved")
        self.assertGreater(shown["cursor_end"] - shown["cursor_start"], 100)
        self.assertEqual(pages, [(shown["cursor_start"] - 1, 100),
                                 (shown["cursor_start"] + 99, 100)])
        payload = read_private(Path(note["snapshot_file"]))["payload"]
        self.assertEqual(len(payload["events"]), shown["cursor_end"] - shown["cursor_start"] + 1)
        self.assertTrue(all(event["cursor"] == shown["cursor_start"] + index
                            for index, event in enumerate(payload["events"])))
        self.assertNotIn(shown["transport_token"], json.dumps(payload))
        chunks = self.execute_reader_turn(note)
        self.assertGreater(len(chunks), 1)
        reconciled = self.plugin.reconcile()
        self.assertEqual(reconciled["notifications"][0]["reconciliations"][0]["status"],
                         "completed", reconciled)
        final = self.gateway.clients[self.app].request("inbox_show", {
            "conversation_id": self.cid, "delivery_id": note["delivery_id"]})
        self.assertEqual((final["status"], final["turn_state"], final["turn_id"]),
                         ("skipped", "completed", "synthetic-native-turn"))
        with sqlite3.connect(self.gateway.fixture.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM delivery WHERE turn_id='synthetic-native-turn' AND turn_state='completed'").fetchone()[0], 1)
        self.assertFalse((self.root / "driver-events.jsonl").exists())
        self.assertFalse(any(item["status"] == "claimed" for item in
                             self.gateway.fixture.cli("coordinator", self.gateway.fixture.ui,
                                                      "dispatch", "status")["dispatches"]))

    def test_unknown_queue_receipt_refuses_duplicate_and_retains_rust_delivery(self):
        self.seed_frozen_delivery()
        self.native.unknown_receipt = True
        first = self.plugin.notify_once()
        self.assertEqual(first["notifications"][0]["status"], "requires_inspection", first)
        self.native.unknown_receipt = False
        second = self.new_plugin().notify_once()
        self.assertEqual(second["notifications"][0]["status"], "requires_inspection", second)
        self.assertEqual(len(self.native.queue_calls), 1)
        note = read_private(next(self.runtime.glob("*.transport.json")))
        shown = self.gateway.clients[self.app].request("inbox_show", {
            "conversation_id": self.cid, "delivery_id": note["delivery_id"]})
        self.assertEqual((shown["status"], shown["turn_state"]), ("claimed", "reserved"))
        self.assertFalse((self.root / "driver-events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
