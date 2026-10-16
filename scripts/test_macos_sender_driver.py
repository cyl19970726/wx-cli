"""No-GUI sender contract checks; no WeChat process is launched."""
import contextlib
import io
import json
import fcntl
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wx_agent import macos_sender_driver as driver

SCRIPT = Path(driver.__file__)
REPO_ROOT = Path(__file__).resolve().parents[1]


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.binding = {"channel_id": "channel-one", "session_id": "session-one",
                        "account": "local-account", "talker": "group@chatroom",
                        "source": "/private/source", "encrypted_snapshot": True,
                        "self_wxid": "wxid-self"}
        self.config = {"binding": self.binding}
        self.request = {"protocol_version": 1, "request_id": "reply.1",
                        "channel_id": "channel-one", "session_id": "session-one",
                        "account": "local-account", "talker": "group@chatroom", "text": "Hello 😊"}

    def test_request_binding_and_stable_safe_transaction(self):
        tid = driver.request_ok(self.request, self.config)
        self.assertRegex(tid, r"^send-[0-9a-f]{48}$")
        self.assertEqual(tid, driver.request_ok(dict(self.request), self.config))
        self.assertEqual(tid, driver.request_ok(dict(self.request, attempt_number=1), self.config))
        self.assertEqual(driver.request_ok(dict(self.request, attempt_number=2), self.config), tid + "-a2")
        self.assertEqual(driver.request_ok(dict(self.request, attempt_number=3, recovery_from_attempt=2),
                                           self.config), tid + "-a3")
        for altered in (dict(self.request, attempt_number=0),
                        dict(self.request, attempt_number=3, recovery_from_attempt=1)):
            with self.assertRaises(driver.Refused):
                driver.request_ok(altered, self.config)
        for key in ("channel_id", "session_id", "account", "talker"):
            bad = dict(self.request, **{key: "other"})
            with self.subTest(key=key), self.assertRaises(driver.Refused):
                driver.request_ok(bad, self.config)

    def test_reject_invalid_text(self):
        for text in ("", " \n", "bad\x00text", "x" * 2001):
            with self.subTest(text=repr(text)[:15]), self.assertRaises(driver.Refused):
                driver.request_ok(dict(self.request, text=text), self.config)

    def test_generic_transaction_is_scoped_to_conversation(self):
        first = dict(self.config, target_name="Contact A")
        request = dict(self.request)
        one = driver.request_ok(request, first)
        other_binding = dict(self.binding, channel_id="channel-two", talker="private-contact")
        other = dict(self.config, binding=other_binding, target_name="Contact B")
        request.update(channel_id="channel-two", talker="private-contact")
        self.assertNotEqual(one, driver.request_ok(request, other))
        self.assertEqual(one, driver.request_ok(dict(self.request), first))

    def test_private_title_requires_unique_fresh_metadata_identity(self):
        config = dict(self.config, binding=dict(self.binding, talker="private-contact"),
                      wx_cli="/private/wx-cli", channel_state_dir="/private/state",
                      target_name="Contact", conversation_kind="private")
        candidate = {"talker": "private-contact", "name": "Contact", "kind": "private"}
        with mock.patch.object(driver, "call", return_value=json.dumps({
                "match_kind": "exact", "matches": [candidate]})) as call:
            driver.verify_target(config, driver.time.monotonic() + 3)
        self.assertIn("--all-matches", call.call_args[0][0])
        self.assertEqual(call.call_args[0][0][-2:], ["--kind", "all"])
        for matches in ([], [candidate, dict(candidate, talker="other@chatroom", kind="group")],
                        [candidate, dict(candidate, name="Contact (2)", talker="other")],
                        [candidate, dict(candidate, name="Contact（２）", talker="other")],
                        [dict(candidate, talker="different")], [dict(candidate, name="Renamed")]):
            with mock.patch.object(driver, "call", return_value=json.dumps({
                    "match_kind": "exact", "matches": matches})), self.assertRaises(driver.Refused):
                driver.verify_target(config, driver.time.monotonic() + 3)

    def test_numeric_title_suffix_collision_is_symmetric(self):
        for left, right in (("Alex", "Alex (2)"), ("Alex（３）", "Alex"),
                            ("Alex", "alex (12)")):
            self.assertTrue(driver.titles_overlap(left, right))
            self.assertTrue(driver.titles_overlap(right, left))
        self.assertFalse(driver.titles_overlap("Alex", "Alex Team"))
        self.assertEqual(driver.title_query("Alex (2)（３）"), "Alex")

    def test_shared_desktop_lock_refuses_other_conversation_before_ui(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            fd = os.open(ui / "sender.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                config = dict(self.config, ui_dir=ui, channel_state_dir=ui / "other-channel")
                with mock.patch.object(driver, "bridge") as bridge, \
                     mock.patch.object(driver, "status", return_value=0):
                    with self.assertRaisesRegex(driver.Refused, "sender busy"):
                        driver.focus_only(config)
                    with self.assertRaisesRegex(driver.Refused, "sender busy"):
                        driver.run(self.request, config)
                bridge.assert_not_called()
                self.assertFalse(list(ui.glob("*.client-result.json")))
            finally:
                os.close(fd)

    def _prior_unsent_artifacts(self, root, result_error="Cannot snapshot a clipboard item", modern=False):
        ui = Path(root)
        ui.chmod(0o700)
        config = dict(self.config, ui_dir=ui, target_group="Synthetic Group", expected_draft="",
                      conversation_kind="group",
                      expected_pid=42, composer_rect={"x": 400, "y": 500, "width": 300, "height": 50},
                      button_point={"x": 800, "y": 570})
        request = dict(self.request)
        tid = driver.request_ok(request, config)
        report = {"id": tid, "stage": "group_focused", "status": "indeterminate",
                  "error": driver.helper_reason({"error": result_error})}
        frame = {"x": 0, "y": 0, "width": 900, "height": 650}
        state = {"id": tid, "stage": "prepare-attempted", "text": request["text"],
                 "targetGroup": config["target_group"], "expectedDraft": "", "expectedPID": 42,
                 "expectedWindowID": 20, "expectedFrame": frame,
                 "composerRect": config["composer_rect"], "buttonPoint": config["button_point"]}
        prep_job = dict(state, action="prepare-visual")
        focus_id = tid + "-focus"
        focus_result = {"id": focus_id, "ok": True, "focused": True,
                        "alreadySelected": False, "source": "search"}
        focus_job = {"id": focus_id, "action": "focus-group", "targetGroup": config["target_group"],
                     "expectedPID": 42, "expectedWindowID": 20, "expectedFrame": frame,
                     "conversationListRect": {"x": 1, "y": 60, "width": 200, "height": 200},
                     "searchPoint": {"x": 20, "y": 30}}
        if modern:
            state.update(targetKind="group", focusEvidenceID=focus_id)
            prep_job.update(targetKind="group", focusEvidenceID=focus_id)
            focus_job["targetKind"] = "group"
            focus_result.update(targetKind="group", focusEvidenceID=focus_id, category="group-chats")
            driver.private_create(ui / (focus_id + ".focus-evidence.json"), {
                "id": focus_id, "targetGroup": config["target_group"], "targetKind": "group",
                "expectedPID": 42, "expectedWindowID": 20, "expectedFrame": frame,
                "source": "search", "category": "group-chats", "selectedAt": 1234.5})
        driver.private_create(ui / (tid + ".client-result.json"), report)
        driver.private_create(ui / (tid + ".prepare-visual.job.json"), prep_job)
        driver.private_create(ui / (tid + ".prepare-visual.result.json"),
                              {"id": tid, "ok": False, "error": result_error,
                               "permissions": {"accessibility": True, "screenRecording": True,
                                               "postEvents": True}})
        driver.private_create(ui / (tid + ".visual-state.json"), state)
        for suffix, action in (("-inspect", "inspect"), ("-focus", "focus-group")):
            prefix = tid + suffix
            driver.private_create(ui / (prefix + "." + action + ".job.json"),
                                  focus_job if action == "focus-group" else {"id": prefix, "action": action})
            driver.private_create(ui / (prefix + ".result.json"),
                                  focus_result if action == "focus-group" else {"id": prefix, "ok": True})
        return request, config, tid

    def test_unsent_proof_requires_exact_prepare_failure_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_unsent_artifacts(root)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                proof = driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)
            self.assertTrue(Path(proof).is_file())
            self.assertEqual(driver.private_read(Path(proof))["prior_focus_mode"], "legacy")
            with mock.patch.object(driver, "_helper_is_live", return_value=False), \
                 self.assertRaisesRegex(driver.Refused, "already exists"):
                driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)
            self.assertEqual(Path(proof).name, tid + ".recovery-proof-to-2.json")

    def test_unsent_proof_accepts_complete_kind_aware_history_and_rejects_partial_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, _ = self._prior_unsent_artifacts(root, modern=True)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                proof = driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3,
                                            persist=False)
            self.assertEqual(proof["prior_focus_mode"], "kind-aware")
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_unsent_artifacts(root, modern=True)
            evidence_path = config["ui_dir"] / (tid + "-focus.focus-evidence.json")
            evidence = driver.private_read(evidence_path)
            evidence["category"] = "contacts"
            driver.private_update(evidence_path, evidence)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            with mock.patch.object(driver, "_helper_is_live", return_value=False), \
                 self.assertRaisesRegex(driver.Refused, "focus evidence"):
                driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3,
                                    persist=False)

    def test_unsent_proof_rejects_submit_markers_late_results_live_helper_and_mismatches(self):
        mutations = ("marker", "missing_result", "live", "wrong_text", "wrong_target", "wrong_stage")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                request, config, tid = self._prior_unsent_artifacts(root)
                ui = config["ui_dir"]
                if mutation == "marker":
                    driver.private_create(ui / (tid + ".submit-attempted.json"), {"id": tid})
                elif mutation == "missing_result":
                    (ui / (tid + ".prepare-visual.result.json")).unlink()
                elif mutation == "wrong_text":
                    path = ui / (tid + ".visual-state.json")
                    value = driver.private_read(path)
                    value["text"] = "different"
                    driver.private_update(path, value)
                elif mutation == "wrong_target":
                    path = ui / (tid + ".prepare-visual.job.json")
                    value = driver.private_read(path)
                    value["targetGroup"] = "another conversation"
                    driver.private_update(path, value)
                elif mutation == "wrong_stage":
                    path = ui / (tid + ".client-result.json")
                    value = driver.private_read(path)
                    value["stage"] = "prepared"
                    driver.private_update(path, value)
                recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
                with mock.patch.object(driver, "_helper_is_live", return_value=(mutation == "live")), \
                     self.assertRaises(driver.Refused):
                    driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)
        with tempfile.TemporaryDirectory() as root:
            request, config, _ = self._prior_unsent_artifacts(root)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1, talker="other@chatroom")
            with mock.patch.object(driver, "_helper_is_live", return_value=False), \
                 self.assertRaisesRegex(driver.Refused, "binding mismatch"):
                driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)

    def test_installed_sender_reports_recovery_capability_without_configuration(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--recovery-capabilities"],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), {"protocol_version": 1,
                         "recovery_protocol_version": 1, "pre_submit_proof_required": True})

    def test_recovery_preflight_is_read_only_and_uses_shared_desktop_lock(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_unsent_artifacts(root)
            lock_path = config["ui_dir"] / "sender.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            before = {path.name for path in config["ui_dir"].iterdir()}
            with mock.patch.object(driver, "status", return_value=11), \
                 mock.patch.object(driver, "_helper_is_live", return_value=False):
                result = driver.recovery_preflight(recovery, config)
            self.assertEqual(result, {"protocol_version": 1, "recovery_protocol_version": 1,
                                      "unsent_proven": True})
            after = {path.name for path in config["ui_dir"].iterdir()}
            self.assertEqual(after, before)
            self.assertFalse((config["ui_dir"] / (tid + ".recovery-proof-to-2.json")).exists())
            with mock.patch.object(driver, "status", return_value=11), \
                 mock.patch.object(driver, "_helper_is_live", return_value=False):
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(driver.Refused, "sender busy"):
                        driver.recovery_preflight(recovery, config)
                finally:
                    os.close(fd)

    def test_inspect_requires_one_calibrated_window_and_permissions(self):
        config = {"expected_pid": 42, "window_size": {"width": 900, "height": 650}}
        data = {"weChatPID": 42, "windows": [{"id": 20, "frame": {"x": 4, "y": 6, "width": 900, "height": 650}}],
                "session": {"onConsole": True, "locked": False},
                "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}}
        self.assertEqual(driver.inspect_ok(data, config)[0], 20)
        for changed in (dict(data, windows=[]), dict(data, weChatPID=43),
                        dict(data, session={"onConsole": True, "locked": True}),
                        dict(data, permissions={"accessibility": False, "screenRecording": True, "postEvents": True})):
            with self.assertRaises(driver.Refused):
                driver.inspect_ok(changed, config)

    def test_local_event_requires_new_exact_outgoing_self_text(self):
        event = {"cursor": 11, "kind": "message_observed", "channel_id": "channel-one",
                 "session_id": "session-one", "account": "local-account", "talker": "group@chatroom",
                 "direction": "outgoing", "message": {"sender": "wxid-self", "talker": "group@chatroom",
                                               "msg_type": 1, "content": {"Text": "Hello 😊"}}}
        self.assertTrue(driver.matching_event(event, self.request, "wxid-self", 10))
        cases = [dict(event, cursor=10), dict(event, direction="unknown"),
                 dict(event, kind="message_updated"), dict(event, kind="message_added"),
                 dict(event, talker="other")]
        for altered in cases:
            self.assertFalse(driver.matching_event(altered, self.request, "wxid-self", 10))
        self.assertFalse(driver.matching_event(dict(event, message=dict(event["message"], sender="other")), self.request, "wxid-self", 10))
        self.assertFalse(driver.matching_event(dict(event, message=dict(event["message"], content={"Text": "other"})), self.request, "wxid-self", 10))

    def test_observation_retries_transient_snapshot_failure_without_resend(self):
        event = {"cursor": 18, "kind": "message_observed", "channel_id": "channel-one",
                 "session_id": "session-one", "account": "local-account", "talker": "group@chatroom",
                 "direction": "outgoing", "message": {"sender": "wxid-self", "talker": "group@chatroom",
                                               "msg_type": 1, "content": {"Text": "Hello 😊"}}}
        calls = []
        def fake_channel(_config, _deadline, *parts):
            calls.append(parts[0])
            if parts[0] == "sync" and calls.count("sync") == 1:
                raise driver.Refused("transient encrypted-copy snapshot failure")
            if parts[0] == "events":
                return json.dumps(event)
            return "{}"
        detail = {}
        with mock.patch.object(driver, "channel", side_effect=fake_channel), \
             mock.patch.object(driver.time, "sleep"):
            cursor = driver.observe(self.config, self.request, 17,
                                    driver.time.monotonic() + 2, detail)
        self.assertEqual(cursor, 18)
        self.assertEqual(detail["sync_failures"], 1)
        self.assertEqual(detail["rows_examined"], 1)
        self.assertEqual(calls, ["sync", "sync", "events"])
        self.assertNotIn("text", detail)

    def test_real_synthetic_channel_event_contract(self):
        binary = REPO_ROOT / "target/debug/wx-cli"
        if not binary.exists():
            self.skipTest("compiled wx-cli fixture binary unavailable")
        from scripts.test_local_channel import fixture, GROUP
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "db_storage"
            state = Path(root) / "state"
            fixture(source)
            base = [str(binary), "channel", "--state-dir", str(state)]
            def cli(*args):
                result = subprocess.run(base + list(args), capture_output=True, text=True, check=True, timeout=10)
                return [json.loads(line) for line in result.stdout.splitlines() if line]
            binding = cli("bind", "--account", "synthetic", "--talker", GROUP,
                          "--session", "test-session", "--source", str(source))[0]
            cli("sync")
            event = cli("events", "--after", "0")[0]
            self.assertEqual(event["kind"], "message_observed")
            self.assertIsInstance(event["message"]["content"], dict)
            text = event["message"]["content"]["Text"]
            # Fixture's sender is synthetic_member; replace only sender/direction to
            # exercise the actual event schema as it would appear for self.
            event["direction"] = "outgoing"
            event["message"]["sender"] = "synthetic_self"
            request = {"channel_id": binding["channel_id"], "session_id": binding["session_id"],
                       "account": binding["account"], "talker": GROUP, "text": text}
            self.assertTrue(driver.matching_event(event, request, "synthetic_self", 0))

    def test_bounded_command_output(self):
        with self.assertRaises(driver.Refused):
            driver.call([sys.executable, "-c", "import sys; sys.stdout.write('x'*100000)"],
                        driver.time.monotonic() + 3, limit=1024)

    def test_helper_error_category_does_not_echo_text(self):
        self.assertEqual(driver.helper_reason({"error": "Copied composer text does not exactly match expected draft"}),
                         "composer draft mismatch")
        self.assertEqual(driver.helper_reason({"error": "private secret 123"}), "helper rejected action")
        denied = {"ok": False,
                  "error": 'Error Domain=com.apple.ScreenCaptureKit.SCStreamErrorDomain Code=-3801 "用户拒绝...TCC"',
                  "permissions": {"accessibility": False, "postEvents": False, "screenRecording": False}}
        self.assertEqual(driver.helper_reason(denied), "bridge permissions missing")

    def test_bridge_surfaces_denied_tcc_from_private_result(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            tid = "focus-denied"
            result = {"id": tid, "ok": False,
                      "error": 'Error Domain=com.apple.ScreenCaptureKit.SCStreamErrorDomain Code=-3801 "用户拒绝...TCC"',
                      "permissions": {"accessibility": False, "postEvents": False, "screenRecording": False}}
            def fake_open(_argv, _deadline, limit):
                driver.private_create(ui / (tid + ".result.json"), result)
                return ""
            with mock.patch.object(driver, "call", side_effect=fake_open):
                with self.assertRaisesRegex(driver.Uncertain, "^bridge permissions missing$"):
                    driver.bridge({"ui_dir": ui, "helper_app": "/private/bridge.app"}, tid,
                                  "inspect", {}, driver.time.monotonic() + 2, 1)

    def test_focus_group_runs_before_draft_or_submit(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            config = dict(self.config, ui_dir=ui, target_group="Synthetic Group",
                          expected_pid=42, expected_draft="",
                          composer_rect={"x": 320, "y": 500, "width": 400, "height": 80},
                          button_point={"x": 800, "y": 610},
                          conversation_list_rect={"x": 60, "y": 55, "width": 240, "height": 500},
                          search_point={"x": 155, "y": 28})
            inspect = {"weChatPID": 42, "windows": [{"id": 20, "frame": {"x": 0, "y": 0, "width": 880, "height": 640}}],
                       "session": {"onConsole": True, "locked": False},
                       "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}}
            config["window_size"] = {"width": 880, "height": 640}
            result = {"inspect": inspect, "focus-group": {"focused": True},
                      "prepare-visual": {"prepared": True}, "submit-visual": {"submitted": True}}
            actions = []
            commands = {}
            def fake_bridge(_cfg, _tid, action, command, _deadline, _seconds):
                actions.append(action)
                commands[action] = command
                if action == "focus-group":
                    driver.private_create(ui / (_tid + ".focus-evidence.json"), {
                        "id": _tid, "targetGroup": command["targetGroup"],
                        "targetKind": command["targetKind"], "expectedPID": command["expectedPID"],
                        "expectedWindowID": command["expectedWindowID"],
                        "expectedFrame": command["expectedFrame"], "source": "search",
                        "category": "group-chats", "selectedAt": 1234.5})
                    return {"focused": True, "alreadySelected": False, "source": "search",
                            "targetKind": command["targetKind"], "focusEvidenceID": _tid,
                            "category": "group-chats"}
                return result[action]
            with mock.patch.object(driver, "bridge", side_effect=fake_bridge), \
                 mock.patch.object(driver, "verify_target"), \
                 mock.patch.object(driver, "status", return_value=0), \
                 mock.patch.object(driver, "channel", return_value="{}"), \
                 mock.patch.object(driver, "call", return_value=""), \
                 mock.patch.object(driver, "observe", return_value=1):
                outcome = driver.run(self.request, config)
            self.assertEqual(outcome["status"], "ui_observed")
            self.assertEqual(actions, ["inspect", "focus-group", "prepare-visual", "submit-visual"])
            self.assertEqual(commands["focus-group"]["targetKind"], "group")
            self.assertEqual(commands["prepare-visual"]["targetKind"], "group")
            self.assertEqual(commands["submit-visual"]["targetKind"], "group")
            self.assertEqual(commands["prepare-visual"]["focusEvidenceID"], commands["submit-visual"]["focusEvidenceID"])
            report = driver.private_read(Path(outcome["evidence"]))
            self.assertEqual(report["stage"], "local_outgoing_observed")
            self.assertGreaterEqual(report["ui_submit_seconds"], 0)
            self.assertGreaterEqual(report["submit_action_seconds"], 0)
            self.assertIn("verification_seconds", report)

    def test_standalone_focus_only_uses_same_lock_and_never_prepares(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            config = dict(self.config, ui_dir=ui, target_group="Synthetic Group", expected_pid=42,
                          conversation_kind="group",
                          window_size={"width": 880, "height": 640},
                          conversation_list_rect={"x": 60, "y": 55, "width": 240, "height": 500},
                          search_point={"x": 155, "y": 28})
            inspect = {"weChatPID": 42, "windows": [{"id": 20, "frame": {"x": 0, "y": 0, "width": 880, "height": 640}}],
                       "session": {"onConsole": True, "locked": False},
                       "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}}
            actions = []
            def fake_bridge(_cfg, _tid, action, command, _deadline, _seconds):
                actions.append(action)
                if action == "inspect":
                    return inspect
                if action == "focus-group":
                    self.assertTrue((ui / "sender.lock").exists())
                    driver.private_create(ui / (_tid + ".focus-evidence.json"), {
                        "id": _tid, "targetGroup": command["targetGroup"],
                        "targetKind": command["targetKind"], "expectedPID": command["expectedPID"],
                        "expectedWindowID": command["expectedWindowID"],
                        "expectedFrame": command["expectedFrame"], "source": "search",
                        "category": "group-chats", "selectedAt": 1234.5})
                    return {"focused": True, "alreadySelected": False, "source": "search",
                            "targetKind": command["targetKind"], "focusEvidenceID": _tid,
                            "category": "group-chats"}
                self.fail("focus-only called a sending action")
            with mock.patch.object(driver, "bridge", side_effect=fake_bridge), \
                 mock.patch.object(driver, "verify_target"), \
                 mock.patch.object(driver, "status", return_value=0) as check_status, \
                 mock.patch.object(driver, "call", return_value=""):
                result = driver.focus_only(config)
            self.assertRegex(result["focus_evidence_id"], r"^focus-[a-f0-9]{32}$")
            self.assertEqual({key: result[key] for key in ("focused", "already_selected", "source", "target_kind", "category")},
                             {"focused": True, "already_selected": False, "source": "search",
                              "target_kind": "group", "category": "group-chats"})
            self.assertEqual(actions, ["inspect", "focus-group"])
            check_status.assert_called_once()
            self.assertFalse(list(ui.glob("*.client-result.json")))

    def test_standalone_focus_refuses_unusable_window_without_search_or_send(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            config = dict(self.config, ui_dir=ui, expected_pid=42, window_size={"width": 880, "height": 640})
            inspect = {"weChatPID": 42, "windows": [{"id": 20, "frame": {"x": 0, "y": 0, "width": 880, "height": 640}}],
                       "session": {"onConsole": True, "locked": False},
                       "permissions": {"accessibility": False, "screenRecording": True, "postEvents": True}}
            actions = []
            def fake_bridge(_cfg, _tid, action, _command, _deadline, _seconds):
                actions.append(action)
                return inspect
            with mock.patch.object(driver, "bridge", side_effect=fake_bridge), \
                 mock.patch.object(driver, "verify_target"), \
                 mock.patch.object(driver, "status", return_value=0), \
                 mock.patch.object(driver, "call", return_value="") as activate:
                with self.assertRaisesRegex(driver.Refused, "permissions"):
                    driver.focus_only(config)
            self.assertEqual(actions, ["inspect"])
            activate.assert_not_called()

    def test_private_visual_command_binds_contact_category_and_focus_evidence(self):
        config = dict(self.config, binding=dict(self.binding, talker="friend-wxid"),
                      target_name="Friend", target_group="Friend", conversation_kind="private",
                      expected_pid=42, composer_rect={"x": 400, "y": 500, "width": 300, "height": 50},
                      button_point={"x": 800, "y": 570})
        command = driver._prepare_command(self.request, config, 20,
                                          {"x": 0, "y": 0, "width": 900, "height": 650},
                                          "send-focus")
        self.assertEqual(command["targetKind"], "private")
        self.assertEqual(command["focusEvidenceID"], "send-focus")

    def test_focus_category_contract_matches_kind_and_saved_evidence(self):
        cases = (("contacts", "private", True), ("group-chats", "group", True),
                 ("most-used", "private", True), ("most-used", "group", True),
                 ("contacts", "group", False), ("group-chats", "private", False),
                 ("other", "group", False))
        for category, kind, accepted in cases:
            with self.subTest(category=category, kind=kind), tempfile.TemporaryDirectory() as root:
                ui = Path(root)
                ui.chmod(0o700)
                config = {"ui_dir": ui, "target_group": "Contact or group", "conversation_kind": kind,
                          "expected_pid": 42}
                focus_id = "focus-contract"
                frame = {"x": 0, "y": 0, "width": 900, "height": 650}
                driver.private_create(ui / (focus_id + ".focus-evidence.json"), {
                    "id": focus_id, "targetGroup": config["target_group"], "targetKind": kind,
                    "expectedPID": 42, "expectedWindowID": 20, "expectedFrame": frame,
                    "source": "search", "category": category, "selectedAt": 1234.5})
                result = {"focused": True, "alreadySelected": False, "source": "search",
                          "targetKind": kind, "focusEvidenceID": focus_id, "category": category}
                if accepted:
                    self.assertEqual(driver.focus_result_ok(result, config, focus_id, 20, frame)["category"],
                                     category)
                else:
                    with self.assertRaises(driver.Uncertain):
                        driver.focus_result_ok(result, config, focus_id, 20, frame)

    def test_focus_cli_does_not_read_stdin_and_returns_nonzero_on_failure(self):
        class ForbiddenInput:
            @property
            def buffer(self):
                raise AssertionError("focus CLI read stdin")
        with mock.patch.object(driver.sys, "argv", [str(SCRIPT), "--focus-only"]), \
             mock.patch.object(driver.sys, "stdin", ForbiddenInput()), \
             mock.patch.dict(driver.os.environ, {"WX_SENDER_CONFIG": "/private/config.json"}), \
             mock.patch.object(driver, "private_read", return_value={}), \
             mock.patch.object(driver, "safe_config", return_value={}), \
             mock.patch.object(driver, "focus_only", return_value={"focused": True, "already_selected": True, "source": "current"}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(driver.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"focused": True, "already_selected": True, "source": "current"})
        with mock.patch.object(driver.sys, "argv", [str(SCRIPT), "--focus-only"]), \
             mock.patch.object(driver.sys, "stdin", ForbiddenInput()), \
             mock.patch.dict(driver.os.environ, {"WX_SENDER_CONFIG": "/private/config.json"}), \
             mock.patch.object(driver, "private_read", return_value={}), \
             mock.patch.object(driver, "safe_config", return_value={}), \
             mock.patch.object(driver, "focus_only", side_effect=driver.Refused("channel binding changed")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(driver.main(), 1)
            self.assertEqual(json.loads(output.getvalue()), {"focused": False, "reason": "channel binding changed"})

    def test_status_checks_live_channel_binding(self):
        self.config["wx_cli"] = Path("/bin/false")
        self.config["channel_state_dir"] = Path("/private/state")
        payload = {"binding": dict(self.binding), "latest_cursor": 9}
        with mock.patch.object(driver, "channel", return_value=json.dumps(payload)):
            self.assertEqual(driver.status(self.config, 100), 9)
        payload["binding"]["self_wxid"] = "other"
        with mock.patch.object(driver, "channel", return_value=json.dumps(payload)):
            with self.assertRaises(driver.Refused):
                driver.status(self.config, 100)

    def test_exclusive_report_blocks_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            directory.chmod(0o700)
            report = directory / "send-id.client-result.json"
            driver.private_create(report, {"stage": "started"})
            with self.assertRaises(FileExistsError):
                driver.private_create(report, {"stage": "started"})
            self.assertEqual(driver.private_read(report)["stage"], "started")


if __name__ == "__main__":
    unittest.main()
