"""One local entry point for the channel controller and its guarded tools."""
import argparse
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="wx-agent", description=__doc__)
    parser.add_argument("command", choices=("start", "status", "pause", "resume", "stop", "run", "reply", "sender", "probe", "memory"))
    if not args or args[0] in ("-h", "--help"):
        parser.parse_args(args)
        return 0
    command = parser.parse_args(args[:1]).command
    remaining = args[1:]
    if command in ("start", "status", "pause", "resume", "stop", "run"):
        from .wechat_agent_ctl import main as control
        return control([*remaining, command])
    if command == "reply":
        from .codex_reply_agent import main as reply
        return reply(remaining)
    if command == "sender":
        from .macos_sender_driver import main as sender
        return sender(remaining)
    if command == "probe":
        from .sender_guard_probe import main as probe
        return probe(remaining)
    from .memory import main as memory
    return memory(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
