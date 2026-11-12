"""Local application client for pre-authorized managed conversation intents.

This is trusted local configuration, not an application credential boundary.
The outbox and coordinator remain the only intent and attempt journals.
Both public methods take the original local business request key (for example,
``notice-001``). Their result returns the full ``app_a.notice-001`` ID; pass
the original key to ``status``, never the returned full ID.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable, Optional

from .macos_sender_driver import Refused, validate_text
from .dispatch import DispatchError, validate_show_response


APP_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
LOCAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
CONVERSATION_ID = re.compile(r"[0-9a-f]{32}\Z")


class ApplicationError(RuntimeError):
    def __init__(self, code: str, request_id: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.request_id = request_id


class ResultUnknown(ApplicationError):
    """The command may have committed; inspect this exact request ID."""


def _canonical(value: str) -> Path:
    if not isinstance(value, str):
        raise ApplicationError("invalid_path")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ApplicationError("invalid_path")
    return path


def _private_config(path: Path) -> dict:
    path = _canonical(str(path))
    try:
        info = path.lstat()
    except OSError as exc:
        raise ApplicationError("config_unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ApplicationError("config_not_private")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ApplicationError("invalid_config") from exc
    if not isinstance(value, dict):
        raise ApplicationError("invalid_config")
    return value


class ApplicationClient:
    """A narrow local transport; callers may substitute only the process runner."""

    def __init__(self, config_path: Path, transport: Optional[Callable] = None, timeout: float = 30):
        value = _private_config(Path(config_path))
        if set(value) != {"version", "application_id", "wx_agent", "wx_cli",
                          "conversation_config", "coordinator_root", "conversations"} or value["version"] != 1:
            raise ApplicationError("invalid_config")
        application_id = value["application_id"]
        if not isinstance(application_id, str) or not APP_ID.fullmatch(application_id):
            raise ApplicationError("invalid_application_id")
        self.application_id = application_id
        self.wx_agent = _canonical(value["wx_agent"])
        self.wx_cli = _canonical(value["wx_cli"])
        self.conversation_config = _canonical(value["conversation_config"])
        self.coordinator_root = _canonical(value["coordinator_root"])
        entries = value["conversations"]
        if not isinstance(entries, dict) or not entries:
            raise ApplicationError("invalid_scope")
        self.conversations = {}
        for cid, entry in entries.items():
            if not isinstance(cid, str) or not CONVERSATION_ID.fullmatch(cid) or not isinstance(entry, dict) or set(entry) != {"state_dir", "owner", "expected_epoch"}:
                raise ApplicationError("invalid_scope")
            owner, epoch = entry["owner"], entry["expected_epoch"]
            if not isinstance(owner, str) or not owner or type(epoch) is not int or epoch < 1:
                raise ApplicationError("invalid_scope")
            self.conversations[cid] = {"state_dir": _canonical(entry["state_dir"]),
                                       "owner": owner, "expected_epoch": epoch}
        self._transport = transport or self._run
        if timeout <= 0:
            raise ApplicationError("invalid_timeout")
        self.timeout = timeout

    @staticmethod
    def _run(argv, data, timeout):
        return subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)

    def full_request_id(self, request_id: str) -> str:
        """Map one business key to its stable outbox ID exactly once."""
        if not isinstance(request_id, str) or not LOCAL_ID.fullmatch(request_id):
            raise ApplicationError("invalid_request_id")
        if request_id.startswith(self.application_id + "."):
            raise ApplicationError("already_namespaced")
        full = self.application_id + "." + request_id
        if len(full) > 128:
            raise ApplicationError("invalid_request_id")
        return full

    def _entry(self, cid: str) -> dict:
        if not isinstance(cid, str) or cid not in self.conversations:
            raise ApplicationError("conversation_out_of_scope")
        return self.conversations[cid]

    def _json(self, argv: list[str], request_id: str, data: Optional[str] = None) -> dict:
        try:
            result = self._transport(argv, data, self.timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ResultUnknown("transport_unknown", request_id) from exc
        if result.returncode != 0:
            try:
                error = json.loads(result.stderr)["error"]["code"]
            except (ValueError, KeyError, TypeError):
                error = "command_failed"
            if error in ("command_failed", "local_state_error", "invalid_output"):
                raise ResultUnknown("command_result_unknown", request_id)
            raise ApplicationError(error, request_id)
        try:
            if len(result.stdout) > 65536:
                raise ValueError("oversized response")
            value = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ResultUnknown("response_unknown", request_id) from exc
        if not isinstance(value, dict):
            raise ResultUnknown("response_unknown", request_id)
        return value

    def _mapped_entry(self, cid: str, full_id: str) -> tuple[dict, dict]:
        """Verify the immutable conversation-to-journal routing for reads and writes."""
        entry = self._entry(cid)
        marker = _private_config(entry["state_dir"] / "managed-coordinator.json")
        if marker.get("version") != 1 or marker.get("root") != str(self.coordinator_root):
            raise ApplicationError("coordinator_drift", full_id)
        binding = self._json([str(self.wx_cli), "coordinator", "--state-dir", str(self.coordinator_root),
                              "status", "--conversation-id", cid], full_id)
        if (binding.get("conversation_id") != cid
                or binding.get("channel_state_dir") != str(entry["state_dir"])):
            raise ApplicationError("coordinator_drift", full_id)
        return entry, binding

    def _fence(self, cid: str, full_id: str) -> dict:
        """Require the original grant to remain current before any new send."""
        entry, binding = self._mapped_entry(cid, full_id)
        if (binding.get("owner") != entry["owner"]
                or binding.get("epoch") != entry["expected_epoch"]
                or binding.get("control") != "active"
                or binding.get("reply_policy") != "authorized"):
            raise ApplicationError("owner_epoch_drift", full_id)
        return entry

    def send_text(self, conversation_id: str, request_id: str, text: str) -> dict:
        full_id = self.full_request_id(request_id)
        self._entry(conversation_id)
        try:
            validate_text(text)
        except (Refused, UnicodeError) as exc:
            # This is a definite input refusal before a subprocess or intent.
            # Runtime failures after submission retain their unknown semantics.
            raise ApplicationError("invalid_text", full_id) from exc
        entry = self._fence(conversation_id, full_id)
        value = self._json([str(self.wx_agent), "conversations", "--config", str(self.conversation_config),
                            "send", "--id", conversation_id, "--request-id", full_id,
                            "--text-stdin", "--owner", entry["owner"],
                            "--expected-epoch", str(entry["expected_epoch"]),
                            "--publisher", self.application_id, "--no-wait"],
                           full_id, text)
        if value.get("request_id") != full_id or value.get("status") not in (
                "pending", "ui_observed", "recipient_confirmed"):
            raise ResultUnknown("response_identity_unknown", full_id)
        return {"application_id": self.application_id, "conversation_id": conversation_id,
                "request_id": full_id, "status": value["status"]}

    def status(self, conversation_id: str, request_id: str) -> dict:
        """Read an original business key after handback without granting a send."""
        full_id = self.full_request_id(request_id)
        entry, _ = self._mapped_entry(conversation_id, full_id)
        value = self._json([str(self.wx_cli), "outbox", "--state-dir", str(entry["state_dir"]),
                            "show", "--request-id", full_id], full_id)
        if (value.get("request_id") != full_id or value.get("publisher_id") != self.application_id
                or value.get("media") is not None):
            raise ApplicationError("request_scope_drift", full_id)
        state = value.get("status")
        attempt = value.get("attempt_number")
        if not isinstance(state, str) or type(attempt) is not int or attempt < 0 or not isinstance(value.get("text"), str):
            raise ResultUnknown("response_unknown", full_id)
        result = {"application_id": self.application_id, "conversation_id": conversation_id,
                  "request_id": full_id, "status": state, "attempt_number": attempt,
                  "text_sha256": hashlib.sha256(value["text"].encode("utf-8")).hexdigest()}
        shown = self._json([str(self.wx_cli), "coordinator", "--state-dir", str(self.coordinator_root),
                            "dispatch", "show", "--conversation-id", conversation_id,
                            "--request-id", full_id], full_id)
        try:
            record = validate_show_response(shown, conversation_id, full_id)["dispatch"]
        except DispatchError as exc:
            raise ResultUnknown("response_unknown", full_id) from exc
        if record is not None and record["result"] == "cancelled_by_user":
            result.update(cancelled=True, dispatch_id=record["dispatch_id"],
                          dispatch_status="done", dispatch_result="cancelled_by_user")
        return result
