#!/usr/bin/env python3
"""MCP WeChat tools against compiled Rust journals, using only synthetic source data.

No provider, desktop driver, live account or production runtime is used.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from scripts import test_application_integration as app_fixture

OWNER = "synthetic_app_a_owner"


class McpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not app_fixture.BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def setUp(self):
        self.fixture = app_fixture.ApplicationIntegrationTests("test_two_apps_concurrent_enqueue_original_ids_and_no_gui")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.ui = self.fixture.ui
        self.cids = self.fixture.cids
        self.fixture.cli("coordinator", self.ui, "dispatch", "pause", "--reason", "synthetic mcp test")
        self.server = subprocess.Popen(
            [sys.executable, "-m", "wx_agent.mcp_server", "--config",
             str(self.root / "app_a" / "conversations-config.json"), "--owner", OWNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=self.root, env=self.fixture.env)
        self.addCleanup(self.stop)
        self.next_id = 0
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "synthetic", "version": "0"}})
        self.assertEqual(init["serverInfo"]["name"], "wx-agent-wechat")
        self.server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.server.stdin.flush()

    def stop(self):
        if self.server.poll() is None:
            self.server.stdin.close()
            self.server.wait(timeout=10)
        self.server.stdout.close()
        self.server.stderr.close()

    def rpc(self, method, params=None):
        self.next_id += 1
        self.server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                            "params": params or {}}) + "\n")
        self.server.stdin.flush()
        line = self.server.stdout.readline()
        self.assertTrue(line, self.server.stderr.read() if self.server.poll() is not None else "no response")
        response = json.loads(line)
        self.assertEqual(response["id"], self.next_id)
        self.assertNotIn("error", response)
        return response["result"]

    def tool(self, name, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"]
        self.assertNotIn("token", text.lower().replace("token_", ""))
        return result, (json.loads(text) if not result["isError"] else text)

    def seed(self, app, server_id, text):
        cid = self.cids[app]
        state = Path(self.fixture.clients[app].conversations[cid]["state_dir"])
        source = self.root / app / "synthetic" / "message" / "message_0.db"
        talker = "synthetic_group@chatroom" if app == "app_a" else "synthetic_friend"
        table = "Msg_" + hashlib.md5(talker.encode()).hexdigest()
        with sqlite3.connect(source) as db:
            db.execute(f"INSERT INTO [{table}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)",
                       (server_id, server_id + 100, 1700000000 + server_id,
                        ("synthetic_member:\n" + text).encode()))
        with sqlite3.connect(state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.fixture.cli("channel", state, "sync")
        self.fixture.cli("coordinator", self.ui, "inbox", "reconcile", "--conversation-id", cid)
        owner = "synthetic_" + app + "_owner"
        return self.fixture.cli("coordinator", self.ui, "inbox", "claim", "--conversation-id", cid,
                                "--owner", owner)

    def outbox(self, app, request_id):
        cid = self.cids[app]
        state = self.fixture.clients[app].conversations[cid]["state_dir"]
        return self.fixture.cli("outbox", state, "show", "--request-id", request_id)

    def test_tools_are_listed_with_schemas(self):
        names = [tool["name"] for tool in self.rpc("tools/list")["tools"]]
        self.assertEqual(names, ["wechat_pending", "wechat_read", "wechat_memory", "wechat_decide", "wechat_reply"])

    def test_read_and_reply_queue_a_managed_intent_and_close_the_delivery(self):
        claim = self.seed("app_a", 40, "When is the review?")
        self.seed("app_b", 41, "Other owner question")
        _, pending = self.tool("wechat_pending")
        self.assertEqual([(d["delivery_id"], d["conversation_id"], d["reply_policy"]) for d in pending["deliveries"]],
                         [(claim["delivery_id"], self.cids["app_a"], "authorized")])
        _, read = self.tool("wechat_read", delivery_id=claim["delivery_id"])
        question = [e for e in read["events"] if e["replyable"]]
        self.assertEqual([e["text"] for e in question], ["When is the review?"])
        self.assertFalse(read["has_more"])
        self.assertNotIn("conversation_notes", read)
        _, notes = self.tool("wechat_memory", conversation_id=self.cids["app_a"])
        self.assertEqual(notes["memory"]["revision"], 0)
        result, replied = self.tool("wechat_reply", delivery_id=claim["delivery_id"],
                                    cursor=question[0]["cursor"], text="Tomorrow at 10.")
        self.assertFalse(result["isError"], replied)
        self.assertEqual((replied["status"], replied["send_status"]), ("replied", "pending"))
        intent = self.outbox("app_a", replied["request_id"])
        self.assertEqual((intent["status"], intent["text"], intent["attempt_number"]),
                         ("managed_queued", "Tomorrow at 10.", 0))
        dispatches = self.fixture.cli("coordinator", self.ui, "dispatch", "status")["dispatches"]
        self.assertEqual([d["status"] for d in dispatches if d["request_id"] == replied["request_id"]], ["queued"])
        again, message = self.tool("wechat_reply", delivery_id=claim["delivery_id"],
                                   cursor=question[0]["cursor"], text="Tomorrow at 10.")
        self.assertTrue(again["isError"])
        self.assertIn("not currently claimed", message)
        self.assertFalse((self.root / "driver-events.jsonl").exists())

    def test_first_page_carries_verified_notes(self):
        from wx_agent import memory
        fact = self.seed("app_a", 45, "Review moved to 10:00 in room 3.")
        state = self.fixture.clients["app_a"].conversations[self.cids["app_a"]]["state_dir"]
        binding = self.fixture.cli("channel", state, "status")["binding"]
        wrapper = str(self.fixture.wrapper)
        memory.update_memory(state, wrapper, {
            "version": 1, "binding": {k: binding[k] for k in ("channel_id", "session_id", "account", "talker")},
            "summary": "Review logistics.", "summary_source_cursors": [fact["cursor_end"]],
            "confirmed": [{"text": "Review is at 10:00 in room 3.", "source_cursors": [fact["cursor_end"]]}],
            "open_questions": []}, 0)
        _, first = self.tool("wechat_read", delivery_id=fact["delivery_id"])
        self.assertEqual(first["conversation_notes"]["confirmed"][0]["text"], "Review is at 10:00 in room 3.")
        _, later = self.tool("wechat_read", delivery_id=fact["delivery_id"], after=fact["cursor_end"])
        self.assertNotIn("conversation_notes", later)

    def test_other_owner_delivery_is_refused(self):
        other = self.seed("app_b", 42, "Other owner question")
        for name, arguments in (("wechat_read", {"delivery_id": other["delivery_id"]}),
                                ("wechat_decide", {"delivery_id": other["delivery_id"], "decision": "skip",
                                                   "reason": "not mine"}),
                                ("wechat_memory", {"conversation_id": self.cids["app_b"]})):
            result, message = self.tool(name, **arguments)
            self.assertTrue(result["isError"], (name, message))
        status = self.fixture.cli("coordinator", self.ui, "inbox", "status", "--conversation-id", self.cids["app_b"])
        self.assertEqual([d["status"] for d in status["deliveries"]], ["claimed"])

    def test_draft_policy_refuses_reply_but_records_draft(self):
        binding = self.fixture.cli("coordinator", self.ui, "status", "--conversation-id", self.cids["app_a"])
        self.fixture.cli("coordinator", self.ui, "configure", "--conversation-id", self.cids["app_a"],
                         "--expected-epoch", binding["epoch"], "--owner", OWNER, "--input", "push",
                         "--reply-policy", "draft")
        claim = self.seed("app_a", 43, "Can you confirm the budget?")
        _, read = self.tool("wechat_read", delivery_id=claim["delivery_id"])
        cursor = [e for e in read["events"] if e["replyable"]][0]["cursor"]
        refused, message = self.tool("wechat_reply", delivery_id=claim["delivery_id"], cursor=cursor, text="Yes.")
        self.assertTrue(refused["isError"])
        self.assertIn("draft", message)
        _, drafted = self.tool("wechat_decide", delivery_id=claim["delivery_id"], decision="draft",
                               reason="budget needs owner approval", draft_text="Budget confirmed.")
        self.assertEqual(drafted["status"], "draft")
        self.assertEqual(self.fixture.cli("coordinator", self.ui, "dispatch", "status")["dispatches"], [])

    def test_invalid_arguments_are_rejected_without_state_change(self):
        claim = self.seed("app_a", 44, "Hello")
        for arguments in ({"delivery_id": claim["delivery_id"], "decision": "reply", "reason": "x"},
                          {"delivery_id": claim["delivery_id"], "decision": "skip", "reason": "x", "draft_text": "y"},
                          {"delivery_id": claim["delivery_id"], "decision": "skip", "reason": "x", "extra": 1},
                          {"delivery_id": claim["delivery_id"], "decision": "skip", "reason": " "}):
            result, _ = self.tool("wechat_decide", **arguments)
            self.assertTrue(result["isError"], arguments)
        _, skipped = self.tool("wechat_decide", delivery_id=claim["delivery_id"], decision="skip",
                               reason="greeting only")
        self.assertEqual(skipped["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
