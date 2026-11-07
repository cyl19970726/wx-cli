"""Install a queue-only WeChat Skill in one explicit project.

Legacy Hook removal is a separate, recorded action. No install path registers a
Hook, changes host trust, or touches a business Session.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile


EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse")  # Legacy v1 only.
SKILL_NAME = "wechat-inbox"
RECORD_NAME = "codex-wechat-plugin-install.json"
ARCHIVE_NAME = "codex-wechat-plugin-install.v1-retired.json"
BUNDLE = Path(__file__).resolve().parents[4] / "plugins" / "codex-wechat"


class InstallError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise InstallError(message)


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


def _paths(project):
    agents = _target(project, ".agents")
    skills = _target(project, ".agents", "skills")
    skill_dir = _target(project, ".agents", "skills", SKILL_NAME)
    skill = _target(project, ".agents", "skills", SKILL_NAME, "SKILL.md")
    local = _target(project, ".local")
    record = _target(project, ".local", RECORD_NAME)
    archive = _target(project, ".local", ARCHIVE_NAME)
    for path in (agents, skills, skill_dir, local):
        _directory(path)
    return (agents, skills, skill_dir, skill, local, record, archive)


def _read_file(path, *, private=False):
    """Read a single owned regular inode after checking links and mode."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError("cannot inspect installation file") from exc
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1,
             "installation file must be an owned regular single-link file")
    if private:
        _require(not (info.st_mode & 0o077), "private installation file has broad permissions")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            _require(stat.S_ISREG(opened.st_mode) and opened.st_uid == info.st_uid and
                     opened.st_nlink == 1 and opened.st_dev == info.st_dev and
                     opened.st_ino == info.st_ino, "installation file changed during read")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)
    except OSError as exc:
        raise InstallError("cannot read installation file") from exc


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _json_object(raw, label):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise InstallError("invalid " + label + " JSON") from exc
    _require(isinstance(value, dict), label + " must be a JSON object")
    return value


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


def _git(project, *args):
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    try:
        return subprocess.run(("git", "-C", str(project), *args), capture_output=True,
                              check=False, text=True, env=env)
    except OSError as exc:
        raise InstallError("git metadata is unavailable") from exc


def _git_metadata(project):
    has_git_marker = any((parent / ".git").exists() for parent in (project, *project.parents))
    try:
        inside = _git(project, "rev-parse", "--is-inside-work-tree")
    except InstallError:
        if has_git_marker:
            raise
        return None
    if inside.returncode != 0:
        if has_git_marker:
            raise InstallError("cannot read project Git metadata")
        return None
    _require(inside.stdout.strip() == "true", "installation project must not be bare Git")
    result = _git(project, "rev-parse", "--path-format=absolute", "--show-toplevel",
                  "--git-common-dir", "--absolute-git-dir")
    _require(result.returncode == 0 and len(result.stdout.splitlines()) == 3,
             "cannot resolve project Git roots")
    return tuple(Path(line).resolve() for line in result.stdout.splitlines())


def _private_record_ignored(project, include_archive=False):
    if _git_metadata(project) is not None:
        names = (RECORD_NAME, ARCHIVE_NAME) if include_archive else (RECORD_NAME,)
        for name in names:
            ignored = _git(project, "check-ignore", "-q", "--", ".local/" + name)
            _require(ignored.returncode == 0,
                     "private .local installation file must be git-ignored: " + name)


def _legacy_source(project, saved, supplied=None):
    """Validate a recorded v1 Hook source, including a linked worktree main checkout."""
    recorded = saved.get("hooks_project", str(project))
    source = _canonical_project(recorded)
    if supplied is not None:
        _require(_canonical_project(supplied) == source,
                 "--hooks-project does not match the legacy installation record")
    if "hooks_project" not in saved:
        _require(source == project, "legacy local Hook source changed")
        return source
    metadata = _git_metadata(project)
    if metadata is None:
        _require(source == project, "legacy Hook source lacks matching Git metadata")
        return source
    checkout, common, git_dir = metadata
    if common == git_dir:
        _require(source == project, "legacy Hook source must match this checkout")
        return source
    worktrees = _git(project, "worktree", "list", "--porcelain", "-z")
    first = worktrees.stdout.split("\0", 1)[0]
    _require(worktrees.returncode == 0 and first.startswith("worktree "),
             "cannot resolve main Git checkout for legacy Hooks")
    main = _canonical_project(first[len("worktree "):])
    expected = main / project.relative_to(checkout)
    _require(source == expected and _git_metadata(main) == (main, common, common) and
             _git_metadata(source) == (main, common, common),
             "legacy Hook source is not the corresponding main checkout")
    return source


def _legacy_record(project, raw):
    saved = _json_object(raw, "legacy installation record")
    _require(saved.get("version") == 1 and saved.get("project") == str(project),
             "legacy installation record belongs to another project")
    groups = saved.get("groups")
    _require(isinstance(groups, dict) and set(groups) == set(EVENTS),
             "legacy Hook group record is malformed")
    commands = []
    for event in EVENTS:
        group = groups[event]
        _require(isinstance(group, dict) and set(group) == {"hooks"} and
                 isinstance(group["hooks"], list) and len(group["hooks"]) == 1,
                 "legacy Hook group record is malformed")
        handler = group["hooks"][0]
        _require(isinstance(handler, dict) and set(handler) ==
                 {"type", "command", "timeout", "additionalContextLimit"} and
                 handler.get("type") == "command" and
                 handler.get("timeout") == 60 and handler.get("additionalContextLimit") == 1200 and
                 isinstance(handler.get("command"), str), "legacy Hook handler record is malformed")
        commands.append(handler["command"])
    _require(len(set(commands)) == 1, "legacy Hook commands disagree")
    try:
        parts = shlex.split(commands[0])
    except ValueError as exc:
        raise InstallError("legacy Hook command is malformed") from exc
    _require(len(parts) == 4 and Path(parts[0]).is_absolute() and parts[1] == "--config" and
             Path(parts[2]).is_absolute() and parts[3] == "hook", "legacy Hook command is not owned")
    _require(saved.get("config", parts[2]) == parts[2] and
             saved.get("executable", parts[0]) == parts[0], "legacy Hook command differs from record")
    _require(isinstance(saved.get("skill_created"), bool) and
             isinstance(saved.get("skill_sha256"), str) and
             re.fullmatch("[0-9a-f]{64}", saved["skill_sha256"]) is not None,
             "legacy Skill ownership record is malformed")
    return saved


def _legacy_hooks(project, saved, supplied=None):
    source = _legacy_source(project, saved, supplied)
    path = _target(source, ".codex", "hooks.json")
    raw = _read_file(path)
    if raw is None:
        return source, path, None, None
    value = _json_object(raw, "legacy project Hooks")
    hooks = value.get("hooks")
    _require(isinstance(hooks, dict) and all(isinstance(x, list) for x in hooks.values()),
             "legacy project Hooks must map events to arrays")
    counts = [hooks.get(event, []).count(group) for event, group in saved["groups"].items()]
    _require(all(count == 1 for count in counts) or all(count == 0 for count in counts),
             "recorded Hook groups changed; preserve user edits")
    return source, path, raw, value


def _original_hooks(saved, current):
    """Restore exact old bytes only when the v1 after-image is proven."""
    if _digest(current) != saved.get("hooks_after_sha256"):
        return False, None
    encoded = saved.get("hooks_before")
    try:
        before = None if encoded is None else base64.b64decode(encoded, validate=True)
        value = {"hooks": {}} if before is None else _json_object(before, "original Hooks")
    except (ValueError, TypeError) as exc:
        raise InstallError("legacy Hook before-image is malformed") from exc
    hooks = value.setdefault("hooks", {})
    _require(isinstance(hooks, dict) and all(isinstance(x, list) for x in hooks.values()),
             "legacy Hook before-image is malformed")
    for event, group in saved["groups"].items():
        hooks.setdefault(event, []).append(group)
    _require(_digest(_json_bytes(value)) == _digest(current),
             "legacy Hook before-image does not prove current file")
    return True, before


def _retire_hooks(project, supplied=None):
    _, _, _, _, local, record, archive = _paths(project)
    raw_record = _read_file(record, private=True)
    if raw_record is None:
        archived = _read_file(archive, private=True)
        if archived is not None:
            saved = _legacy_record(project, archived)
            source, _, _, value = _legacy_hooks(project, saved, supplied)
            _require(value is None or all(value["hooks"].get(event, []).count(group) == 0
                                         for event, group in saved["groups"].items()),
                     "retired Hooks reappeared; inspect before continuing")
            return {"status": "already_retired", "project": str(project),
                    "hooks_project": str(source), "archive_path": str(archive)}
        raise InstallError("no legacy installation record")
    saved = _legacy_record(project, raw_record)
    _private_record_ignored(project, include_archive=True)
    archived = _read_file(archive, private=True)
    _require(archived is None or archived == raw_record,
             "legacy retirement archive differs from active record; inspect prior progress")
    source, hooks_path, current, value = _legacy_hooks(project, saved, supplied)
    exact, before = (False, None) if current is None else _original_hooks(saved, current)
    if exact and before is not None:
        mode = saved.get("hooks_before_mode")
        _require(isinstance(mode, int) and 0 <= mode <= 0o777,
                 "legacy Hook original mode is malformed")
    _directory(local)
    if archived is None:
        _atomic(archive, raw_record, 0o600)
    if current is not None:
        hooks = value["hooks"]
        if all(hooks.get(event, []).count(group) == 1 for event, group in saved["groups"].items()):
            if exact:
                if before is None:
                    hooks_path.unlink()
                else:
                    _atomic(hooks_path, before, mode)
            else:
                for event, group in saved["groups"].items():
                    hooks[event].remove(group)
                    if not hooks[event]:
                        del hooks[event]
                mode = stat.S_IMODE(hooks_path.stat().st_mode)
                _atomic(hooks_path, _json_bytes(value), mode)
    record.unlink()
    return {"status": "hooks_retired", "project": str(project),
            "hooks_project": str(source), "archive_path": str(archive),
            "skill_path": str(project / ".agents" / "skills" / SKILL_NAME / "SKILL.md")}


def retire_hooks(project, hooks_project=None):
    return _retire_hooks(_canonical_project(project), hooks_project)


def install(project, config, executable, bundle=None, hooks_project=None):
    project = _canonical_project(project)
    _require(hooks_project is None, "new queue installs do not accept --hooks-project")
    config, executable = Path(config), Path(executable)
    _require(config.is_absolute() and _read_file(config, private=True) is not None,
             "private queue config must be an existing owned file")
    _require(executable.is_absolute() and executable.is_file() and os.access(executable, os.X_OK),
             "queue session CLI must be an existing absolute executable")
    bundle_path = BUNDLE if bundle is None else Path(bundle)
    _require(bundle_path.is_absolute() and bundle_path == bundle_path.resolve(),
             "--bundle must identify a canonical absolute plugin directory")
    source = bundle_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    _require(source.is_file() and not source.is_symlink(),
             "plugin Skill bundle unavailable; installed wheels require --bundle")
    skill_bytes = source.read_bytes()
    agents, skills, skill_dir, skill_path, local, record, archive = _paths(project)
    _private_record_ignored(project)
    raw_record = _read_file(record, private=True)
    if raw_record is not None:
        saved = _json_object(raw_record, "installation record")
        if saved.get("version") == 1:
            raise InstallError("legacy Hook record present; run --action retire-hooks explicitly")
        _require(saved.get("version") == 2 and saved.get("project") == str(project),
                 "installation record belongs to another project")
        _require(isinstance(saved.get("skill_created"), bool),
                 "installation Skill ownership record is malformed")
        _require((saved.get("config"), saved.get("executable"), saved.get("skill_sha256")) ==
                 (str(config), str(executable), _digest(skill_bytes)),
                 "existing installation has different parameters or Skill")
        _require(_read_file(skill_path) == skill_bytes, "installed Skill changed; preserve user edits")
        return {"status": "already_installed", "project": str(project),
                "skill_path": str(skill_path), "record_path": str(record)}
    old_skill = _read_file(skill_path)
    old_skill_mode = stat.S_IMODE(skill_path.stat().st_mode) if old_skill is not None else None
    archived_raw = _read_file(archive, private=True)
    prior_created = False
    if archived_raw is not None:
        legacy = _legacy_record(project, archived_raw)
        # Retirement is the explicit migration boundary. An old owned Skill
        # can be replaced only when its exact recorded bytes remain installed.
        if old_skill is not None and old_skill != skill_bytes:
            _require(legacy["skill_created"] and _digest(old_skill) == legacy["skill_sha256"],
                     "retired Skill changed or preexisted; preserve user edits")
            prior_created = True
        elif old_skill is not None and legacy["skill_created"]:
            prior_created = True
    elif old_skill is not None:
        _require(old_skill == skill_bytes, "project Skill has different content")
    skill_created = old_skill is None or prior_created
    saved = {"version": 2, "project": str(project), "config": str(config),
             "executable": str(executable), "skill_sha256": _digest(skill_bytes),
             "skill_created": skill_created}
    for directory in (agents, skills, skill_dir, local):
        directory.mkdir(mode=0o700, exist_ok=True)
    try:
        if old_skill != skill_bytes:
            _atomic(skill_path, skill_bytes, 0o644)
        _atomic(record, _json_bytes(saved), 0o600)
    except Exception:
        if _read_file(record, private=True) is None:
            if old_skill is None:
                skill_path.unlink(missing_ok=True)
            elif old_skill != skill_bytes:
                _atomic(skill_path, old_skill, old_skill_mode)
        raise
    return {"status": "installed", "project": str(project),
            "skill_path": str(skill_path), "record_path": str(record)}


def uninstall(project, hooks_project=None):
    project = _canonical_project(project)
    _, _, _, skill_path, _, record, archive = _paths(project)
    raw = _read_file(record, private=True)
    if raw is not None:
        saved = _json_object(raw, "installation record")
        if saved.get("version") == 1:
            _require(_read_file(skill_path) is not None and
                     _digest(_read_file(skill_path)) == saved.get("skill_sha256"),
                     "installed Skill changed; use retire-hooks to preserve it")
            _retire_hooks(project, hooks_project)
        else:
            _require(saved.get("version") == 2 and saved.get("project") == str(project),
                     "installation record belongs to another project")
            _require(isinstance(saved.get("skill_created"), bool),
                     "installation Skill ownership record is malformed")
            _require(hooks_project is None, "queue installation has no Hook source")
            _require(_digest(_read_file(skill_path) or b"") == saved.get("skill_sha256"),
                     "installed Skill changed; preserve user edits")
            if saved.get("skill_created"):
                skill_path.unlink()
            record.unlink()
            return {"status": "uninstalled", "project": str(project)}
    else:
        _require(hooks_project is None, "retired installation has no active Hook source")
    archived = _read_file(archive, private=True)
    _require(archived is not None, "no project installation record")
    legacy = _legacy_record(project, archived)
    if legacy["skill_created"]:
        skill = _read_file(skill_path)
        _require(skill is not None and _digest(skill) == legacy["skill_sha256"],
                 "retired Skill changed; preserve user edits")
        skill_path.unlink()
    return {"status": "uninstalled", "project": str(project), "archive_path": str(archive)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--hooks-project", help="legacy retirement only: recorded main checkout Hook source")
    parser.add_argument("--config", help="private queue wrapper configuration")
    parser.add_argument("--executable", help="queue session CLI")
    parser.add_argument("--bundle")
    parser.add_argument("--action", choices=("install", "uninstall", "retire-hooks"), default="install")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            if not args.config or not args.executable:
                parser.error("install requires --config and --executable")
            result = install(args.project, args.config, args.executable, args.bundle, args.hooks_project)
        elif args.action == "retire-hooks":
            result = retire_hooks(args.project, args.hooks_project)
        else:
            result = uninstall(args.project, args.hooks_project)
    except InstallError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
