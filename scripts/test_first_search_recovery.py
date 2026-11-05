"""Narrow no-selection recovery proof; all native work is forbidden in probes."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wx_agent import macos_sender_driver as sender
from scripts import test_macos_sender_driver as fixtures


class FirstSearchRecoveryTests(unittest.TestCase):
    def fixture(self, directory):
        request, config, tid = fixtures.ImageAbandonProofTests.fixture(self, directory)
        request.pop("abandon_protocol_version")
        request.pop("content_kind")
        request.update(text="Original frozen text\n正文", attempt_number=2, recovery_from_attempt=1)
        ui = Path(directory)
        failed = sender.private_read(ui / (tid + "-focus.result.json"))
        failed["error"] = sender.FOCUS_SEARCH_REJECTION_ERROR
        sender.private_update(ui / (tid + "-focus.result.json"), failed)
        frame = {"x": 100, "y": 100, "width": 880, "height": 640}
        sender.private_update(ui / (tid + "-focus.focus-polls.json"), [
            {"elapsedMs": elapsed, "windowCount": 2, "popupPresent": True, "popupSelected": False,
             "windows": [{"id": 21, **frame}, {"id": 22, "x": 148, "y": 135, "width": 368, "height": 194}]}
            for elapsed in (500, 5500)])
        for suffix in ("focus-initial", "before-search-click", "before-search-select", "before-search-paste",
                       "before-search-copy", "search-copy-ready", "focus-search-result-main",
                       "focus-search-result-popup", "focus-search-result-confirm"):
            p = ui / (tid + "-focus." + suffix + ".png")
            p.write_bytes(b"synthetic private frame"); p.chmod(0o600)
        return request, config, tid

    def proof(self, request, config, persist=False):
        with mock.patch.object(sender, "status"), mock.patch.object(sender, "_helper_is_live", return_value=False), \
             mock.patch.object(sender, "bridge", side_effect=AssertionError("no native action in proof")):
            if not persist:
                return sender.focus_recovery_preflight(request, config)
            return sender.prove_unsent(request, config, 2, sender.time.monotonic() + 20)

    def test_complete_original_proof_is_readonly_and_separate_from_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            request, config, _ = self.fixture(directory)
            before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
            self.assertEqual(self.proof(request, config), {"protocol_version": 1,
                "focus_recovery_protocol_version": 1, "unsent_proven": True})
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(directory).iterdir()})

    def test_recovered_attempt_records_proof_after_its_started_report_only(self):
        with tempfile.TemporaryDirectory() as directory:
            request, config, tid = self.fixture(directory)
            current = {"id": tid + "-a2", "attempt_number": 2, "request_id": request["request_id"],
                "channel_id": request["channel_id"], "session_id": request["session_id"],
                "stage": "started", "status": "indeterminate", "started_at": "synthetic-time", "recovery_from_attempt": 1}
            sender.private_create(Path(directory) / (tid + "-a2.client-result.json"), current)
            with self.assertRaises(sender.Refused): self.proof(request, config)
            proof = sender.private_read(Path(self.proof(request, config, persist=True)))
            self.assertEqual(proof["text_sha256"], hashlib.sha256(request["text"].encode()).hexdigest())
            self.assertEqual(proof["prior_focus_mode"], "first-search-no-selection")
            self.assertEqual(proof["from_attempt"], 1)

    def test_any_later_effect_missing_file_link_or_wrong_failure_refuses(self):
        for mutation in ("submit", "prepare", "selected", "header-error", "missing", "linked", "live", "changed-target", "second-attempt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                request, config, tid = self.fixture(directory); ui = Path(directory)
                if mutation == "submit": sender.private_create(ui / (tid + ".submit-attempted.json"), {})
                elif mutation == "prepare": sender.private_create(ui / (tid + ".prepare-visual.job.json"), {})
                elif mutation == "selected":
                    p = ui / (tid + "-focus.focus-polls.json"); v = sender.private_read(p);v[-1]["popupSelected"] = True;sender.private_update(p,v)
                elif mutation == "header-error":
                    p = ui / (tid + "-focus.result.json");v=sender.private_read(p);v["error"]=sender.FOCUS_HEADER_REJECTION_ERROR;sender.private_update(p,v)
                elif mutation == "missing": (ui / (tid + "-focus.focus-search-result-popup.png")).unlink()
                elif mutation == "linked":
                    p=ui/(tid+"-focus.focus-search-result-popup.png");p.rename(ui/"original.png");p.symlink_to(ui/"original.png")
                elif mutation == "changed-target": config["target_group"]="Different Group"
                elif mutation == "second-attempt": request.update(attempt_number=3,recovery_from_attempt=2)
                if mutation == "live":
                    with mock.patch.object(sender,"_helper_is_live",return_value=True), mock.patch.object(sender,"status"), self.assertRaises(sender.Refused):
                        sender.focus_recovery_preflight(request,config)
                else:
                    with self.assertRaises(sender.Refused): self.proof(request,config)

    def test_proof_rechecks_artifact_hashes_before_authorizing(self):
        with tempfile.TemporaryDirectory() as directory:
            request, config, tid = self.fixture(directory)
            def changed(*_):
                p=Path(directory)/(tid+"-focus.focus-search-result-popup.png");p.write_bytes(b"changed");return False
            with mock.patch.object(sender,"_helper_is_live",side_effect=changed),mock.patch.object(sender,"status"),self.assertRaises(sender.Refused):
                sender.focus_recovery_preflight(request,config)


if __name__ == "__main__": unittest.main()
