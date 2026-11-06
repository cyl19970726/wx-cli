"""Offline exact prepare-header recovery admission against the compiled CLI.

The proof driver is synthetic; Python/native structural proof is tested at its
own layer. These tests exercise Rust admission, fencing, immutable history and
strictly separate recovery protocols without operating the desktop.
"""
import json
import os
import unittest

from scripts import test_managed_focus_recovery as focus


class PreparedHeaderRecoveryTests(focus.ManagedFocusRecoveryTests):
    recovery_command = "recover-prepare-header"
    capability_mode = "--prepare-header-recovery-capabilities"
    proof_mode = "--prepare-header-recovery-preflight"
    audit_label = "prepare_header_recovery"
    review_reason = "reviewed synthetic first prepare header rejection"

    def setUp(self):
        super().setUp()
        self.report.update(stage="group_focused", error="target group header mismatch",
                           target_kind="group", focus_evidence_id=self.tid + "-focus",
                           focus_category="group-header")
        self.write_artifact(".client-result.json", self.report)
        self.write_artifact("-focus.result.json", {
            "id": self.tid + "-focus", "ok": True,
            "result": {"focused": True, "alreadySelected": True, "source": "current-header",
                       "category": "group-header", "headerConfirmations": 2},
        })
        for suffix, value in {
            "-focus.focus-evidence.json": {"id": self.tid + "-focus", "source": "current-header",
                                           "category": "group-header", "headerConfirmations": 2},
            ".prepare-visual.job.json": {"id": self.tid, "action": "prepare-visual",
                                         "text": "synthetic reply"},
            ".prepare-visual.result.json": {"id": self.tid, "ok": False,
                "error": "Fresh chat header is not exact target and kind",
                "permissions": {"accessibility": True, "postEvents": True, "screenRecording": True}},
            ".visual-state.json": {"id": self.tid, "stage": "prepare-attempted", "text": "synthetic reply"},
        }.items():
            self.write_artifact(suffix, value)
        # Complete UI semantics belong to the real Python proof driver. Include
        # the current-header and post-paste boundary screenshots in Rust hashes.
        for suffix in ("-focus.focus-initial.png", "-focus.current-header-confirmed.png", ".prepare-start.png",
                       ".old-draft-before-click.png", ".old-draft-before-select.png", ".old-draft-before-copy.png",
                       ".old-draft-copy-ready.png", ".old-draft-empty-check.png",
                       ".replace-draft-before-paste.png", ".new-draft-before-click.png"):
            path = self.ui / (self.tid + suffix)
            path.write_bytes(focus.fixtures.PNG)
            path.chmod(0o600)
        driver = self.registered.read_text()
        driver = (driver.replace("--focus-recovery-capabilities", self.capability_mode)
                  .replace("--focus-recovery-preflight", self.proof_mode)
                  .replace("focus_recovery_protocol_version", "prepare_header_recovery_protocol_version")
                  .replace("first_text_before_prepare_only", "first_text_no_submit_only")
                  .replace("focus_recovery %", self.audit_label + " %"))
        self.registered.write_text(driver)

    def write_artifact(self, suffix, value):
        path = self.ui / (self.tid + suffix)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_not_enumerated_search_keeps_recovery_identity_and_single_attempt(self):
        # Unlike search recovery, this API cannot accept either old search stop.
        for error in (
            "Search popup had no unique category-eligible exact target within six seconds",
            "Search popup not enumerated by ScreenCaptureKit within six seconds",
        ):
            with self.subTest(error=error):
                self.write_artifact(".prepare-visual.result.json", {
                    "id": self.tid, "ok": False, "error": error,
                    "permissions": {"accessibility": True, "postEvents": True, "screenRecording": True}})
                self.recover(okay=False)
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_other_native_errors_never_probe_or_change_history(self):
        path = self.ui / (self.tid + ".prepare-visual.result.json")
        original = json.loads(path.read_text())
        cases = [dict(error=error) for error in (
            "Selected chat header is not exact target and kind",
            "Fresh chat header is not exact target and kind ",
            "Composer Copy did not replace text sentinel exactly once",
            "Expected exactly one visible Send label in lower conversation pane",
        )]
        cases += [dict(ok=True), dict(id=self.tid + "-a2"), dict(extra=True),
                  dict(permissions={"accessibility": True, "postEvents": False, "screenRecording": True})]
        for changes in cases:
            with self.subTest(changes=changes):
                self.write_artifact(".prepare-visual.result.json", dict(original, **changes))
                self.recover(okay=False)
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_prepare_submit_click_and_later_artifacts_or_wrong_error_rejected(self):
        for suffix in (".submit-visual.job.json", ".submit-visual.result.json",
                       ".submit-attempted.json", ".prepared.json", ".currentstate.json",
                       ".new-draft-before-select.png", ".new-draft-copy-ready.png",
                       "-a2.client-result.json", "-a3.client-result.json", ".late-observation.json"):
            with self.subTest(suffix=suffix):
                path = self.ui / (self.tid + suffix)
                path.write_text("{}"); path.chmod(0o600)
                self.recover(okay=False)
                path.unlink()
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_prepared_submitted_or_wrong_visual_identity_rejected(self):
        for changes in (dict(stage="prepared"), dict(stage="submit-attempted"),
                        dict(stage="submitted-unverified"), dict(id=self.tid + "-a2")):
            with self.subTest(changes=changes):
                self.write_artifact(".visual-state.json", dict(id=self.tid, stage="prepare-attempted") | changes)
                self.recover(okay=False)
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_report_must_be_exact_original_group_focused_indeterminate_rejection(self):
        for changes in (dict(stage="inspected"), dict(stage="prepared"), dict(status="failed"),
                        dict(error="focus failed before message preparation: target group header mismatch"),
                        dict(attempt_number=2), dict(request_id="other-request"), dict(channel_id="other"),
                        dict(session_id="other"), dict(prepared=True), dict(recovery_from_attempt=1),
                        dict(target_kind="private"), dict(focus_evidence_id="other-focus"),
                        dict(focus_category="group-chats")):
            with self.subTest(changes=changes):
                self.write_artifact(".client-result.json", dict(self.report, **changes))
                self.recover(okay=False)
                self.unchanged()
                self.assertEqual(self.calls(), [])

    def test_missing_original_artifact_or_unsafe_proof_rejected(self):
        for suffix in (".client-result.json", "-inspect.inspect.job.json", "-inspect.result.json",
                       "-focus.focus-group.job.json", "-focus.result.json", "-focus.focus-evidence.json",
                       ".prepare-visual.job.json", ".prepare-visual.result.json", ".visual-state.json",
                       "-focus.focus-initial.png", "-focus.current-header-confirmed.png", ".prepare-start.png",
                       ".old-draft-before-click.png", ".old-draft-before-select.png", ".old-draft-before-copy.png",
                       ".old-draft-copy-ready.png", ".old-draft-empty-check.png",
                       ".replace-draft-before-paste.png", ".new-draft-before-click.png"):
            with self.subTest(suffix=suffix):
                path = self.ui / (self.tid + suffix)
                raw = path.read_bytes(); path.unlink()
                self.recover(okay=False)
                path.write_bytes(raw); path.chmod(0o600)
                self.unchanged()
                self.assertEqual(self.calls(), [])
        path = self.ui / (self.tid + ".prepare-start.png")
        path.chmod(0o644)
        self.recover(okay=False)
        path.chmod(0o600)
        self.assertEqual(self.calls(), [])
        link = self.ui / "synthetic-header-link"
        os.link(path, link)
        self.recover(okay=False)
        link.unlink()
        self.assertEqual(self.calls(), [])

    def test_old_focus_command_cannot_admit_prepare_header_failure(self):
        self.recovery_command = "recover-unsent"
        self.recover(okay=False)
        self.unchanged()
        self.assertEqual(self.calls(), [])

    def test_prepare_header_capability_cannot_fall_back_to_old_focus_protocol(self):
        driver = self.registered.read_text()
        self.registered.write_text(driver.replace("prepare_header_recovery_protocol_version",
                                                   "focus_recovery_protocol_version"))
        self.recover(okay=False)
        self.unchanged()
        self.assertEqual(self.calls(), [self.capability_mode])

    def test_proof_races_include_native_state_and_png_before_claim(self):
        driver = self.registered.read_text()
        for suffix in (".visual-state.json", ".new-draft-before-click.png"):
            with self.subTest(suffix=suffix):
                path = self.ui / (self.tid + suffix)
                raw = path.read_bytes()
                changed = driver.replace("result=dict(protocol_version=1,prepare_header_recovery_protocol_version=1,unsent_proven=True)",
                    "pathlib.Path(" + repr(str(path)) + ").write_bytes(b'changed')\n        "
                    "result=dict(protocol_version=1,prepare_header_recovery_protocol_version=1,unsent_proven=True)")
                self.registered.write_text(changed)
                self.recover(okay=False)
                path.write_bytes(raw)
                self.unchanged()
                self.assertNotIn("execute", self.calls())
        self.registered.write_text(driver)


if __name__ == "__main__":
    unittest.main()
