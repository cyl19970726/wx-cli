"""Installed entrypoint and controller lifecycle with synthetic local tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

from wx_agent import channel_agent, wechat_agent_ctl


FAKE_CLI = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(args[args.index('--state-dir') + 1])
if args[0:1] == ['outbox'] and 'list' in args:
    print('[]')
elif args[0:1] != ['channel']:
    sys.exit(2)
elif 'status' in args:
    if '--consumer' not in args and (state / 'fail-start').exists():
        sys.exit(7)
    output = {'binding': {'channel_id': 'synthetic', 'session_id': 's1',
        'account': 'test-account', 'talker': 'test@chatroom',
        'source': '/synthetic', 'encrypted_snapshot': True}, 'latest_cursor': 1}
    if '--consumer' in args:
        output['consumer_cursor'] = int((state / 'acked').read_text()) if (state / 'acked').exists() else 0
    print(json.dumps(output))
elif 'sync' in args:
    print('{"events_appended":0}')
elif 'ack' in args:
    cursor = int(args[args.index('--cursor') + 1])
    (state / 'acked').write_text(str(cursor))
    print(json.dumps({'cursor': cursor}))
elif 'subscribe' in args:
    if int((state / 'acked').read_text()) < 1:
        print(json.dumps({'cursor': 1, 'server_id': 101, 'kind': 'message_observed',
            'direction': 'incoming', 'channel_id': 'synthetic', 'session_id': 's1',
            'account': 'test-account', 'talker': 'test@chatroom',
            'message': {'server_id': 101, 'talker': 'test@chatroom',
                        'sender': 'member', 'content': {'Text': 'hello'}}}))
elif 'events' in args:
    if int(args[args.index('--after') + 1]) < 1:
        print(json.dumps({'cursor': 1, 'server_id': 101, 'kind': 'message_observed',
            'direction': 'incoming', 'channel_id': 'synthetic', 'session_id': 's1',
            'account': 'test-account', 'talker': 'test@chatroom',
            'message': {'server_id': 101, 'talker': 'test@chatroom',
                        'sender': 'member', 'content': {'Text': 'hello'}}}))
else:
    sys.exit(3)
'''


class InstalledPackageTests(unittest.TestCase):
    def test_entrypoint_resolves_installed_modules(self):
        entrypoint = Path(sysconfig.get_path("scripts")) / "wx-agent"
        self.assertTrue(entrypoint.is_file())
        self.assertTrue(Path(channel_agent.__file__).is_file())
        self.assertEqual(wechat_agent_ctl.RUNNER, Path(channel_agent.__file__).resolve())
        result = subprocess.run([str(entrypoint), "--help"], cwd=tempfile.gettempdir(),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sender", result.stdout)
        refused = subprocess.run([str(entrypoint), "sender", "--unsupported"],
                                 cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=5)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(json.loads(refused.stdout)["reason"], "unsupported command")

    def test_installed_controller_starts_identifies_and_stops_child(self):
        entrypoint = Path(sysconfig.get_path("scripts")) / "wx-agent"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            state = root / "state"
            state.mkdir(mode=0o700)
            cli = root / "fake-cli"
            cli.write_text(FAKE_CLI, encoding="utf-8")
            cli.chmod(0o700)
            agent = root / "fake-agent"
            agent.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
Path(os.environ['FAKE_AGENT_REQUEST']).write_text(json.dumps(request))
print('{"skip":true}')
''', encoding="utf-8")
            agent.chmod(0o700)
            settings = {"wx_cli": str(cli), "state_dir": str(state), "consumer": "test",
                        "agent_command": [str(agent)], "sender_driver": "/synthetic/driver",
                        "poll_seconds": 1, "start_after": 0,
                        "read_retries": 1, "retry_base_seconds": 0.01}
            config = root / "config.json"
            config.write_text(json.dumps(settings), encoding="utf-8")
            config.chmod(0o600)
            memory = {"version": 1, "binding": {"channel_id": "synthetic", "session_id": "s1",
                      "account": "test-account", "talker": "test@chatroom"}, "revision": 1,
                      "summary": "Earlier synthetic lesson preference", "summary_source_cursors": [1],
                      "confirmed": [], "open_questions": []}
            memory_path = state / "agent-memory.json"
            memory_path.write_text(json.dumps(memory), encoding="utf-8")
            memory_path.chmod(0o600)
            request_path = root / "agent-request.json"

            def run(command):
                env = dict(os.environ, FAKE_AGENT_REQUEST=str(request_path))
                return subprocess.run([str(entrypoint), command, "--config", str(config)],
                                      cwd=root, env=env, capture_output=True, text=True, timeout=8)

            def chat(command, *args):
                env = dict(os.environ, FAKE_AGENT_REQUEST=str(request_path))
                return subprocess.run([str(entrypoint), "chat", "--config", str(config), command, *args],
                                      cwd=root, env=env, capture_output=True, text=True, timeout=8)

            started = run("start")
            self.assertEqual(started.returncode, 0, started.stderr)
            pid = json.loads(started.stdout)["pid"]
            try:
                status = None
                for _ in range(30):
                    status = run("status")
                    if status.returncode == 0 and json.loads(status.stdout)["alive"]:
                        break
                    time.sleep(0.05)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(json.loads(status.stdout)["pid"], pid,
                                 (state / "channel-agent.log").read_text(encoding="utf-8"))
                metadata = wechat_agent_ctl.metadata_for(state)
                self.assertTrue(wechat_agent_ctl.alive(metadata, state),
                                repr((metadata, wechat_agent_ctl.RUNNER,
                                      wechat_agent_ctl.process_stamp(pid),
                                      (state / "channel-agent.log").read_text(encoding="utf-8"))))
                self.assertIn(str(wechat_agent_ctl.RUNNER), wechat_agent_ctl.process_stamp(pid)[1])
                for _ in range(100):
                    if request_path.exists() and (state / "acked").read_text() == "1":
                        break
                    time.sleep(0.05)
                self.assertTrue(request_path.exists(), repr({
                    "log": (state / "channel-agent.log").read_text(),
                    "health": (state / "channel-agent-health.json").read_text(),
                    "acked": (state / "acked").read_text() if (state / "acked").exists() else None,
                }))
                self.assertEqual(json.loads(request_path.read_text())["session_memory"]["summary"],
                                 "Earlier synthetic lesson preference")
                selected = chat("select", "--talker", "test@chatroom")
                self.assertEqual(selected.returncode, 0, selected.stderr)
                taken = chat("takeover")
                self.assertEqual(taken.returncode, 0, taken.stderr)
                self.assertTrue(json.loads(taken.stdout)["previously_alive"])
                self.assertFalse(json.loads(run("status").stdout)["alive"])
                (state / "fail-start").touch()
                failed = chat("handback")
                self.assertNotEqual(failed.returncode, 0, failed.stdout)
                self.assertEqual(json.loads((state / "channel-agent-interactive.json").read_text())["phase"], "handback")
                self.assertFalse((state / channel_agent.HANDOFF_READY_NAME).exists())
                (state / "fail-start").unlink()
                returned = chat("handback")
                self.assertEqual(returned.returncode, 0, returned.stderr)
                self.assertTrue(json.loads(returned.stdout)["alive"])
                self.assertFalse((state / "channel-agent-interactive.json").exists())
            finally:
                if json.loads(run("status").stdout)["alive"]:
                    stopped = run("stop")
                    self.assertEqual(stopped.returncode, 0, stopped.stderr)
                    self.assertFalse(json.loads(stopped.stdout)["alive"])


if __name__ == "__main__":
    unittest.main()
