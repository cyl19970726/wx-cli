#!/usr/bin/env python3
"""Gateway and client through real Rust journals, using only synthetic source data."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from scripts import test_application_integration as app_fixture
from scripts.test_local_channel import TABLE
from wx_agent.gateway_client import GatewayClient


class GatewayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not app_fixture.BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def setUp(self):
        # Compose its setup only; none of the application suite's tests run here.
        self.fixture = app_fixture.ApplicationIntegrationTests("test_two_apps_concurrent_enqueue_original_ids_and_no_gui")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.policy = self.root / "gateway-policy.json"
        grants = {}
        self.clients = {}
        gateway_executable = self.root / "synthetic-gateway"
        gateway_executable.write_text("#!" + sys.executable + "\n"
            "import importlib.abc,sys\n"
            "class DenyProvider(importlib.abc.MetaPathFinder):\n"
            " def find_spec(self,name,path=None,target=None):\n"
            "  if name in ('wx_agent.session_adapter','wx_agent.integrations.codex','wx_agent.codex_plugin'): raise AssertionError(name)\n"
            "sys.meta_path.insert(0,DenyProvider())\n"
            "from wx_agent.gateway import main\nraise SystemExit(main())\n")
        gateway_executable.chmod(0o700)
        for app in ("app_a", "app_b"):
            cid = self.fixture.cids[app]
            token = "synthetic-token-for-" + app
            grants[app] = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                           "client_config": str(self.root / app / "application.json"),
                           "permissions": ["send", "result", "inbox"],
                           "session_bindings": {cid: {"session_ref": "codex:synthetic:" + app,
                                                       "thread_id": "thread-" + app}}}
            config = self.root / app / "gateway-client.json"
            config.write_text(json.dumps({"version": 1, "application_id": app, "token": token,
                                          "command": [str(gateway_executable), "--config", str(self.policy), "request"]}))
            config.chmod(0o600)
            self.clients[app] = GatewayClient(config)
        self.policy.write_text(json.dumps({"version": 1, "applications": grants}))
        self.policy.chmod(0o600)
        self.fixture.cli("coordinator", self.fixture.ui, "dispatch", "pause", "--reason", "synthetic gateway test")

    def test_two_apps_queue_and_one_inbox_turn_without_gui_or_provider(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            sent = list(pool.map(lambda app: self.clients[app].request("send_text", {
                "conversation_id": self.fixture.cids[app], "request_id": "notice-001",
                "text": "synthetic notice"}), ("app_a", "app_b")))
        self.assertEqual({item["request_id"] for item in sent}, {"app_a.notice-001", "app_b.notice-001"})
        self.assertEqual({item["status"] for item in sent}, {"pending"})
        for app in ("app_a", "app_b"):
            result = self.clients[app].request("result", {"conversation_id": self.fixture.cids[app],
                                                             "request_id": "notice-001"})
            self.assertEqual((result["status"], result["attempt_number"]), ("managed_queued", 0))
        self.assertFalse((self.root / "driver-events.jsonl").exists())

        cid = self.fixture.cids["app_a"]
        source = self.root / "app_a" / "synthetic" / "message" / "message_0.db"
        with sqlite3.connect(source) as db:
            db.execute(f"INSERT INTO [{TABLE}] VALUES (30, 130, 1, 1, 1700000030, ?, NULL, 0)",
                       (b'synthetic_member:\nnew inbox message',))
        state = self.fixture.clients["app_a"].conversations[cid]["state_dir"]
        with sqlite3.connect(state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.fixture.cli("channel", state, "sync")
        reconciled = self.fixture.cli("coordinator", self.fixture.ui, "inbox", "reconcile",
                                      "--conversation-id", cid)
        self.assertEqual(reconciled["created"], 1)
        client = self.clients["app_a"]
        binding = client.request("binding", {"conversation_id": cid})
        self.assertEqual(binding["owner"], "synthetic_app_a_owner")
        self.assertNotIn("channel_state_dir", binding)
        status = client.request("inbox_status", {"conversation_id": cid})
        self.assertEqual(status["deliveries"][0]["status"], "pending")
        client_config = self.root / "app_a" / "gateway-client.json"
        cli = subprocess.run([sys.executable, "-m", "wx_agent.gateway_client", "--config",
                              str(client_config), "inbox_status", "--params-stdin"],
                             input=json.dumps({"conversation_id": cid}), capture_output=True, text=True,
                             env=self.fixture.env, timeout=20)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["result"]["deliveries"][0]["status"], "pending")
        claim = client.request("inbox_claim", {"conversation_id": cid})
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("channel_state_dir", claim)
        delivery = claim["delivery_id"]
        token = claim["token"]
        shown = client.request("inbox_show", {"conversation_id": cid, "delivery_id": delivery})
        self.assertEqual(shown["token"], token)
        events = client.request("events", {"conversation_id": cid, "delivery_id": delivery})
        self.assertEqual(events["events"][0]["direction"], "incoming")
        reserved = client.request("reserve_turn", {"conversation_id": cid, "delivery_id": delivery, "token": token})
        self.assertEqual(reserved["session_ref"], "codex:synthetic:app_a")
        recorded = client.request("record_turn", {"conversation_id": cid, "delivery_id": delivery,
                                                    "token": token, "turn_id": "turn-synthetic"})
        self.assertEqual(recorded["thread_id"], "thread-app_a")
        terminal = client.request("finish_turn", {"conversation_id": cid, "delivery_id": delivery,
                                                    "token": token, "status": "completed"})
        self.assertEqual(terminal["turn_state"], "completed")
        completed = client.request("complete", {"conversation_id": cid, "delivery_id": delivery,
                                                  "token": token, "decision": "skip"})
        self.assertEqual(completed["status"], "skipped")
        other = self.fixture.cids["app_b"]
        self.fixture.cli("coordinator", self.fixture.ui, "configure", "--conversation-id", other,
                         "--expected-epoch", 1, "--owner", "successor_owner", "--input", "pull",
                         "--reply-policy", "observe")
        old_result = self.clients["app_b"].request("result", {"conversation_id": other,
                                                                  "request_id": "notice-001"})
        self.assertEqual((old_result["status"], old_result["attempt_number"]), ("managed_queued", 0))
        self.assertFalse((self.root / "driver-events.jsonl").exists())


    def test_old_grant_finishes_recorded_turn_and_rust_rehomes_undecided_delivery(self):
        cid = self.fixture.cids["app_a"]
        state = self.fixture.clients["app_a"].conversations[cid]["state_dir"]
        source = self.root / "app_a" / "synthetic" / "message" / "message_0.db"
        with sqlite3.connect(source) as db:
            db.execute(f"INSERT INTO [{TABLE}] VALUES (30, 131, 1, 1, 1700000030, ?, NULL, 0)",
                       (b'synthetic_member:\nturn before takeover',))
        with sqlite3.connect(state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.fixture.cli("channel", state, "sync")
        self.fixture.cli("coordinator", self.fixture.ui, "inbox", "reconcile", "--conversation-id", cid)
        client = self.clients["app_a"]
        claim = client.request("inbox_claim", {"conversation_id": cid})
        delivery, token = claim["delivery_id"], claim["token"]
        client.request("reserve_turn", {"conversation_id": cid, "delivery_id": delivery, "token": token})
        self.fixture.cli("coordinator", self.fixture.ui, "configure", "--conversation-id", cid,
                         "--expected-epoch", 1, "--owner", "successor_owner", "--input", "pull",
                         "--reply-policy", "observe")
        records = client.request("transport_status", {"conversation_id": cid})["deliveries"]
        self.assertEqual([(row["delivery_id"], row["transport_token"]) for row in records], [(delivery, token)])
        self.assertEqual(records[0]["turn_state"], "reserved")
        recorded = client.request("record_turn", {"conversation_id": cid, "delivery_id": delivery,
                                                   "token": token, "turn_id": "old-turn"})
        self.assertEqual(recorded["thread_id"], "thread-app_a")
        finished = client.request("finish_turn", {"conversation_id": cid, "delivery_id": delivery,
                                                   "token": token, "status": "completed"})
        self.assertEqual(finished["turn_state"], "completed")
        current = self.fixture.cli("coordinator", self.fixture.ui, "inbox", "status", "--conversation-id", cid)
        row = next(item for item in current["deliveries"] if item["delivery_id"] == delivery)
        self.assertEqual((row["status"], row["owner"], row["epoch"], row["turn_state"]),
                         ("pending", "successor_owner", 2, None))
        self.assertEqual(client.request("transport_status", {"conversation_id": cid})["deliveries"], [])



if __name__ == "__main__":
    unittest.main()
