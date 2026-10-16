#!/usr/bin/env python3
"""Offline controller tests with a synthetic, non-WeChat runner."""
import json
import contextlib
import fcntl
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from wx_agent import wechat_agent_ctl as ctl


FAKE_RUNNER = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv
state = Path(args[args.index('--state-dir') + 1])
while True:
    path = state / 'channel-agent-control.json'
    action = json.loads(path.read_text())['action']
    if action == 'stop':
        break
    time.sleep(0.05)
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.runner = self.root / "fake_runner.py"
        self.runner.write_text(FAKE_RUNNER)
        self.runner.chmod(0o700)
        self.original_runner = ctl.RUNNER
        ctl.RUNNER = self.runner
        self.addCleanup(setattr, ctl, "RUNNER", self.original_runner)
        for name in ("wx_cli", "sender", "agent"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
        self.settings = {"wx_cli": str(self.root / "wx_cli"), "state_dir": str(self.state),
                         "consumer": "test", "agent_command": [str(self.root / "agent")],
                         "sender_driver": str(self.root / "sender"), "read_retries": 0}
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.settings))
        self.config_path.chmod(0o600)
        self.addCleanup(self.stop_if_live)

    def stop_if_live(self):
        try:
            metadata = ctl.metadata_for(self.state)
            if metadata and ctl.alive(metadata, self.state):
                ctl.write_control(self.state, "stop")
                for _ in range(40):
                    if not ctl.alive(metadata, self.state):
                        break
                    time.sleep(0.05)
        except (OSError, ctl.ControlError):
            pass

    def test_rejects_nonprivate_config(self):
        self.config_path.chmod(0o644)
        with self.assertRaises(ctl.ControlError):
            ctl.config(self.config_path)
        self.config_path.chmod(0o600)
        self.assertEqual(ctl.config(self.config_path)["consumer"], "test")

    def test_remote_sender_path_only_needs_to_be_absolute(self):
        self.settings["sender_driver"] = "/remote/mac-mini/wechat-driver"
        self.config_path.write_text(json.dumps(self.settings))
        self.assertEqual(ctl.config(self.config_path)["sender_driver"], self.settings["sender_driver"])

    def test_busy_runner_lock_rejects_without_overwriting_control(self):
        lock = self.state / "channel-agent.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ctl.write_control(self.state, "pause")
            with self.assertRaises(ctl.ControlError):
                ctl.start(ctl.config(self.config_path))
            self.assertEqual(json.loads((self.state / ctl.CONTROL_NAME).read_text())["action"], "pause")
        finally:
            os.close(fd)

    def test_interactive_lease_blocks_start_and_resume(self):
        settings = ctl.config(self.config_path)
        ctl.atomic_json(self.state / ctl.INTERACTIVE_NAME, {"version": 1, "phase": "active"})
        with self.assertRaisesRegex(ctl.ControlError, "interactive lease"):
            ctl.start(settings)
        with self.assertRaisesRegex(ctl.ControlError, "interactive lease"):
            ctl.action(settings, "resume", 0)
        self.assertFalse((self.state / ctl.CONTROL_NAME).exists())

    def test_handback_pid_registration_failure_cleans_unreleased_child(self):
        settings = ctl.config(self.config_path)
        ctl.atomic_json(self.state / ctl.INTERACTIVE_NAME,
                        {"version": 1, "phase": "handback", "handoff_token": "test-token"})
        spawned = []
        original_popen = ctl.subprocess.Popen

        def capture(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            spawned.append(process)
            return process

        with mock.patch.object(ctl.subprocess, "Popen", side_effect=capture), \
             mock.patch.object(ctl, "process_stamp", return_value=None):
            with self.assertRaisesRegex(ctl.ControlError, "startup"):
                ctl.start(settings, handoff_token="test-token")
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll(), "unreleased child retained runner lock")
        self.assertFalse((self.state / ctl.PID_NAME).exists())
        self.assertTrue((self.state / ctl.INTERACTIVE_NAME).exists())

    def test_start_duplicate_pause_resume_stop_without_kill(self):
        settings = ctl.config(self.config_path)
        first = ctl.start(settings)
        self.assertTrue(first["started"])
        self.assertTrue(ctl.status(settings)["alive"])
        with self.assertRaises(ctl.ControlError):
            ctl.start(settings)
        with mock.patch.object(ctl.os, "kill", side_effect=AssertionError("blind kill")):
            self.assertEqual(ctl.action(settings, "pause", 0)["requested"], "pause")
            self.assertEqual(json.loads((self.state / ctl.CONTROL_NAME).read_text())["action"], "pause")
            self.assertEqual(ctl.action(settings, "resume", 0)["requested"], "resume")
            self.assertEqual(json.loads((self.state / ctl.CONTROL_NAME).read_text())["action"], "run")
            self.assertEqual(ctl.action(settings, "stop", 3)["requested"], "stop")
        self.assertFalse(ctl.status(settings)["alive"])

    def test_stale_pid_does_not_control_unrelated_process(self):
        settings = ctl.config(self.config_path)
        ctl.atomic_json(self.state / ctl.PID_NAME, {"version": 1, "pid": os.getpid(), "start_stamp": "wrong"})
        result = ctl.status(settings)
        self.assertFalse(result["alive"])
        self.assertTrue(result["stale_pid"])
        with self.assertRaises(ctl.ControlError):
            ctl.action(settings, "stop", 0)
        self.assertFalse((self.state / ctl.CONTROL_NAME).exists())

    def test_status_excludes_configuration_and_private_message(self):
        settings = ctl.config(self.config_path)
        ctl.atomic_json(self.state / ctl.HEALTH_NAME, {"version": 1, "stage": "reading",
                                                        "retry_count": 0, "last_processed_cursor": 4,
                                                        "private_message": "do not display"})
        result = ctl.status(settings)
        self.assertEqual(result["health"]["stage"], "reading")
        self.assertNotIn("private_message", json.dumps(result))
        self.assertNotIn(str(self.root / "sender"), json.dumps(result))

    def test_management_survives_temporarily_unexecutable_dependencies(self):
        settings = ctl.config(self.config_path)
        ctl.start(settings)
        (self.root / "wx_cli").chmod(0o600)
        (self.root / "agent").chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ctl.main(["--config", str(self.config_path), "status"]), 0)
        self.assertTrue(json.loads(output.getvalue())["alive"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ctl.main(["--config", str(self.config_path), "stop"]), 0)
        self.assertFalse(json.loads(output.getvalue())["alive"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ctl.main(["--config", str(self.config_path), "start"]), 1)


if __name__ == "__main__":
    unittest.main()
