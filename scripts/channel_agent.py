#!/usr/bin/env python3
"""Compatibility launcher; install packages/wx-agent for deployment."""
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parents[1] / "packages" / "wx-agent" / "src"
if _SOURCE.is_dir():
    sys.path.insert(0, str(_SOURCE))

from wx_agent.channel_agent import main

if __name__ == "__main__":
    raise SystemExit(main())
