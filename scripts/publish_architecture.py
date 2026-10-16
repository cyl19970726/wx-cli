#!/usr/bin/env python3
"""Publish a blank-line-normalized copy without changing Archify provenance."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / ".archify/architecture-agent-channel-20261016-123000"
    receipt = json.loads((folder / "agent-channel.finalize-summary.json").read_text())
    if receipt.get("status") != "pass":
        parser.error("Archify finalize must pass before publication")
    for name, key in [("candidate.json", "specification"), ("agent-channel.html", "artifact")]:
        content = (folder / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != receipt[key]["sha256"]:
            parser.error("Archify source/artifact differs from its passing receipt")
    normalized = b"\n".join(b"" if not line.strip() else line for line in content.split(b"\n"))
    public = root / "docs/diagrams/agent-channel.html"
    if args.check:
        if not public.exists() or public.read_bytes() != normalized:
            parser.error("Public diagram differs from the normalized Archify artifact")
    else:
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_bytes(normalized)
    print("Public architecture diagram matches the validated source artifact.")


if __name__ == "__main__":
    main()
