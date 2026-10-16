#!/usr/bin/env python3
"""Offline contract tests for the Codex reply adapter; no real account is used."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("codex_reply_agent.py")
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
capture = pathlib.Path(os.environ['FAKE_CAPTURE'])
capture.write_text(json.dumps({'args':args,'prompt':prompt,'cwd':os.getcwd()}))
if os.environ.get('FAKE_FAIL'):
    sys.exit(9)
output = pathlib.Path(args[args.index('--output-last-message')+1])
output.write_text(os.environ.get('FAKE_RESULT', '{"skip":false,"text":"您好，请问具体需要什么？"}'))
'''


def request():
    return {
        "version": 1,
        "event": {"direction": "incoming", "channel_id": "private-channel-id",
                  "session_id": "private-session-id", "account": "private-account-id",
                  "talker": "private-group@chatroom", "server_id": 987654,
                  "message": {"sender": "private-wxid-A", "content": {"Text": "想做一个预约页面"}}},
        "recent_context": [
            {"sender": "private-wxid-B", "direction": "incoming", "text": "已有设计稿吗？"},
            {"sender": "private-wxid-A", "direction": "incoming", "text": "还没有"},
        ],
    }


class CodexReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "fake-codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o700)
        self.capture = self.root / "capture.json"

    def run_adapter(self, data=None, result=None, fail=False, model="gpt-6-astra"):
        env = dict(os.environ, FAKE_CAPTURE=str(self.capture))
        if result is not None:
            env["FAKE_RESULT"] = json.dumps(result, ensure_ascii=False)
        if fail:
            env["FAKE_FAIL"] = "1"
        cmd = [sys.executable, str(SCRIPT), "--codex", str(self.codex)]
        if model:
            cmd += ["--model", model]
        return subprocess.run(cmd, input=json.dumps(data if data is not None else request(), ensure_ascii=False),
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=5)

    def test_fresh_private_cwd_flags_and_anonymous_prompt(self):
        done = self.run_adapter()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "您好，请问具体需要什么？"})
        captured = json.loads(self.capture.read_text())
        args = captured["args"]
        self.assertEqual(args[0], "exec")
        self.assertEqual(args[-1], "-")
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("--skip-git-repo-check", args)
        self.assertIn("read-only", args)
        for feature in ("apps", "plugins", "multi_agent", "shell_tool", "unified_exec", "shell_snapshot"):
            self.assertIn(feature, args)
        self.assertIn('web_search="disabled"', args)
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")
        self.assertTrue(captured["cwd"].startswith("/"))
        self.assertNotEqual(captured["cwd"], str(SCRIPT.parent))
        prompt = captured["prompt"]
        for secret in ("private-account-id", "private-group@chatroom", "private-session-id",
                       "private-channel-id", "private-wxid-A", "private-wxid-B", "987654"):
            self.assertNotIn(secret, prompt)
        self.assertIn("成员_", prompt)
        self.assertIn("想做一个预约页面", prompt)
        self.assertIn("已有设计稿吗", prompt)

    def test_curated_memory_prompt_keeps_only_note_text(self):
        data = request()
        data["session_memory"] = {"version": 1,
                                  "binding": {"channel_id": "memory-private-channel", "session_id": "memory-private-session",
                                              "account": "memory-private-account", "talker": "memory-private-group"},
                                  "revision": 2, "summary": "Prefers Tuesday lessons",
                                  "summary_source_cursors": [23456],
                                  "confirmed": [{"text": "Tuesday works", "source_cursors": [23456]}],
                                  "open_questions": [{"text": "Which time zone?", "source_cursors": [23456]}]}
        done = self.run_adapter(data=data)
        self.assertEqual(done.returncode, 0, done.stderr)
        prompt = json.loads(self.capture.read_text())["prompt"]
        for text in ("Prefers Tuesday lessons", "Tuesday works", "Which time zone?", "人工整理的会话笔记"):
            self.assertIn(text, prompt)
        for secret in ("memory-private-channel", "memory-private-session", "memory-private-account",
                       "memory-private-group", "23456"):
            self.assertNotIn(secret, prompt)

    def test_skip_and_bad_response_fail_closed(self):
        skipped = self.run_adapter(result={"skip": True, "text": ""})
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertEqual(json.loads(skipped.stdout), {"skip": True})
        for bad in ({"skip": False, "text": "x" * 1501},
                    {"skip": True, "text": "oops"},
                    {"skip": False, "text": " "},
                    {"text": "missing skip"}):
            failed = self.run_adapter(result=bad)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, "")

    def test_codex_failure_and_bad_input_do_not_create_fallback_reply(self):
        failed = self.run_adapter(fail=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        wrong = request()
        wrong["event"]["direction"] = "unknown"
        failed = self.run_adapter(data=wrong)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")

    def test_unavailable_model_stops_without_fallback(self):
        failed = self.run_adapter(model="gpt-6-luna", fail=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
        self.assertEqual(args.count("--model"), 1)
        default = self.run_adapter(model=None)
        self.assertEqual(default.returncode, 0, default.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")


if __name__ == "__main__":
    unittest.main()
