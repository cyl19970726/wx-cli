"""No-GUI sender contract checks; no WeChat process is launched."""
import contextlib
import io
import json
import fcntl
import hashlib
import os
import struct
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
    def test_inherited_recovery_lock_must_reacquire_same_locked_description(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / "sender.lock"
            held = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            other = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.dict(os.environ, {"WX_DESKTOP_LOCK_FD": str(other)}):
                    with self.assertRaises(driver.Refused):
                        with driver.recovery_sender_lock(lock_path):
                            pass
                with mock.patch.dict(os.environ, {"WX_DESKTOP_LOCK_FD": str(held)}):
                    with driver.recovery_sender_lock(lock_path):
                        pass
            finally:
                os.close(other)
                os.close(held)

    def setUp(self):
        self.binding = {"channel_id": "channel-one", "session_id": "session-one",
                        "account": "local-account", "talker": "group@chatroom",
                        "source": "/private/source", "encrypted_snapshot": True,
                        "self_wxid": "wxid-self"}
        self.config = {"binding": self.binding}
        self.request = {"protocol_version": 1, "request_id": "reply.1",
                        "channel_id": "channel-one", "session_id": "session-one",
                        "account": "local-account", "talker": "group@chatroom", "text": "Hello 😊"}

    def test_empty_composer_reference_is_private_pinned_and_geometry_bound(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            path = ui / "empty.png"
            # The Python boundary checks the pinned PNG header; Swift must also
            # decode and compare the entire image before accepting any pixels.
            raw = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 400, 80)
            path.write_bytes(raw)
            path.chmod(0o600)
            reference = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
            config = dict(self.config, ui_dir=ui, target_group="Synthetic Group", expected_pid=42,
                          composer_rect={"x": 320, "y": 500, "width": 400, "height": 80},
                          button_point={"x": 800, "y": 610}, empty_composer_reference=reference)
            driver.validate_empty_composer_reference(config)
            self.assertEqual(driver._prepare_command(self.request, config)["emptyComposerReference"], reference)
            self.assertNotIn("emptyComposerReference", driver._prepare_command(
                self.request, dict(config, empty_composer_reference=None)))
            for changed in ({"path": str(path), "sha256": "0" * 64},
                            dict(reference, unexpected=True),
                            dict(reference, path=str(ui / ".." / path.name)),
                            dict(reference, path=str(ui.parent / path.name))):
                with self.subTest(reference=changed), self.assertRaises(driver.Refused):
                    driver.validate_empty_composer_reference(dict(config, empty_composer_reference=changed))
            with self.assertRaises(driver.Refused):
                driver.validate_empty_composer_reference(dict(config, composer_rect={
                    "x": 320, "y": 500, "width": 401, "height": 80}))
            path.chmod(0o644)
            with self.assertRaises(driver.Refused):
                driver.validate_empty_composer_reference(config)
            path.chmod(0o600)
            link = ui / "link.png"
            link.symlink_to(path)
            with self.assertRaises(driver.Refused):
                driver.validate_empty_composer_reference(dict(config, empty_composer_reference=dict(reference, path=str(link))))
            link.unlink()
            os.link(path, link)
            with self.assertRaises(driver.Refused):
                driver.validate_empty_composer_reference(config)

    def test_empty_composer_alternates_are_bounded_pinned_and_forwarded(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            references = []
            for index in range(4):
                path = ui / (str(index) + ".png")
                raw = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 400, 80) + bytes([index])
                path.write_bytes(raw)
                path.chmod(0o600)
                references.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
            config = dict(self.config, ui_dir=ui, target_group="Synthetic Group", expected_pid=42,
                          composer_rect={"x": 320, "y": 500, "width": 400, "height": 80},
                          button_point={"x": 800, "y": 610}, empty_composer_reference=references[0],
                          empty_composer_alternates=references[1:3])
            driver.validate_empty_composer_reference(config)
            self.assertEqual(driver._prepare_command(self.request, config)["emptyComposerAlternates"], references[1:3])
            for changes in ({"empty_composer_alternates": references[1:]},
                            {"empty_composer_alternates": [references[0]]},
                            {"empty_composer_reference": None},
                            {"empty_composer_alternates": "invalid"},
                            {"empty_composer_alternates": [dict(references[1], sha256="0" * 64)]}):
                with self.subTest(changes=changes), self.assertRaises(driver.Refused):
                    driver.validate_empty_composer_reference(dict(config, **changes))

    def test_recovery_preserves_calibrated_alternates(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_unsent_artifacts(root, modern=True)
            alternate = {"path": str(Path(root) / "indicator.png"), "sha256": "2" * 64}
            config["empty_composer_alternates"] = [alternate]
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            for suffix in (".prepare-visual.job.json", ".visual-state.json"):
                path = Path(root) / (tid + suffix)
                driver.private_update(path, dict(driver.private_read(path), emptyComposerAlternates=[alternate]))
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                for replacement in (None, [], [dict(alternate, sha256="3" * 64)]):
                    with self.assertRaisesRegex(driver.Refused, "prior empty composer alternates differ"):
                        driver.prove_unsent(recovery, dict(config, empty_composer_alternates=replacement),
                                            2, driver.time.monotonic() + 3, persist=False)

    def test_recovery_preserves_reference_and_allows_reviewed_addition(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_unsent_artifacts(root, modern=True)
            reference = {"path": str(Path(root) / "empty.png"), "sha256": "1" * 64}
            config["empty_composer_reference"] = reference
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                for suffix in (".prepare-visual.job.json", ".visual-state.json"):
                    path = Path(root) / (tid + suffix)
                    driver.private_update(path, dict(driver.private_read(path), emptyComposerReference=reference))
                driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                for replacement in (None, dict(reference, sha256="2" * 64)):
                    with self.assertRaisesRegex(driver.Refused, "prior empty composer reference differs"):
                        driver.prove_unsent(recovery, dict(config, empty_composer_reference=replacement),
                                            2, driver.time.monotonic() + 3, persist=False)
                job = Path(root) / (tid + ".prepare-visual.job.json")
                driver.private_update(job, dict(driver.private_read(job), emptyComposerReference=None))
                with self.assertRaisesRegex(driver.Refused, "prior prepare state differs"):
                    driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)

    def test_recovery_after_restart_binds_old_pid_to_its_original_evidence(self):
        for modern in (False, True):
            with self.subTest(modern=modern), tempfile.TemporaryDirectory() as root:
                request, config, tid = self._prior_unsent_artifacts(root, modern=modern)
                config["expected_pid"] = 43
                recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
                path = Path(root) / (tid + "-inspect.result.json")
                original = driver.private_read(path)
                with mock.patch.object(driver, "_helper_is_live", return_value=False):
                    with self.assertRaisesRegex(driver.Refused, "prior inspect"):
                        driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                    inspected = dict(original, weChatPID=42,
                        windows=[{"id": 20, "frame": {"x": 0, "y": 0, "width": 900, "height": 650}}],
                        permissions={"accessibility": True, "screenRecording": True, "postEvents": True},
                        session={"onConsole": True, "locked": False})
                    driver.private_update(path, inspected)
                    before = {f.name: f.read_bytes() for f in Path(root).iterdir()}
                    driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                    self.assertEqual(before, {f.name: f.read_bytes() for f in Path(root).iterdir()})
                    for changed in (dict(inspected, weChatPID=43), dict(inspected, windows=[]),
                                    dict(inspected, windows=[{"id": 21, "frame": inspected["windows"][0]["frame"]}]),
                                    dict(inspected, permissions={}), dict(inspected, session={"onConsole": False})):
                        driver.private_update(path, changed)
                        with self.assertRaisesRegex(driver.Refused, "prior inspect"):
                            driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                    driver.private_update(path, inspected)
                    focus = Path(root) / (tid + "-focus.focus-group.job.json")
                    driver.private_update(focus, dict(driver.private_read(focus), expectedPID=43))
                    with self.assertRaisesRegex(driver.Refused, "prior focus request"):
                        driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)

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
        for text in ("", " \n", "bad\x00text", "x" * 2001,
                     "你好 😊\n第二行\n", "你好 😊\n第二行\r\n"):
            with self.subTest(text=repr(text)[:15]), self.assertRaises(driver.Refused):
                driver.request_ok(dict(self.request, text=text), self.config)
        self.assertRegex(driver.request_ok(dict(self.request, text="你好 😊\n第二行"), self.config),
                         r"^send-[0-9a-f]{48}$")

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

    def _prior_empty_inspect_artifacts(self, root):
        ui = Path(root)
        ui.chmod(0o700)
        config = dict(self.config, ui_dir=ui, target_group="Synthetic Group", expected_pid=42)
        request = dict(self.request)
        tid = driver.request_ok(request, config)
        prefix = tid + "-inspect"
        driver.private_create(ui / (tid + ".client-result.json"), {
            "id": tid, "request_id": request["request_id"], "attempt_number": 1,
            "stage": "started", "status": "failed", "error": "WeChat process or window changed"})
        driver.private_create(ui / (prefix + ".inspect.job.json"), {"id": prefix, "action": "inspect"})
        driver.private_create(ui / (prefix + ".result.json"), {
            "id": prefix, "action": "inspect", "ok": True, "weChatPID": 42, "windows": [],
            "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True},
            "session": {"onConsole": True, "locked": None}})
        return request, config, tid

    def test_empty_inspect_recovery_regression(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_empty_inspect_artifacts(root)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                proof = driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3,
                                            persist=False)
            self.assertEqual(proof["prior_focus_mode"], "inspect-only")
            self.assertEqual(proof["report_stage"], "started")
            self.assertFalse((config["ui_dir"] / (tid + ".recovery-proof-to-2.json")).exists())

    def test_empty_inspect_preflight_is_read_only_and_proof_is_single_use(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, tid = self._prior_empty_inspect_artifacts(root)
            recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
            # The production preflight expects an existing shared sender lock.
            driver.private_create(config["ui_dir"] / "sender.lock", {})
            before = {path.name: path.read_bytes() for path in config["ui_dir"].iterdir()}
            with mock.patch.object(driver, "status", return_value=0), \
                 mock.patch.object(driver, "_helper_is_live", return_value=False):
                self.assertTrue(driver.recovery_preflight(recovery, config)["unsent_proven"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in config["ui_dir"].iterdir()})
            driver.private_create(config["ui_dir"] / (tid + "-a2.client-result.json"), {
                "id": tid + "-a2", "request_id": request["request_id"],
                "attempt_number": 2, "recovery_from_attempt": 1,
                "stage": "started", "status": "indeterminate"})
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                proof = driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)
            self.assertEqual(driver.private_read(Path(proof))["prior_focus_mode"], "inspect-only")
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                with self.assertRaises(driver.Refused):
                    driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3, persist=False)
                with self.assertRaises(driver.Refused):
                    driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)
            self.assertEqual(Path(proof).name, tid + ".recovery-proof-to-2.json")

    def test_empty_inspect_recovery_rejects_broken_or_later_evidence(self):
        mutations = ("missing_report", "missing_job", "missing_result", "wrong_report_id",
                     "wrong_request", "missing_request", "wrong_attempt", "missing_attempt",
                     "wrong_stage", "wrong_status", "wrong_error",
                     "wrong_job_id", "wrong_action", "wrong_result_id", "wrong_result_action", "result_not_ok",
                     "wrong_pid", "zero_pid", "nonempty_windows", "missing_windows",
                     "missing_permissions", "bad_permission", "off_console", "locked", "unknown_lock",
                     "focus_job", "prepare_job", "submit_marker", "visual_state", "other_artifact",
                     "later_submit", "later_report", "live_helper", "image", "second_recovery")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                request, config, tid = self._prior_empty_inspect_artifacts(root)
                ui = config["ui_dir"]
                recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
                report_path = ui / (tid + ".client-result.json")
                job_path = ui / (tid + "-inspect.inspect.job.json")
                result_path = ui / (tid + "-inspect.result.json")
                report = driver.private_read(report_path)
                job = driver.private_read(job_path)
                result = driver.private_read(result_path)
                if mutation.startswith("missing_") and mutation in ("missing_report", "missing_job", "missing_result"):
                    {"missing_report": report_path, "missing_job": job_path,
                     "missing_result": result_path}[mutation].unlink()
                elif mutation in ("wrong_report_id", "wrong_request", "missing_request", "wrong_attempt",
                                  "missing_attempt", "wrong_stage", "wrong_status", "wrong_error"):
                    if mutation in ("missing_request", "missing_attempt"):
                        report.pop("request_id" if mutation == "missing_request" else "attempt_number")
                    else:
                        key, value = {"wrong_report_id": ("id", "other"),
                                      "wrong_request": ("request_id", "other"),
                                      "wrong_attempt": ("attempt_number", 2),
                                      "wrong_stage": ("stage", "inspected"),
                                      "wrong_status": ("status", "indeterminate"),
                                      "wrong_error": ("error", "other")}[mutation]
                        report[key] = value
                    driver.private_update(report_path, report)
                elif mutation in ("wrong_job_id", "wrong_action"):
                    job["id" if mutation == "wrong_job_id" else "action"] = "other"
                    driver.private_update(job_path, job)
                elif mutation in ("wrong_result_id", "wrong_result_action", "result_not_ok", "wrong_pid", "zero_pid",
                                  "nonempty_windows", "missing_windows", "missing_permissions",
                                  "bad_permission", "off_console", "locked", "unknown_lock"):
                    if mutation == "wrong_result_id": result["id"] = "other"
                    if mutation == "wrong_result_action": result["action"] = "other"
                    if mutation == "result_not_ok": result["ok"] = False
                    if mutation == "wrong_pid": result["weChatPID"] = 43
                    if mutation == "zero_pid": result["weChatPID"] = 0
                    if mutation == "nonempty_windows": result["windows"] = [{"id": 1}]
                    if mutation == "missing_windows": result.pop("windows")
                    if mutation == "missing_permissions": result.pop("permissions")
                    if mutation == "bad_permission": result["permissions"]["postEvents"] = False
                    if mutation == "off_console": result["session"]["onConsole"] = False
                    if mutation == "locked": result["session"]["locked"] = True
                    if mutation == "unknown_lock": result["session"]["locked"] = "unknown"
                    driver.private_update(result_path, result)
                elif mutation in ("focus_job", "prepare_job", "submit_marker", "visual_state", "other_artifact"):
                    suffix = {"focus_job": "-focus.focus-group.job.json",
                              "prepare_job": ".prepare-visual.job.json",
                              "submit_marker": ".submit-attempted.json",
                              "visual_state": ".visual-state.json",
                              "other_artifact": "-unknown.result.json"}[mutation]
                    driver.private_create(ui / (tid + suffix), {"id": tid})
                elif mutation == "image":
                    recovery = dict(recovery, protocol_version=2, text=None, media={})
                elif mutation == "second_recovery":
                    recovery = dict(recovery, attempt_number=3, recovery_from_attempt=2)
                elif mutation in ("later_submit", "later_report"):
                    driver.private_create(ui / (tid + ("-a2.submit-attempted.json" if mutation == "later_submit"
                                                     else "-a3.client-result.json")), {"id": tid})
                with mock.patch.object(driver, "_helper_is_live", return_value=mutation == "live_helper"):
                    with self.assertRaises(driver.Refused):
                        driver.prove_unsent(recovery, config, recovery["attempt_number"],
                                            driver.time.monotonic() + 3, persist=False)

    def test_empty_inspect_locked_recheck_accepts_only_current_started_report(self):
        for mutation in (None, "current_focus", "other_attempt", "wrong_request", "wrong_stage"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                request, config, tid = self._prior_empty_inspect_artifacts(root)
                ui = config["ui_dir"]
                current_path = ui / (tid + "-a2.client-result.json")
                current = {"id": tid + "-a2", "request_id": request["request_id"],
                           "attempt_number": 2, "recovery_from_attempt": 1,
                           "stage": "started", "status": "indeterminate"}
                if mutation == "wrong_request": current["request_id"] = "other"
                if mutation == "wrong_stage": current["stage"] = "inspected"
                driver.private_create(current_path, current)
                if mutation == "current_focus":
                    driver.private_create(ui / (tid + "-a2-focus.focus-group.job.json"), {"id": tid})
                if mutation == "other_attempt":
                    driver.private_create(ui / (tid + "-a3.client-result.json"), {"id": tid})
                recovery = dict(request, attempt_number=2, recovery_from_attempt=1)
                with mock.patch.object(driver, "_helper_is_live", return_value=False):
                    if mutation is None:
                        proof = driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)
                        self.assertEqual(driver.private_read(Path(proof))["prior_focus_mode"], "inspect-only")
                    else:
                        with self.assertRaises(driver.Refused):
                            driver.prove_unsent(recovery, config, 2, driver.time.monotonic() + 3)

    def _prior_focus_timeout_artifacts(self, root):
        request, config, base = self._prior_empty_inspect_artifacts(root)
        ui = config["ui_dir"]
        prior_proof = ui / (base + ".recovery-proof-to-2.json")
        driver.private_create(prior_proof, {
            "request_id": request["request_id"], "from_attempt": 1,
            "attempt_number": 2, "transaction_id": base,
            "result_error": "WeChat process or window changed", "report_stage": "started",
            "report_status": "failed", "prior_focus_mode": "inspect-only",
            "proof_saved_at": "2026-10-27T06:18:20+00:00"})
        tid = base + "-a2"
        inspect_id = tid + "-inspect"
        focus_id = tid + "-focus"
        frame = {"x": 520, "y": 220, "width": 880, "height": 640}
        config.update(conversation_kind="group", conversation_list_rect={"x": 1, "y": 60, "width": 200, "height": 200},
                      search_point={"x": 20, "y": 30})
        driver.private_create(ui / (tid + ".client-result.json"), {
            "id": tid, "request_id": request["request_id"], "attempt_number": 2,
            "recovery_from_attempt": 1, "recovery_proof": str(prior_proof),
            "stage": "inspected", "status": "indeterminate", "error": "helper action timed out"})
        driver.private_create(ui / (inspect_id + ".inspect.job.json"), {"id": inspect_id, "action": "inspect"})
        driver.private_create(ui / (inspect_id + ".result.json"), {
            "id": inspect_id, "action": "inspect", "ok": True, "weChatPID": 42,
            "windows": [{"id": 835, "frame": frame}],
            "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True},
            "session": {"onConsole": True, "locked": None}})
        driver.private_create(ui / (focus_id + ".focus-group.job.json"), {
            "id": focus_id, "action": "focus-group", "targetGroup": config["target_group"],
            "targetKind": "group", "expectedPID": 42, "expectedWindowID": 835,
            "expectedFrame": frame, "conversationListRect": config["conversation_list_rect"],
            "searchPoint": config["search_point"]})
        driver.private_create(ui / (focus_id + ".result.json"), {
            "id": focus_id, "ok": False, "error": "Cannot snapshot a clipboard item",
            "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}})
        (ui / (focus_id + ".focus-attempted.json")).touch(mode=0o600)
        for suffix in ("before-search-click.png", "before-search-select.png", "focus-initial.png"):
            path = ui / (focus_id + "." + suffix)
            path.write_bytes(b"synthetic pixels")
            path.chmod(0o600)
        return request, config, base, tid

    def test_focus_timeout_recovery_regression(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, _, _ = self._prior_focus_timeout_artifacts(root)
            recovery = dict(request, attempt_number=3, recovery_from_attempt=2)
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                proof = driver.prove_unsent(recovery, config, 3, driver.time.monotonic() + 3,
                                            persist=False)
            self.assertEqual(proof["prior_focus_mode"], "focus-timeout-unsent")

    def test_focus_timeout_preflight_and_locked_proof_are_single_use(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, base, tid = self._prior_focus_timeout_artifacts(root)
            recovery = dict(request, attempt_number=3, recovery_from_attempt=2)
            driver.private_create(config["ui_dir"] / "sender.lock", {})
            before = {path.name: path.read_bytes() for path in config["ui_dir"].iterdir()}
            with mock.patch.object(driver, "status", return_value=0), \
                 mock.patch.object(driver, "_helper_is_live", return_value=False):
                self.assertTrue(driver.recovery_preflight(recovery, config)["unsent_proven"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in config["ui_dir"].iterdir()})
            driver.private_create(config["ui_dir"] / (base + "-a3.client-result.json"), {
                "id": base + "-a3", "request_id": request["request_id"],
                "attempt_number": 3, "recovery_from_attempt": 2,
                "stage": "started", "status": "indeterminate"})
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                proof = driver.prove_unsent(recovery, config, 3, driver.time.monotonic() + 3)
                self.assertEqual(driver.private_read(Path(proof))["prior_focus_mode"], "focus-timeout-unsent")
                with self.assertRaises(driver.Refused):
                    driver.prove_unsent(recovery, config, 3, driver.time.monotonic() + 3)
                with self.assertRaises(driver.Refused):
                    driver.prove_unsent(recovery, config, 3, driver.time.monotonic() + 3, persist=False)
            self.assertEqual(Path(proof).name, tid + ".recovery-proof-to-3.json")

    def test_focus_timeout_preflight_keeps_reconciled_helper_marker_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            request, config, _, tid = self._prior_focus_timeout_artifacts(root)
            ui = config["ui_dir"]
            driver.private_create(ui / "sender.lock", {})
            marker = ui / driver.HELPER_UNRESOLVED
            driver.private_create(marker, {"id": tid + "-focus", "action": "focus-group"})
            before = {path.name: path.read_bytes() for path in ui.iterdir()}
            recovery = dict(request, attempt_number=3, recovery_from_attempt=2)
            with mock.patch.object(driver, "status", return_value=0), \
                 mock.patch.object(driver, "_helper_is_live", return_value=False):
                self.assertTrue(driver.recovery_preflight(recovery, config)["unsent_proven"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in ui.iterdir()})
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                driver._guard_helper(config, driver.time.monotonic() + 3)
            self.assertFalse(marker.exists())

    def test_guard_rejects_unknown_success_without_action(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            marker = ui / driver.HELPER_UNRESOLVED
            driver.private_create(marker, {"id": "test-inspect", "action": "inspect"})
            driver.private_create(ui / "test-inspect.inspect.job.json", {"id": "test-inspect", "action": "inspect"})
            driver.private_create(ui / "test-inspect.result.json", {"id": "test-inspect", "ok": True})
            with mock.patch.object(driver, "_helper_is_live", return_value=False), \
                 self.assertRaisesRegex(driver.Refused, "prior helper terminal result is invalid"):
                driver._guard_helper({"ui_dir": ui}, driver.time.monotonic() + 3)
            self.assertTrue(marker.exists())

    def test_focus_timeout_recovery_rejects_incomplete_or_changed_trace(self):
        mutations = ("wrong_report_request", "wrong_report_error", "wrong_report_stage",
                     "wrong_report_attempt", "wrong_report_proof", "wrong_proof_request",
                     "wrong_proof_mode", "wrong_first_result", "wrong_inspect_pid",
                     "empty_second_windows", "wrong_inspect_window", "bad_inspect_permission",
                     "locked_session", "wrong_focus_target", "wrong_focus_frame",
                     "wrong_focus_error", "focus_ok", "focus_action", "bad_focus_permission",
                     "nonempty_focus_marker", "missing_capture", "missing_focus_result",
                     "missing_old_proof", "prepare_job", "visual_state",
                     "submit_job", "focus_evidence", "unknown_artifact", "early_a3_report",
                     "live_helper", "image", "wrong_recovery_attempt")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                request, config, base, tid = self._prior_focus_timeout_artifacts(root)
                ui = config["ui_dir"]
                report_path = ui / (tid + ".client-result.json")
                proof_path = ui / (base + ".recovery-proof-to-2.json")
                old_result_path = ui / (base + "-inspect.result.json")
                inspect_path = ui / (tid + "-inspect.result.json")
                focus_job_path = ui / (tid + "-focus.focus-group.job.json")
                focus_result_path = ui / (tid + "-focus.result.json")
                changes = {
                    "wrong_report_request": (report_path, "request_id", "other"),
                    "wrong_report_error": (report_path, "error", "other"),
                    "wrong_report_stage": (report_path, "stage", "prepared"),
                    "wrong_report_attempt": (report_path, "attempt_number", 3),
                    "wrong_report_proof": (report_path, "recovery_proof", "other"),
                    "wrong_proof_request": (proof_path, "request_id", "other"),
                    "wrong_proof_mode": (proof_path, "prior_focus_mode", "other"),
                    "wrong_first_result": (old_result_path, "windows", [{"id": 1}]),
                    "wrong_inspect_pid": (inspect_path, "weChatPID", 43),
                    "empty_second_windows": (inspect_path, "windows", []),
                    "wrong_inspect_window": (inspect_path, "windows", [{"id": 0, "frame": {}}]),
                    "bad_inspect_permission": (inspect_path, "permissions", {}),
                    "locked_session": (inspect_path, "session", {"onConsole": True, "locked": True}),
                    "wrong_focus_target": (focus_job_path, "targetGroup", "other"),
                    "wrong_focus_frame": (focus_job_path, "expectedFrame", {}),
                    "wrong_focus_error": (focus_result_path, "error", "other"),
                    "focus_ok": (focus_result_path, "ok", True),
                    "focus_action": (focus_result_path, "action", "focus-group"),
                    "bad_focus_permission": (focus_result_path, "permissions", {}),
                }
                if mutation in changes:
                    path, key, value = changes[mutation]
                    data = driver.private_read(path)
                    data[key] = value
                    driver.private_update(path, data)
                elif mutation == "nonempty_focus_marker":
                    (ui / (tid + "-focus.focus-attempted.json")).write_text("{}")
                elif mutation == "missing_capture":
                    (ui / (tid + "-focus.focus-initial.png")).unlink()
                elif mutation == "missing_focus_result":
                    focus_result_path.unlink()
                elif mutation == "missing_old_proof":
                    proof_path.unlink()
                elif mutation in ("prepare_job", "visual_state", "submit_job", "focus_evidence",
                                  "unknown_artifact", "early_a3_report"):
                    suffix = {"prepare_job": ".prepare-visual.job.json",
                              "visual_state": ".visual-state.json",
                              "submit_job": ".submit-visual.job.json",
                              "focus_evidence": "-focus.focus-evidence.json",
                              "unknown_artifact": "-focus.other.json",
                              "early_a3_report": ""}[mutation]
                    path = ui / (base + "-a3.client-result.json") if mutation == "early_a3_report" else ui / (tid + suffix)
                    driver.private_create(path, {"id": tid})
                recovery = dict(request, attempt_number=3, recovery_from_attempt=2)
                if mutation == "image": recovery = dict(recovery, protocol_version=2, text=None, media={})
                if mutation == "wrong_recovery_attempt": recovery["recovery_from_attempt"] = 1
                with mock.patch.object(driver, "_helper_is_live", return_value=mutation == "live_helper"):
                    with self.assertRaises(driver.Refused):
                        driver.prove_unsent(recovery, config, 3, driver.time.monotonic() + 3, persist=False)

    def test_focus_timeout_locked_recheck_rejects_extra_or_changed_attempt_three(self):
        for mutation in ("extra_focus", "extra_submit", "wrong_request", "wrong_stage", "wrong_attempt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                request, config, base, _ = self._prior_focus_timeout_artifacts(root)
                ui = config["ui_dir"]
                report_path = ui / (base + "-a3.client-result.json")
                report = {"id": base + "-a3", "request_id": request["request_id"],
                          "attempt_number": 3, "recovery_from_attempt": 2,
                          "stage": "started", "status": "indeterminate"}
                if mutation == "wrong_request": report["request_id"] = "other"
                if mutation == "wrong_stage": report["stage"] = "inspected"
                if mutation == "wrong_attempt": report["attempt_number"] = 4
                driver.private_create(report_path, report)
                if mutation.startswith("extra_"):
                    driver.private_create(ui / (base + "-a3" +
                                             ("-focus.focus-group.job.json" if mutation == "extra_focus"
                                              else ".submit-attempted.json")), {"id": base})
                recovery = dict(request, attempt_number=3, recovery_from_attempt=2)
                with mock.patch.object(driver, "_helper_is_live", return_value=False):
                    with self.assertRaises(driver.Refused):
                        driver.prove_unsent(recovery, config, 3, driver.time.monotonic() + 3)

    def test_prior_helper_blocks_new_desktop_actions_even_after_lock_release(self):
        # A timed-out helper launched by `open` can outlive its Python driver
        # and the driver's UI lock. A different request must not overlap it.
        for listing in ("42 /private/WeChatTestBridge --input /private/prior.job.json",
                        driver.Refused("process inspection unavailable")):
            with self.subTest(listing=type(listing).__name__), tempfile.TemporaryDirectory() as root:
                ui = Path(root)
                ui.chmod(0o700)
                config = dict(self.config, ui_dir=ui)
                driver.private_create(ui / "previous.client-result.json", {"status": "indeterminate"})
                fd = os.open(ui / "sender.lock", os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
                before = {path.name: path.read_bytes() for path in ui.iterdir()}
                with mock.patch.object(driver, "call", side_effect=(listing if isinstance(listing, Exception)
                                                                    else None), return_value=listing), \
                     mock.patch.object(driver, "status", return_value=0), \
                     mock.patch.object(driver, "bridge") as bridge:
                    for action in (lambda: driver.focus_only(config), lambda: driver.run(self.request, config)):
                        with self.assertRaisesRegex(driver.Refused, "prior helper process"):
                            action()
                    bridge.assert_not_called()
                self.assertEqual(before, {path.name: path.read_bytes() for path in ui.iterdir()})

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
        trailing = dict(self.request, text="Hello 😊\n")
        self.assertFalse(driver.matching_event(event, trailing, "wxid-self", 10))

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
            with mock.patch.object(driver, "call", side_effect=fake_open), \
                 mock.patch.object(driver, "_helper_is_live", return_value=False):
                with self.assertRaisesRegex(driver.Uncertain, "^bridge permissions missing$"):
                    driver.bridge({"ui_dir": ui, "helper_app": "/private/bridge.app"}, tid,
                                  "inspect", {}, driver.time.monotonic() + 2, 1)

    def test_bridge_collects_late_focus_failure_while_holding_ui_guard(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            tid = "focus-late"
            config = {"ui_dir": ui, "helper_app": "/private/bridge.app"}
            clock = [0.0]
            def advance(seconds):
                clock[0] += seconds
                if clock[0] >= 23 and not (ui / (tid + ".result.json")).exists():
                    driver.private_create(ui / (tid + ".result.json"), {
                        "id": tid, "ok": False, "error": "Cannot snapshot a clipboard item",
                        "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}})
            with mock.patch.object(driver.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(driver.time, "sleep", side_effect=advance), \
                 mock.patch.object(driver, "call", return_value=""), \
                 mock.patch.object(driver, "_helper_is_live", side_effect=lambda *_: clock[0] < 24):
                with self.assertRaisesRegex(driver.Uncertain,
                                            "focus failed before message preparation: clipboard state changed"):
                    driver.bridge(config, tid, "focus-group", {}, 48.0, 18.0)
            self.assertGreaterEqual(clock[0], 24)
            self.assertFalse((ui / driver.HELPER_UNRESOLVED).exists())

    def test_unresolved_helper_blocks_later_ui_until_terminal_result_and_exit(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            tid = "focus-unresolved"
            config = {"ui_dir": ui, "helper_app": "/private/bridge.app"}
            clock = [0.0]
            with mock.patch.object(driver.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(driver.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                 mock.patch.object(driver, "call", return_value=""), \
                 mock.patch.object(driver, "_helper_is_live", return_value=True):
                with self.assertRaisesRegex(driver.Uncertain, "focus before message preparation helper result or exit unresolved"):
                    driver.bridge(config, tid, "focus-group", {}, 48.0, 18.0)
                self.assertLessEqual(clock[0], 54)
                with self.assertRaisesRegex(driver.Refused, "prior helper result or exit is unresolved"):
                    driver._guard_helper(config, clock[0] + 8)
            marker = ui / driver.HELPER_UNRESOLVED
            self.assertTrue(marker.exists())
            driver.private_create(ui / (tid + ".result.json"), {
                "id": tid, "ok": False, "error": "Cannot snapshot a clipboard item",
                "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}})
            with mock.patch.object(driver, "_helper_is_live", return_value=True):
                with self.assertRaises(driver.Refused):
                    driver._guard_helper(config, driver.time.monotonic() + 8)
            with mock.patch.object(driver, "_helper_is_live", return_value=False):
                driver._guard_helper(config, driver.time.monotonic() + 8)
            self.assertFalse(marker.exists())

    def test_submit_timeout_remains_distinct_and_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            clock = [0.0]
            with mock.patch.object(driver.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(driver.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                 mock.patch.object(driver, "call", return_value=""), \
                 mock.patch.object(driver, "_helper_is_live", return_value=True):
                with self.assertRaisesRegex(driver.Uncertain, "submit-visual helper result or exit unresolved"):
                    driver.bridge({"ui_dir": ui, "helper_app": "/private/bridge.app"},
                                  "submit-unknown", "submit-visual", {}, 48.0, 18.0)
            self.assertTrue((ui / driver.HELPER_UNRESOLVED).exists())

    def test_late_start_focus_timeout_stays_within_total_budget(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            clock = [40.0]  # Prior status, target lookup and inspect used the budget.
            with mock.patch.object(driver.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(driver.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                 mock.patch.object(driver, "call", return_value=""):
                with self.assertRaisesRegex(driver.Uncertain,
                                            "focus before message preparation helper result or exit unresolved"):
                    driver.bridge({"ui_dir": ui, "helper_app": "/private/bridge.app"},
                                  "late-focus", "focus-group", {}, 48.0, 18.0)
            self.assertLess(clock[0], 49.0)
            self.assertTrue((ui / driver.HELPER_UNRESOLVED).exists())

    def test_unresolved_marker_refuses_focus_and_send_before_desktop_action(self):
        with tempfile.TemporaryDirectory() as root:
            ui = Path(root)
            ui.chmod(0o700)
            config = dict(self.config, ui_dir=ui)
            driver.private_create(ui / driver.HELPER_UNRESOLVED,
                                  {"id": "prior-focus", "action": "focus-group"})
            driver.private_create(ui / "prior-focus.focus-group.job.json",
                                  {"id": "prior-focus", "action": "focus-group"})
            with mock.patch.object(driver, "_helper_is_live", return_value=False), \
                 mock.patch.object(driver, "status"), \
                 mock.patch.object(driver, "bridge") as bridge:
                with self.assertRaisesRegex(driver.Refused, "prior helper result or exit is unresolved"):
                    driver.focus_only(config)
                with self.assertRaisesRegex(driver.Refused, "prior helper result or exit is unresolved"):
                    driver.run(self.request, config)
            bridge.assert_not_called()
            self.assertFalse(list(ui.glob("*.client-result.json")))

    def test_slow_focus_completion_or_failure_preserves_single_attempt(self):
        # Native search has a six-second polling budget *after* search setup.
        # Exercise the real bridge wait, including a result later than eight
        # seconds, without operating the desktop or sleeping in real time.
        for focus_only, focus_ok in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(focus_only=focus_only, focus_ok=focus_ok), tempfile.TemporaryDirectory() as root:
                ui = Path(root)
                ui.chmod(0o700)
                config = dict(self.config, ui_dir=ui, target_group="Synthetic Group",
                              helper_app="/private/bridge.app", expected_pid=42,
                              window_size={"width": 880, "height": 640},
                              composer_rect={"x": 320, "y": 500, "width": 400, "height": 80},
                              button_point={"x": 800, "y": 610},
                              conversation_list_rect={"x": 60, "y": 55, "width": 240, "height": 500},
                              search_point={"x": 155, "y": 28})
                frame = {"x": 0, "y": 0, "width": 880, "height": 640}
                clock = [0.0]
                pending = []
                actions = []

                def advance(seconds):
                    clock[0] += seconds
                    if pending and clock[0] >= pending[0][0]:
                        _, tid, command = pending.pop()
                        if not focus_ok:
                            driver.private_create(ui / (tid + ".result.json"), {
                                "id": tid, "ok": False, "error": "Selected chat header is not exact target and kind",
                                "permissions": {"accessibility": True, "screenRecording": True, "postEvents": True}})
                            return
                        evidence = {key: command[key] for key in
                                    ("targetGroup", "targetKind", "expectedPID", "expectedWindowID", "expectedFrame")}
                        evidence.update(id=tid, source="search", category="group-chats", selectedAt=1234.5)
                        driver.private_create(ui / (tid + ".focus-evidence.json"), evidence)
                        driver.private_create(ui / (tid + ".result.json"), {
                            "id": tid, "action": "focus-group", "ok": True, "focused": True, "alreadySelected": False,
                            "source": "search", "category": "group-chats", "targetKind": "group",
                            "focusEvidenceID": tid})

                def fake_open(argv, _deadline, limit=driver.MAX_OUTPUT):
                    self.assertNotEqual(argv[:2], ["/usr/bin/open", "-a"],
                                        "focus must not reopen the WeChat app")
                    if "--input" not in argv:
                        return ""
                    command = driver.private_read(Path(argv[-1]))
                    tid, action = command["id"], command["action"]
                    actions.append(action)
                    result = {"id": tid, "action": action, "ok": True}
                    suffix = ".result.json"
                    if action == "inspect":
                        result.update(weChatPID=42, windows=[{"id": 20, "frame": frame}],
                                      session={"onConsole": True, "locked": False},
                                      permissions={"accessibility": True, "screenRecording": True,
                                                   "postEvents": True})
                    elif action == "focus-group":
                        pending.append((clock[0] + 9, tid, command))
                        return ""
                    else:
                        self.assertFalse(pending)
                        result["prepared" if action == "prepare-visual" else "submitted"] = True
                        suffix = "." + action + ".result.json"
                    driver.private_create(ui / (tid + suffix), result)
                    return ""

                with mock.patch.object(driver.time, "monotonic", side_effect=lambda: clock[0]), \
                     mock.patch.object(driver.time, "sleep", side_effect=advance), \
                     mock.patch.object(driver, "call", side_effect=fake_open), \
                     mock.patch.object(driver, "status", side_effect=lambda *_: (advance(4), 0)[1]), \
                     mock.patch.object(driver, "verify_target", side_effect=lambda *_: advance(5)), \
                     mock.patch.object(driver, "channel", return_value="{}"), \
                     mock.patch.object(driver, "observe", return_value=1):
                    if focus_only:
                        if focus_ok:
                            self.assertTrue(driver.focus_only(config)["focused"])
                        else:
                            with self.assertRaisesRegex(driver.Uncertain, "header mismatch"):
                                driver.focus_only(config)
                        self.assertEqual(actions, ["inspect", "focus-group"])
                    else:
                        result = driver.run(self.request, config)
                        self.assertEqual(result["status"], "ui_observed" if focus_ok else "indeterminate")
                        self.assertEqual(actions, ["inspect", "focus-group"] +
                                         (["prepare-visual", "submit-visual"] if focus_ok else []))
                        artifacts = {path.name: path.read_bytes() for path in ui.iterdir()}
                        with self.assertRaisesRegex(driver.Uncertain, "already attempted"):
                            driver.run(self.request, config)
                        self.assertEqual(artifacts, {path.name: path.read_bytes() for path in ui.iterdir()})

    def test_activation_refusal_stops_before_draft_and_keeps_original_attempt(self):
        for focus_only in (True, False):
            with self.subTest(focus_only=focus_only), tempfile.TemporaryDirectory() as root:
                ui = Path(root)
                ui.chmod(0o700)
                config = dict(self.config, ui_dir=ui, target_group="Synthetic Group", expected_pid=42,
                              window_size={"width": 880, "height": 640},
                              conversation_list_rect={"x": 60, "y": 55, "width": 240, "height": 500},
                              search_point={"x": 155, "y": 28})
                frame = {"x": 10, "y": 20, "width": 880, "height": 640}
                actions = []
                def fake_bridge(_config, _tid, action, command, _deadline, _seconds):
                    actions.append(action)
                    if action == "inspect":
                        return {"weChatPID": 42, "windows": [{"id": 20, "frame": frame}],
                                "session": {"onConsole": True, "locked": False},
                                "permissions": {"accessibility": True, "screenRecording": True,
                                                "postEvents": True}}
                    self.assertEqual(action, "focus-group")
                    self.assertEqual(command["expectedPID"], 42)
                    self.assertEqual(command["expectedWindowID"], 20)
                    self.assertEqual(command["expectedFrame"], frame)
                    raise driver.Uncertain("WeChat focus changed")
                with mock.patch.object(driver, "_helper_is_live", return_value=False), \
                     mock.patch.object(driver, "status", return_value=0), \
                     mock.patch.object(driver, "verify_target"), \
                     mock.patch.object(driver, "bridge", side_effect=fake_bridge), \
                     mock.patch.object(driver, "call", side_effect=AssertionError("unexpected app launch")):
                    if focus_only:
                        with self.assertRaisesRegex(driver.Uncertain, "focus changed"):
                            driver.focus_only(config)
                    else:
                        self.assertEqual(driver.run(self.request, config)["status"], "indeterminate")
                        before = {path.name: path.read_bytes() for path in ui.iterdir()}
                        with self.assertRaisesRegex(driver.Uncertain, "already attempted"):
                            driver.run(self.request, config)
                        self.assertEqual(before, {path.name: path.read_bytes() for path in ui.iterdir()})
                self.assertEqual(actions, ["inspect", "focus-group"])

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
                 mock.patch.object(driver, "_helper_is_live", return_value=False), \
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


class ImageAbandonProofTests(unittest.TestCase):
    def test_image_abandonment_has_a_separate_capability_without_changing_recovery(self):
        for flag, expected in (
                ("--abandon-capabilities", {"protocol_version": 1, "abandon_protocol_version": 1,
                    "image_before_prepare_only": True, "pre_submit_proof_required": True}),
                ("--recovery-capabilities", {"protocol_version": 1, "recovery_protocol_version": 1,
                    "pre_submit_proof_required": True})):
            output = io.StringIO()
            with self.subTest(flag=flag), contextlib.redirect_stdout(output):
                self.assertEqual(driver.main([flag]), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)

    def fixture(self, directory):
        ui = Path(directory).resolve(); ui.chmod(0o700)
        (ui / "sender.lock").touch(mode=0o600)
        request = {"protocol_version": 1, "abandon_protocol_version": 1,
                   "content_kind": "image", "request_id": "image-before-prepare",
                   "attempt_number": 1, "channel_id": "channel-one",
                   "session_id": "session-one", "account": "synthetic-account",
                   "talker": "synthetic@chatroom"}
        config = {"ui_dir": ui, "binding": {k: request[k] for k in
                  ("channel_id", "session_id", "account", "talker")},
                  "target_name": "Synthetic Group", "target_group": "Synthetic Group",
                  "conversation_kind": "group", "expected_pid": 42,
                  "window_size": {"width": 880, "height": 640},
                  "conversation_list_rect": {"x": 60, "y": 55, "width": 240, "height": 570},
                  "search_point": {"x": 155, "y": 28}}
        tid = "send-" + hashlib.sha256(b"channel-one\0image-before-prepare").hexdigest()[:48]
        focus = tid + "-focus"; inspect = tid + "-inspect"
        permissions = {"accessibility": True, "screenRecording": True, "postEvents": True}
        frame = {"x": 100, "y": 100, "width": 880, "height": 640}
        driver.private_create(ui / (tid + ".client-result.json"), {
            "id": tid, "request_id": request["request_id"], "attempt_number": 1,
            "channel_id": request["channel_id"], "session_id": request["session_id"],
            "stage": "inspected", "status": "indeterminate",
            "error": "focus failed before message preparation: helper rejected action"})
        driver.private_create(ui / (inspect + ".inspect.job.json"), {"id": inspect, "action": "inspect"})
        driver.private_create(ui / (inspect + ".result.json"), {
            "id": inspect, "action": "inspect", "ok": True, "weChatPID": 42,
            "windows": [{"id": 21, "frame": frame}], "permissions": permissions,
            "session": {"onConsole": True, "locked": False}})
        driver.private_create(ui / (focus + ".focus-group.job.json"), {
            "id": focus, "action": "focus-group", "targetGroup": "Synthetic Group",
            "targetKind": "group", "expectedPID": 42, "expectedWindowID": 21,
            "expectedFrame": frame, "conversationListRect": config["conversation_list_rect"],
            "searchPoint": config["search_point"]})
        driver.private_create(ui / (focus + ".result.json"), {
            "id": focus, "ok": False,
            "error": "Search popup not enumerated by ScreenCaptureKit within six seconds",
            "permissions": permissions})
        driver.private_create(ui / (focus + ".focus-polls.json"), [
            {"elapsedMs": elapsed, "windowCount": 1, "popupPresent": False,
             "popupSelected": False, "windows": [{"id": 21, **frame}]}
            for elapsed in (10, 5500)])
        marker = ui / (focus + ".focus-attempted.json"); marker.touch(mode=0o600)
        return request, config, tid

    def prove(self, request, config):
        with mock.patch.object(driver, "_helper_is_live", return_value=False), \
             mock.patch.object(driver, "status"), \
             mock.patch.object(driver, "bridge", side_effect=AssertionError("proof must not operate UI")):
            return driver.abandon_preflight(request, config)

    def test_image_focus_failure_proof_is_read_only_and_never_authorizes_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            request, config, _ = self.fixture(directory)
            before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
            self.assertEqual(self.prove(request, config), {
                "protocol_version": 1, "abandon_protocol_version": 1,
                "unsent_proven": True, "before_prepare": True})
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(directory).iterdir()})
            with self.assertRaises(driver.Refused):
                driver.request_ok(dict(request, protocol_version=2), config)

    def test_prepare_submit_or_later_attempt_trace_refuses_image_abandonment(self):
        for suffix in (".prepare-visual.job.json", ".prepare-visual.result.json",
                       ".visual-state.json", ".submit-attempted.json",
                       ".submit-visual.job.json", ".submit-visual.result.json",
                       "-a2.client-result.json", "-a3-inspect.result.json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                request, config, tid = self.fixture(directory)
                driver.private_create(Path(directory) / (tid + suffix), {})
                with self.assertRaises(driver.Refused): self.prove(request, config)

    def test_missing_mismatched_or_nonterminal_native_proof_refuses(self):
        mutations = (
            (".client-result.json", {"stage": "group_focused"}),
            (".client-result.json", {"media_prepared": {"exact_pixels": True}}),
            (".client-result.json", {"focus_evidence_id": "contradictory-focus-success"}),
            (".client-result.json", {"attempt_number": 2}),
            ("-inspect.result.json", {"permissions": {}}),
            ("-inspect.result.json", {"weChatPID": 43}),
            ("-focus.focus-group.job.json", {"targetGroup": "Another Group"}),
            ("-focus.result.json", {"ok": True}),
            ("-focus.result.json", {"error": "Cannot snapshot a clipboard item"}),
            ("-focus.result.json", None),
        )
        for suffix, changed in mutations:
            with self.subTest(suffix=suffix, changed=changed), tempfile.TemporaryDirectory() as directory:
                request, config, tid = self.fixture(directory); path = Path(directory) / (tid + suffix)
                if changed is None: path.unlink()
                else: driver.private_update(path, dict(driver.private_read(path), **changed))
                with self.assertRaises(driver.Refused): self.prove(request, config)
        with tempfile.TemporaryDirectory() as directory:
            request, config, _ = self.fixture(directory)
            with mock.patch.object(driver, "_helper_is_live", return_value=True), \
                 mock.patch.object(driver, "status"):
                with self.assertRaises(driver.Refused): driver.abandon_preflight(request, config)
            for changed in ({"attempt_number": 2}, {"talker": "another@chatroom"},
                            {"content_kind": "text"}, {"recovery_from_attempt": 1},
                            {"protocol_version": True}, {"attempt_number": True}):
                with self.subTest(changed=changed), self.assertRaises(driver.Refused):
                    self.prove(dict(request, **changed), config)

    def test_missing_or_inconsistent_timeout_polls_and_busy_sender_refuse(self):
        for replacement in (None, [], [{"elapsedMs": 6000}],
                            [{"elapsedMs": 5500, "windowCount": 2, "popupPresent": True,
                              "popupSelected": False, "windows": []}]):
            with self.subTest(polls=replacement), tempfile.TemporaryDirectory() as directory:
                request, config, tid = self.fixture(directory)
                path = Path(directory) / (tid + "-focus.focus-polls.json")
                if replacement is None: path.unlink()
                else: driver.private_update(path, replacement)
                with self.assertRaises((driver.Refused, OSError)):
                    self.prove(request, config)
        with tempfile.TemporaryDirectory() as directory:
            request, config, _ = self.fixture(directory)
            with (Path(directory) / "sender.lock").open() as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(driver.Refused): self.prove(request, config)


if __name__ == "__main__":
    unittest.main()
