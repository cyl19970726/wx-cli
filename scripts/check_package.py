#!/usr/bin/env python3
"""Install a wheel into a fresh environment and smoke-test it off checkout."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile


def check_wheel_members(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    if not any(name.startswith("wx_agent/") and name.endswith(".py") for name in names):
        raise ValueError("wheel has no wx_agent Python modules")
    forbidden = (".local/", "scripts/", "tests/", "__pycache__/", ".db", ".sqlite3",
                 ".pem", ".key", ".env")
    bad = [name for name in names if any(piece in name for piece in forbidden)]
    if bad:
        raise ValueError("wheel contains unexpected private/test material: " + ", ".join(bad))


def check_install(wheel, tests=None):
    wheel = wheel.resolve(strict=True)
    if tests is not None:
        tests = tests.resolve(strict=True)
    check_wheel_members(wheel)
    with tempfile.TemporaryDirectory(prefix="wx-agent-wheel-") as tmp:
        root = Path(tmp)
        env_dir = root / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        command = bin_dir / ("wx-agent.exe" if os.name == "nt" else "wx-agent")
        clean_env = os.environ.copy()
        clean_env.pop("PYTHONPATH", None)
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                       cwd=root, env=clean_env, check=True)
        subprocess.run([str(command), "--help"], cwd=root, env=clean_env, check=True,
                       stdout=subprocess.PIPE, text=True)
        result = subprocess.run(
            [str(python), "-c", "import wx_agent; print(wx_agent.__file__)"],
            cwd=root, env=clean_env, check=True, capture_output=True, text=True,
        )
        installed = Path(result.stdout.strip()).resolve()
        if env_dir.resolve() not in installed.parents:
            raise ValueError("wx_agent imported outside isolated environment: " + str(installed))
        if tests is not None:
            subprocess.run(
                [str(python), "-m", "unittest", "discover", "-s", str(tests), "-p", "test_*.py"],
                cwd=root, env=clean_env, check=True,
            )
    print("Wheel install, wx-agent entrypoint, and installed tests passed: " + wheel.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--tests", type=Path, help="run package tests against the installed wheel")
    args = parser.parse_args()
    try:
        check_install(args.wheel, args.tests)
    except (OSError, subprocess.CalledProcessError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
