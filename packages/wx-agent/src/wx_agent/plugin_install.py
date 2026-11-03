"""Install the Codex WeChat hook wrapper into one explicit project only."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile


EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse")
SKILL_NAME = "wechat-inbox"
RECORD_NAME = "codex-wechat-plugin-install.json"
BUNDLE = Path(__file__).resolve().parents[4] / "plugins" / "codex-wechat"


class InstallError(ValueError):
    pass


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _canonical_project(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or path != path.resolve():
        raise InstallError("project must be an existing canonical absolute directory")
    home = Path.home().resolve()
    system_trees = (Path("/usr"), Path("/etc"), Path("/opt"), Path("/System"),
                    Path("/Library"), Path("/Applications"), Path("/bin"), Path("/sbin"))
    system_roots = (Path("/"), Path("/var"), Path("/private"), Path("/tmp"))
    global_roots = tuple((home / name).resolve() for name in (".codex", ".agents", ".claude"))
    if os.environ.get("CODEX_HOME"):
        global_roots += (Path(os.environ["CODEX_HOME"]).expanduser().resolve(),)
    if (path == home or path in home.parents or path in system_roots or
            any(path == root or root in path.parents for root in system_trees + global_roots)):
        raise InstallError("home, global agent, and system directories cannot be installation projects")
    return path


def _target(project, *parts):
    path = project
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise InstallError("refusing a symbolic link in project installation path")
    return path


def _directory(path):
    if path.exists() and not path.is_dir():
        raise InstallError("installation directory is occupied by a file")


def _json_file(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InstallError("invalid project hook or installation record JSON") from exc
    if not isinstance(value, dict):
        raise InstallError("project hook or installation record must be an object")
    return value


def _hook_groups(command):
    return {event: {"hooks": [{"type": "command", "command": command,
                              "timeout": 60, "additionalContextLimit": 1200}]} for event in EVENTS}


def _hooks(value):
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(items, list) for items in hooks.values()):
        raise InstallError("project hooks must map events to arrays")
    return hooks


def _command_present(hooks, command):
    return any(isinstance(group, dict) and isinstance(group.get("hooks"), list) and
               any(isinstance(handler, dict) and handler.get("command") == command
                   for handler in group["hooks"])
               for groups in hooks.values() for group in groups)


def _atomic(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _paths(project):
    codex = _target(project, ".codex")
    hooks = _target(project, ".codex", "hooks.json")
    agents = _target(project, ".agents")
    skills = _target(project, ".agents", "skills")
    skill_dir = _target(project, ".agents", "skills", SKILL_NAME)
    skill = _target(project, ".agents", "skills", SKILL_NAME, "SKILL.md")
    local = _target(project, ".local")
    record = _target(project, ".local", RECORD_NAME)
    for directory in (codex, agents, skills, skill_dir, local):
        _directory(directory)
    if hooks.exists() and not hooks.is_file() or skill.exists() and not skill.is_file():
        raise InstallError("installation target is not a regular file")
    return codex, hooks, agents, skills, skill_dir, skill, local, record


def _private_git_paths(project):
    try:
        inside = subprocess.run(("git", "-C", str(project), "rev-parse", "--is-inside-work-tree"),
                                capture_output=True, check=False, text=True)
    except OSError:
        return
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    for relative in (".codex/hooks.json", ".local/" + RECORD_NAME):
        ignored = subprocess.run(("git", "-C", str(project), "check-ignore", "-q", "--", relative),
                                 capture_output=True, check=False)
        if ignored.returncode != 0:
            raise InstallError(relative + " must be git-ignored before storing private paths")


def install(project, config, executable, bundle=None):
    project = _canonical_project(project)
    config, executable = Path(config), Path(executable)
    if not config.is_absolute() or not config.is_file():
        raise InstallError("private config must be an existing absolute file")
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise InstallError("plugin executable must be an existing absolute executable")
    bundle_path = BUNDLE if bundle is None else Path(bundle)
    if not bundle_path.is_absolute() or bundle_path != bundle_path.resolve():
        raise InstallError("--bundle must identify a canonical absolute plugin directory")
    source = bundle_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    if not source.is_file() or source.is_symlink():
        raise InstallError("plugin skill bundle unavailable; installed wheels require --bundle")
    skill_bytes = source.read_bytes()
    codex, hooks_path, agents, skills, skill_dir, skill_path, local, record = _paths(project)
    _private_git_paths(project)
    command = shlex.join((str(executable), "--config", str(config), "hook"))
    groups = _hook_groups(command)
    if record.exists():
        saved = _json_file(record)
        if (saved.get("project"), saved.get("config"), saved.get("executable"),
                saved.get("skill_sha256"), saved.get("groups")) != (
                str(project), str(config), str(executable), _digest(skill_bytes), groups):
            raise InstallError("existing installation has different parameters or skill")
        current = _json_file(hooks_path)
        current_hooks = _hooks(current)
        if (not skill_path.is_file() or _digest(skill_path.read_bytes()) != _digest(skill_bytes)
                or any(current_hooks.get(event, []).count(group) != 1 for event, group in groups.items())):
            raise InstallError("installed hook or skill changed; inspect before reinstalling")
        return {"status": "already_installed", "project": str(project), "hook_trust": "host_review_required"}
    if skill_path.exists() and skill_path.read_bytes() != skill_bytes:
        raise InstallError("project skill has different content")
    before = hooks_path.read_bytes() if hooks_path.exists() else None
    value = _json_file(hooks_path) if before is not None else {"hooks": {}}
    hooks = _hooks(value)
    if _command_present(hooks, command):
        raise InstallError("matching unmanaged hook already exists")
    for event, group in groups.items():
        hooks.setdefault(event, []).append(group)
    after = _json_bytes(value)
    skill_created = not skill_path.exists()
    before_mode = stat.S_IMODE(hooks_path.stat().st_mode) if before is not None else None
    saved = {"version": 1, "project": str(project), "config": str(config),
             "executable": str(executable), "groups": groups, "skill_sha256": _digest(skill_bytes),
             "skill_created": skill_created, "hooks_before": None if before is None else
             base64.b64encode(before).decode("ascii"), "hooks_before_mode": before_mode,
             "hooks_after_sha256": _digest(after)}
    for directory in (codex, agents, skills, skill_dir, local):
        directory.mkdir(mode=0o700, exist_ok=True)
    try:
        if skill_created:
            _atomic(skill_path, skill_bytes, 0o644)
        _atomic(hooks_path, after, 0o600)
        _atomic(record, _json_bytes(saved), 0o600)
    except Exception:
        if not record.exists():
            if before is None:
                hooks_path.unlink(missing_ok=True)
            else:
                _atomic(hooks_path, before, before_mode)
            if skill_created:
                skill_path.unlink(missing_ok=True)
        raise
    return {"status": "installed", "project": str(project), "hook_trust": "host_review_required"}


def uninstall(project):
    project = _canonical_project(project)
    _, hooks_path, _, _, _, skill_path, _, record = _paths(project)
    if not record.is_file():
        raise InstallError("no project installation record")
    saved = _json_file(record)
    if saved.get("version") != 1 or saved.get("project") != str(project):
        raise InstallError("installation record belongs to another project")
    if not skill_path.is_file() or _digest(skill_path.read_bytes()) != saved.get("skill_sha256"):
        raise InstallError("installed skill changed; preserve user edits")
    value = _json_file(hooks_path)
    hooks = _hooks(value)
    groups = saved.get("groups")
    if not isinstance(groups, dict) or set(groups) != set(EVENTS) or any(
            hooks.get(event, []).count(group) != 1 for event, group in groups.items()):
        raise InstallError("installed hooks changed; preserve user edits")
    current = hooks_path.read_bytes()
    if _digest(current) == saved.get("hooks_after_sha256"):
        encoded = saved.get("hooks_before")
        original = None if encoded is None else base64.b64decode(encoded, validate=True)
        if original is None:
            hooks_path.unlink()
        else:
            _atomic(hooks_path, original, saved["hooks_before_mode"])
    else:
        for event, group in groups.items():
            hooks[event].remove(group)
            if not hooks[event]:
                del hooks[event]
        _atomic(hooks_path, _json_bytes(value), stat.S_IMODE(hooks_path.stat().st_mode))
    if saved.get("skill_created"):
        skill_path.unlink()
    record.unlink()
    return {"status": "uninstalled", "project": str(project)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--config")
    parser.add_argument("--executable")
    parser.add_argument("--bundle")
    parser.add_argument("--action", choices=("install", "uninstall"), default="install")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            if not args.config or not args.executable:
                parser.error("install requires --config and --executable")
            result = install(args.project, args.config, args.executable, args.bundle)
        else:
            result = uninstall(args.project)
    except InstallError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
