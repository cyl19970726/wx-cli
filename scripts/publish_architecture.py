#!/usr/bin/env python3
"""Publish a blank-line-normalized copy without changing Archify provenance."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diagram", choices=("architecture", "capabilities"), default="architecture")
    parser.add_argument("--receipt", type=Path, help="passing finalize summary from a named review directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.diagram == "architecture":
        folder = root / ".archify/architecture-agent-channel-20261016-123000"
        source = folder / "candidate.json"
        stem = "agent-channel"
        default_receipt = folder / (stem + ".finalize-summary.json")
    else:
        folder = root / ".archify/tree-conversation-capabilities-20261016-general"
        source = root / "docs/diagrams/conversation-capabilities.tree.json"
        stem = "conversation-capabilities"
        default_receipt = folder / "revision-3" / (stem + ".finalize-summary.json")
    receipt_path = args.receipt or default_receipt
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("status") != "pass":
        parser.error("Archify finalize must pass before publication")
    for path, key in [(source, "specification"), (folder / (stem + ".html"), "artifact")]:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != receipt[key]["sha256"]:
            parser.error("Archify source/artifact differs from its passing receipt")
    normalized = b"\n".join(b"" if not line.strip() else line for line in content.split(b"\n"))
    public = root / "docs/diagrams" / (stem + ".html")
    if args.check:
        if not public.exists() or public.read_bytes() != normalized:
            parser.error("Public diagram differs from the normalized Archify artifact")
    else:
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_bytes(normalized)
    print("Public diagram matches the validated source artifact.")


if __name__ == "__main__":
    main()
