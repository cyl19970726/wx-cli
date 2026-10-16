"""Offline tests for the operator-only sender guard probe."""
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from wx_agent import sender_guard_probe as probe

PATH = Path(probe.__file__)


class ProbeTests(unittest.TestCase):
    def test_raw_driver_validation_rejects_config_overriding_wrapper(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            wrapper = root / "sender-driver-v4"
            wrapper.write_text("#!/bin/sh\nexport WX_SENDER_CONFIG=/fixed/config\nexec python3 /driver\n")
            wrapper.chmod(0o700)
            with self.assertRaisesRegex(probe.ProbeFailure, "raw macos_sender_driver.py"):
                probe.validate_raw_driver(wrapper)
            disguised = root / "macos_sender_driver.py"
            disguised.write_text(wrapper.read_text())
            disguised.chmod(0o700)
            with self.assertRaisesRegex(probe.ProbeFailure, "Python source"):
                probe.validate_raw_driver(disguised)
            self.assertEqual(probe.validate_raw_driver(PATH.with_name("macos_sender_driver.py")),
                             PATH.with_name("macos_sender_driver.py"))

    def test_config_clone_is_private_and_does_not_change_original(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            original = {"expected_pid": 42, "window_size": {"width": 880, "height": 640}}
            clone = probe.cloned_config(root, original, {"expected_pid": 2147483647})
            self.assertEqual(clone.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(clone.read_text())["expected_pid"], 2147483647)
            self.assertEqual(original["expected_pid"], 42)

    def test_focus_fault_refuses_without_gui_action_job(self):
        with tempfile.TemporaryDirectory() as name:
            ui = Path(name)
            ui.chmod(0o700)
            response = types.SimpleNamespace(returncode=1, stdout=b'{"focused":false,"reason":"window size changed"}')
            with mock.patch.object(probe, "command", return_value=response) as invoke:
                result = probe.probe_focus_refusal(Path("/driver"), ui, Path("/private/config"), "window size changed")
            self.assertEqual(result["new_gui_action_jobs"], 0)
            self.assertIn("--focus-only", invoke.call_args.args[0])

    def test_repeat_guard_uses_only_inert_driver(self):
        with tempfile.TemporaryDirectory() as name:
            scratch = Path(name)
            scratch.chmod(0o700)
            states = [{"request_id": "attempted", "status": "indeterminate", "attempt_number": 1}]
            response = types.SimpleNamespace(returncode=1, stdout=b"")
            with mock.patch.object(probe, "cli_json", return_value=states), \
                 mock.patch.object(probe, "command", return_value=response) as invoke:
                result = probe.repeat_without_ui({"wx_cli": "/bin/false", "channel_state_dir": "/private/state"}, scratch, scratch)
            self.assertTrue(result["refused_before_driver"])
            self.assertIn("--driver", invoke.call_args.args[0])
            self.assertFalse((scratch / "inert-driver-invoked").exists())

    def test_post_prepare_channel_read_failure_prevents_submit_fixture(self):
        sender = probe.load_driver(PATH.with_name("macos_sender_driver.py"))
        with tempfile.TemporaryDirectory() as name:
            ui = Path(name)
            ui.chmod(0o700)
            request = {"protocol_version": 1, "request_id": "fixture-read-fail",
                       "channel_id": "channel", "session_id": "session",
                       "account": "account", "talker": "group@chatroom", "text": "fixture"}
            config = {"binding": {"channel_id": "channel", "session_id": "session",
                                  "account": "account", "talker": "group@chatroom", "self_wxid": "self"},
                      "ui_dir": ui, "expected_pid": 42,
                      "window_size": {"width": 880, "height": 640},
                      "target_group": "Synthetic Group", "expected_draft": "",
                      "composer_rect": {}, "button_point": {},
                      "conversation_list_rect": {}, "search_point": {}}
            inspected = {"weChatPID": 42, "windows": [{"id": 2,
                          "frame": {"x": 0, "y": 0, "width": 880, "height": 640}}],
                         "session": {"onConsole": True, "locked": False},
                         "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}}
            actions = []
            def bridge(_cfg, _tid, action, _command, _deadline, _seconds):
                actions.append(action)
                if action == "inspect": return inspected
                if action == "focus-group": return {"focused": True}
                if action == "prepare-visual": return {"prepared": True}
                self.fail("read failure allowed submit or another UI action")
            with mock.patch.object(sender, "status", return_value=0), \
                 mock.patch.object(sender, "verify_target"), \
                 mock.patch.object(sender, "bridge", side_effect=bridge), \
                 mock.patch.object(sender, "call", return_value=""), \
                 mock.patch.object(sender, "channel", side_effect=sender.Refused("fixture read failed")):
                outcome = sender.run(request, config)
            self.assertEqual(outcome["status"], "indeterminate")
            self.assertEqual(actions, ["inspect", "focus-group", "prepare-visual"])
            report = sender.private_read(Path(outcome["evidence"]))
            self.assertEqual(report["stage"], "prepared")
            self.assertNotIn("ui_submit_seconds", report)

    def test_draft_probe_never_calls_submit_or_outbox(self):
        with tempfile.TemporaryDirectory() as name:
            ui = Path(name)
            ui.chmod(0o700)
            config = {"ui_dir": str(ui), "target_group": "Synthetic Group", "expected_pid": 42,
                      "composer_rect": {}, "button_point": {}}
            driver = types.SimpleNamespace()
            driver.Uncertain = type("Uncertain", (Exception,), {})
            driver.safe_config = lambda c: dict(c, ui_dir=Path(c["ui_dir"]))
            driver.status = lambda *_: 0
            driver.inspect_ok = lambda *_: (20, {"x": 0, "y": 0, "width": 880, "height": 640})
            driver.call = lambda *_args, **_kwargs: ""
            actions = []
            def bridge(_cfg, _tid, action, _data, _deadline, _seconds):
                actions.append(action)
                if action == "prepare-visual":
                    raise driver.Uncertain("composer draft mismatch")
                return {"ok": True}
            driver.bridge = bridge
            with mock.patch.object(probe, "load_driver", return_value=driver):
                result = probe.draft_probe(config, PATH.with_name("macos_sender_driver.py"))
            self.assertEqual(actions, ["inspect", "capture", "prepare-visual", "capture"])
            self.assertTrue(result["prepare_refused"])
            self.assertFalse(result["submit_called"])


if __name__ == "__main__":
    unittest.main()
