"""Bounded one-shot JSON RPC client using local argv or an existing SSH alias."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys

from .gateway import MAX_REQUEST, MAX_RESPONSE, CORRELATION, GatewayError, _private_json


SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _private_ssh_config(value: str) -> str:
    if not isinstance(value, str):
        raise GatewayError("invalid_config")
    path = Path(value)
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise GatewayError("invalid_config")
        info = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise GatewayError("invalid_config") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise GatewayError("invalid_config")
    return value


class GatewayClientError(GatewayError):
    def __init__(self, code: str, request_id: str):
        super().__init__(code)
        self.code = code
        self.request_id = request_id


class GatewayUnknown(GatewayClientError):
    """The request may have reached the gateway; inspect its original IDs."""


class GatewayRejected(GatewayClientError):
    """A definite gateway refusal with a bounded public error code."""


class GatewayClient:
    def __init__(self, config_path: Path, timeout: float = 35):
        value = _private_json(Path(config_path))
        keys = {"version", "application_id", "token", "command"}
        optional = {"ssh_target", "ssh_config"}
        if (not keys <= set(value) or set(value) - keys - optional
                or value["version"] != 1):
            raise GatewayError("invalid_config")
        command = value["command"]
        if (not isinstance(command, list) or len(command) != 4
                or not all(isinstance(item, str) for item in command)
                or not Path(command[0]).is_absolute() or Path(command[0]).resolve() != Path(command[0])
                or command[1] != "--config" or not Path(command[2]).is_absolute()
                or Path(command[2]).resolve() != Path(command[2]) or command[3] != "request"
                or not isinstance(value["application_id"], str)
                or not isinstance(value["token"], str) or not value["token"] or len(value["token"]) > 512
                or type(timeout) not in (int, float) or timeout <= 0):
            raise GatewayError("invalid_config")
        target = value.get("ssh_target")
        if target is not None and (not isinstance(target, str) or not SSH_ALIAS.fullmatch(target)):
            raise GatewayError("invalid_config")
        if "ssh_config" in value and target is None:
            raise GatewayError("invalid_config")
        self.application_id = value["application_id"]
        self.token = value["token"]
        self.command = command
        self.ssh_target = target
        self.ssh_config = _private_ssh_config(value["ssh_config"]) if "ssh_config" in value else None
        self.timeout = timeout
        self.last_id = None

    def request(self, method: str, params: dict, request_id: str = None) -> dict:
        correlation = request_id if request_id is not None else secrets.token_hex(16)
        if not isinstance(correlation, str) or not CORRELATION.fullmatch(correlation):
            raise GatewayError("invalid_request")
        self.last_id = correlation
        payload = json.dumps({"protocol_version": 1, "id": correlation,
                              "application_id": self.application_id, "token": self.token,
                              "method": method, "params": params}, ensure_ascii=False).encode()
        if len(payload) > MAX_REQUEST:
            raise GatewayRejected("request_too_large", correlation)
        if self.ssh_target is None:
            command = self.command
        else:
            command = ["ssh"]
            if self.ssh_config is not None:
                command.extend(["-F", self.ssh_config])
            command.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-T",
                            self.ssh_target, shlex.join(self.command)])
        try:
            done = subprocess.run(command, input=payload, capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GatewayUnknown("transport_unknown", correlation) from exc
        if len(done.stdout) > MAX_RESPONSE:
            raise GatewayUnknown("response_unknown", correlation)
        try:
            response = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise GatewayUnknown("response_unknown", correlation) from exc
        if (not isinstance(response, dict) or set(response) != {"protocol_version", "id", "ok"} | (
                {"result"} if response.get("ok") is True else {"error"})
                or response.get("protocol_version") != 1 or response.get("id") != correlation):
            raise GatewayUnknown("response_unknown", correlation)
        if response["ok"] is True and done.returncode == 0 and isinstance(response["result"], dict):
            return response["result"]
        if response["ok"] is False and isinstance(response["error"], dict):
            code = response["error"].get("code")
            if code == "result_unknown":
                raise GatewayUnknown(code, correlation)
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code):
                raise GatewayRejected(code, correlation)
        raise GatewayUnknown("response_unknown", correlation)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("method")
    parser.add_argument("--params-stdin", action="store_true", required=True)
    args = parser.parse_args(argv)
    correlation = None
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise GatewayError("request_too_large")
        params = json.loads(raw.decode("utf-8"))
        client = GatewayClient(args.config)
        result = client.request(args.method, params)
        correlation = client.last_id
        value = {"protocol_version": 1, "id": correlation, "ok": True, "result": result}
    except GatewayClientError as exc:
        value = {"protocol_version": 1, "id": exc.request_id, "ok": False, "error": {"code": exc.code}}
    except (GatewayError, UnicodeError, ValueError, OSError):
        value = {"protocol_version": 1, "id": correlation, "ok": False, "error": {"code": "invalid_request"}}
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0 if value["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
