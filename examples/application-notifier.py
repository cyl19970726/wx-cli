#!/usr/bin/env python3
"""Local, no-model application notice example (installed wx_agent package required)."""
import argparse
import json
from pathlib import Path
import sys

from wx_agent.application_client import ApplicationClient, ApplicationError


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Copy the synthetic config to a private 0600 file, fill its local paths and existing owner/epoch, then use one stable request ID per business notice.")
    parser.add_argument("--config", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("send", "status"):
        action = actions.add_parser(name)
        action.add_argument("--conversation-id", required=True)
        action.add_argument("--request-id", required=True,
                            help="original business key, e.g. notice-001; status uses this key, never the returned app_a.notice-001 ID")
        if name == "send":
            action.add_argument("--text-stdin", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        client = ApplicationClient(args.config)
        if args.action == "send":
            result = client.send_text(args.conversation_id, args.request_id, sys.stdin.read())
        else:
            result = client.status(args.conversation_id, args.request_id)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ApplicationError as exc:
        print(json.dumps({"error": exc.code, "request_id": exc.request_id}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
