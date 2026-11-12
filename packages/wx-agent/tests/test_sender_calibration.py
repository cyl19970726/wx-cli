"""Synthetic tests for private per-conversation sender calibration."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from wx_agent import conversations as conv
from wx_agent import sender_calibration as calibration


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
)


class SenderCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.ui = self.root / "ui"
        self.ui.mkdir(mode=0o700)
        self.binding = {"channel_id": "channel-a", "session_id": "session-a",
                        "account": "synthetic-account", "talker": "room@chatroom",
                        "source": "/synthetic/source"}
        self.cid = "a" * 32
        self.template = {
            "protocol_version": 1, "ui_dir": str(self.ui),
            "helper_sha256": "1" * 64,
            "window_size": {"width": 800, "height": 600},
            "composer_rect": {"x": 10, "y": 500, "width": 1, "height": 1},
            "empty_composer_reference": {"path": "/shared/default.png", "sha256": "2" * 64},
            "empty_composer_alternates": [], "expected_draft": "",
            "sender_executable": "/synthetic/sender", "target_name": "Synthetic",
            "binding": self.binding.copy(),
        }
        text_chunk = b"tEXt" + b"comment\x00alternate"
        png_alternate = (PNG_1X1[:-12] + struct.pack(">I", len(text_chunk)) + text_chunk +
                         struct.pack(">I", zlib.crc32(text_chunk) & 0xffffffff) + PNG_1X1[-12:])
        self.refs = []
        for name, raw in (("conversation.png", PNG_1X1), ("alternate.png", png_alternate)):
            path = self.ui / name
            path.write_bytes(raw)
            path.chmod(0o600)
            self.refs.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        self.value = self.entry()

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, **updates):
        value = {
            "version": 1, "conversation_id": self.cid,
            "binding": {key: self.binding[key] for key in calibration._BINDING_KEYS},
            "helper_sha256": self.template["helper_sha256"],
            "window_size": dict(self.template["window_size"]),
            "composer_rect": dict(self.template["composer_rect"]),
            "empty_composer_reference": self.refs[0],
            "empty_composer_alternates": [self.refs[1]],
        }
        value.update(updates)
        return value

    def write_calibration(self, value=None, name=calibration.CALIBRATION_NAME):
        path = self.state / name
        path.write_text(json.dumps(self.value if value is None else value), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_absent_file_returns_original_template_unchanged(self):
        original = dict(self.template)
        result = calibration.load_sender_calibration(self.state, self.template, self.cid, self.binding)
        self.assertIs(result, self.template)
        self.assertEqual(result, original)

    def test_calibration_is_per_conversation_and_repeated_prepare_ignores_shared_reference_churn(self):
        path = self.write_calibration()
        output = self.state / "conversation-sender.json"
        self.assertEqual(calibration.load_sender_calibration(
            self.state, self.template, self.cid, self.binding)["empty_composer_reference"], self.refs[0])

        other = dict(self.binding, channel_id="channel-b", session_id="session-b")
        with self.assertRaises(calibration.CalibrationError):
            calibration.apply_sender_calibration(self.template, self.value, self.cid, other)

        template_path = self.root / "shared-template.json"
        template_path.write_text(json.dumps(self.template), encoding="utf-8")
        template_path.chmod(0o600)
        executable = self.root / "sender"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        settings = {"sender_template": str(template_path), "wx_cli": "/synthetic/wx-cli",
                    "sender_executable": str(executable)}
        entry = {"state_dir": str(self.state), "conversation_id": self.cid,
                 "name": "Synthetic", "kind": "group"}
        with mock.patch.object(conv, "channel_status", return_value={"binding": self.binding}):
            conv.prepare_driver(settings, entry)
            first = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(first["empty_composer_reference"], self.refs[0])
            self.assertEqual(first["empty_composer_alternates"], [self.refs[1]])
            self.template["empty_composer_reference"] = {"path": "/shared/changed.png",
                                                          "sha256": "3" * 64}
            template_path.write_text(json.dumps(self.template), encoding="utf-8")
            conv.prepare_driver(settings, entry)
        second = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(second["empty_composer_reference"], self.refs[0])
        self.assertEqual(second["empty_composer_alternates"], [self.refs[1]])
        self.assertEqual(second["target_name"], "Synthetic")
        self.assertEqual(second["binding"], self.binding)
        self.assertTrue(path.exists())

    def test_legacy_entry_without_conversation_id_succeeds_without_override(self):
        settings = dict(sender_template=str(self.root / "template.json"), wx_cli="/synthetic/wx-cli",
                        sender_executable="/synthetic/sender")
        (self.root / "template.json").write_text(json.dumps(self.template), encoding="utf-8")
        (self.root / "template.json").chmod(0o600)
        executable = self.root / "sender"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        with mock.patch.object(conv, "channel_status", return_value={"binding": self.binding}):
            conv.prepare_driver(settings, {"state_dir": str(self.state), "name": "Legacy", "kind": "group"})
        saved = json.loads((self.state / "conversation-sender.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["empty_composer_reference"], self.template["empty_composer_reference"])

    def test_rejects_identity_geometry_hash_and_extra_field_mismatch(self):
        cases = [
            (self.entry(conversation_id="b" * 32), "conversation"),
            (self.entry(binding=dict(self.value["binding"], talker="other@chatroom")), "binding"),
            (self.entry(helper_sha256="4" * 64), "helper"),
            (self.entry(window_size={"width": 801, "height": 600}), "geometry"),
            (self.entry(window_size={"width": True, "height": 600}), "geometry"),
            (self.entry(composer_rect={"x": 10, "y": 500, "width": 2, "height": 1}), "geometry"),
            (self.entry(unexpected=True), "fields"),
            (self.entry(version=True), "version"),
        ]
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(calibration.CalibrationError, message):
                calibration.apply_sender_calibration(self.template, value, self.cid, self.binding)

    def test_rejects_bad_reference_hash_and_preserves_strict_reference_validation(self):
        bad_ref = dict(self.refs[0], sha256="0" * 64)
        with self.assertRaisesRegex(calibration.CalibrationError, "references"):
            calibration.apply_sender_calibration(self.template,
                self.entry(empty_composer_reference=bad_ref), self.cid, self.binding)
        duplicate = self.entry(empty_composer_alternates=[self.refs[0]])
        with self.assertRaisesRegex(calibration.CalibrationError, "references"):
            calibration.apply_sender_calibration(self.template, duplicate, self.cid, self.binding)

    def test_explicit_calibration_cannot_disable_the_pixel_reference(self):
        with self.assertRaisesRegex(calibration.CalibrationError, "complete image"):
            calibration.apply_sender_calibration(self.template,
                self.entry(empty_composer_reference=None, empty_composer_alternates=[]),
                self.cid, self.binding)

    def test_private_file_rejects_symlink_hardlink_mode_and_oversize(self):
        real = self.write_calibration()
        link = self.state / "conversation-sender-calibration.json"
        saved = self.state / "saved-calibration.json"
        real.rename(saved)
        link.symlink_to(saved)
        with self.assertRaises(calibration.CalibrationError):
            calibration.read_calibration(link)
        link.unlink()
        saved.rename(link)

        hard = self.state / "hardlink.json"
        os.link(link, hard)
        with self.assertRaises(calibration.CalibrationError):
            calibration.read_calibration(link)
        hard.unlink()

        link.chmod(0o640)
        with self.assertRaises(calibration.CalibrationError):
            calibration.read_calibration(link)
        link.chmod(0o600)
        link.write_bytes(b" " * (calibration.MAX_CALIBRATION_BYTES + 1))
        link.chmod(0o600)
        with self.assertRaisesRegex(calibration.CalibrationError, "64 KiB"):
            calibration.read_calibration(link)

    def test_existing_calibration_requires_canonical_absolute_path_without_symlink_parent(self):
        path = self.write_calibration()
        relative = Path(os.path.relpath(path))
        with self.assertRaisesRegex(calibration.CalibrationError, "absolute and canonical"):
            calibration.read_calibration(relative)
        with self.assertRaisesRegex(calibration.CalibrationError, "absolute and canonical"):
            calibration.read_calibration(self.state / ".." / self.state.name / path.name)

        parent_alias = self.root / "state-alias"
        parent_alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(calibration.CalibrationError, "absolute and canonical"):
            calibration.read_calibration(parent_alias / path.name)

    def test_file_mutation_during_bounded_read_is_rejected(self):
        path = self.write_calibration()
        original_fstat = os.fstat
        calls = 0

        def mutate_after_initial_fstat(fd):
            nonlocal calls
            result = original_fstat(fd)
            calls += 1
            if calls == 1:
                with path.open("ab") as stream:
                    stream.write(b" ")
                    stream.flush()
                    os.fsync(stream.fileno())
            return result

        with mock.patch.object(calibration.os, "fstat", side_effect=mutate_after_initial_fstat):
            with self.assertRaisesRegex(calibration.CalibrationError, "changed while reading"):
                calibration.read_calibration(path)
        self.assertEqual(calls, 2)

    def test_regular_file_replaced_by_fifo_before_open_is_nonblocking_and_rejected(self):
        path = self.write_calibration()
        original_open = os.open
        flags_seen = []

        def replace_with_fifo_then_open(filename, flags, *args, **kwargs):
            if Path(filename) == path:
                flags_seen.append(flags)
                path.unlink()
                os.mkfifo(path, 0o600)
            return original_open(filename, flags, *args, **kwargs)

        with mock.patch.object(calibration.os, "open", side_effect=replace_with_fifo_then_open):
            with self.assertRaises(calibration.CalibrationError):
                calibration.read_calibration(path)
        self.assertEqual(len(flags_seen), 1)
        self.assertTrue(flags_seen[0] & os.O_NONBLOCK)

    def test_rejects_duplicate_json_keys_and_invalid_json(self):
        path = self.state / calibration.CALIBRATION_NAME
        path.write_text('{"version":1,"version":1}', encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(calibration.CalibrationError, "duplicate"):
            calibration.read_calibration(path)
        path.write_text("{invalid", encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(calibration.CalibrationError, "JSON"):
            calibration.read_calibration(path)

    def test_invalid_override_fails_before_conversation_driver_config_write(self):
        self.write_calibration(self.entry(conversation_id="f" * 32))
        template_path = self.root / "template.json"
        template_path.write_text(json.dumps(self.template), encoding="utf-8")
        template_path.chmod(0o600)
        executable = self.root / "sender"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        settings = {"sender_template": str(template_path), "wx_cli": "/synthetic/wx-cli",
                    "sender_executable": str(executable)}
        entry = {"state_dir": str(self.state), "conversation_id": self.cid,
                 "name": "Synthetic", "kind": "group"}
        with mock.patch.object(conv, "channel_status", return_value={"binding": self.binding}), \
                mock.patch.object(conv.ctl, "atomic_json", wraps=conv.ctl.atomic_json) as write:
            with self.assertRaisesRegex(conv.ConversationError, "calibration conversation"):
                conv.prepare_driver(settings, entry)
        write.assert_not_called()
        self.assertFalse((self.state / "conversation-sender.json").exists())


if __name__ == "__main__":
    unittest.main()
