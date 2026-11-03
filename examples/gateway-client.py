#!/usr/bin/env python3
"""Synthetic gateway call; copy private configs outside the repository first."""
import argparse
import json
from pathlib import Path
import sys

from wx_agent.gateway_client import GatewayClient, GatewayClientError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--text-stdin", action="store_true")
    args = parser.parse_args()
    method = "send_text" if args.text_stdin else "result"
    params = {"conversation_id": args.conversation_id, "request_id": args.request_id}
    if args.text_stdin:
        params["text"] = sys.stdin.read(65537)
    try:
        result = GatewayClient(args.config).request(method, params)
    except GatewayClientError as exc:
        print(json.dumps({"error": {"code": exc.code}, "id": exc.request_id}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
