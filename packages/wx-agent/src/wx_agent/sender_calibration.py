"""Bounded, private per-conversation empty-composer reference overrides."""
import json
import os
from pathlib import Path
import stat

from . import macos_sender_driver as sender


CALIBRATION_NAME = "conversation-sender-calibration.json"
MAX_CALIBRATION_BYTES = 64 * 1024
_BINDING_KEYS = ("channel_id", "session_id", "account", "talker")
_TOP_LEVEL_KEYS = {
    "version", "conversation_id", "binding", "helper_sha256", "window_size",
    "composer_rect", "empty_composer_reference", "empty_composer_alternates",
}


class CalibrationError(ValueError):
    """A calibration file is unsafe, malformed, or bound to other state."""


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CalibrationError("duplicate calibration JSON key")
        value[key] = item
    return value


def _private_info(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def _strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (left.keys() == right.keys() and
                all(_strict_equal(left[key], right[key]) for key in left))
    if isinstance(left, list):
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def read_calibration(path):
    """Read one bounded private calibration file, or return None if absent."""
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CalibrationError("cannot inspect sender calibration file") from exc
    try:
        canonical = path.resolve(strict=True) if path.is_absolute() and ".." not in path.parts else None
    except (OSError, RuntimeError) as exc:
        raise CalibrationError("sender calibration path must be absolute and canonical") from exc
    if canonical != path:
        raise CalibrationError("sender calibration path must be absolute and canonical")
    if not _private_info(before):
        raise CalibrationError("unsafe sender calibration file")
    if before.st_size > MAX_CALIBRATION_BYTES:
        raise CalibrationError("sender calibration file exceeds 64 KiB")
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise CalibrationError("cannot safely open sender calibration file") from exc
    try:
        opened = os.fstat(fd)
        if (not _private_info(opened) or (before.st_dev, before.st_ino) !=
                (opened.st_dev, opened.st_ino)):
            raise CalibrationError("sender calibration file changed while opening")
        if opened.st_size > MAX_CALIBRATION_BYTES:
            raise CalibrationError("sender calibration file exceeds 64 KiB")
        opened_metadata = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
                           opened.st_dev, opened.st_ino)
        try:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_CALIBRATION_BYTES + 1)
        except OSError as exc:
            raise CalibrationError("cannot read sender calibration file") from exc
        after = os.fstat(fd)
        after_metadata = (after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                          after.st_dev, after.st_ino)
        if after_metadata != opened_metadata:
            raise CalibrationError("sender calibration file changed while reading")
        if len(raw) > MAX_CALIBRATION_BYTES:
            raise CalibrationError("sender calibration file exceeds 64 KiB")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except CalibrationError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise CalibrationError("invalid sender calibration JSON") from exc
    if not isinstance(value, dict):
        raise CalibrationError("sender calibration must be a JSON object")
    return value


def apply_sender_calibration(template, calibration, conversation_id, binding):
    """Return a template copy with only this conversation's references applied."""
    if not isinstance(calibration, dict) or set(calibration) != _TOP_LEVEL_KEYS:
        raise CalibrationError("invalid sender calibration fields")
    if type(calibration.get("version")) is not int or calibration["version"] != 1:
        raise CalibrationError("unsupported sender calibration version")
    if (not isinstance(conversation_id, str) or
            calibration["conversation_id"] != conversation_id):
        raise CalibrationError("sender calibration conversation differs")
    saved_binding = calibration.get("binding")
    if (not isinstance(saved_binding, dict) or set(saved_binding) != set(_BINDING_KEYS)
            or not isinstance(binding, dict)
            or any(saved_binding[key] != binding.get(key) for key in _BINDING_KEYS)):
        raise CalibrationError("sender calibration binding differs")
    if calibration["helper_sha256"] != template.get("helper_sha256"):
        raise CalibrationError("sender calibration helper hash differs")
    if (not _strict_equal(calibration["window_size"], template.get("window_size")) or
            not _strict_equal(calibration["composer_rect"], template.get("composer_rect"))):
        raise CalibrationError("sender calibration geometry differs")
    if not isinstance(calibration["empty_composer_reference"], dict):
        raise CalibrationError("sender calibration requires a complete image reference")

    result = dict(template)
    result["empty_composer_reference"] = calibration["empty_composer_reference"]
    result["empty_composer_alternates"] = calibration["empty_composer_alternates"]
    ui_dir = result.get("ui_dir")
    if not isinstance(ui_dir, str) or not Path(ui_dir).is_absolute() or ".." in Path(ui_dir).parts:
        raise CalibrationError("sender calibration UI directory is invalid")
    try:
        validation = dict(result, ui_dir=Path(ui_dir))
        sender.validate_empty_composer_reference(validation)
    except (sender.Refused, KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise CalibrationError("invalid sender calibration references") from exc
    return result


def load_sender_calibration(state_dir, template, conversation_id, binding):
    """Load and apply the optional state-local calibration to a template."""
    value = read_calibration(Path(state_dir) / CALIBRATION_NAME)
    if value is None:
        return template
    return apply_sender_calibration(template, value, conversation_id, binding)
