"""One local entry point for managed WeChat conversations and their guarded tools."""
import argparse
import sys

COMMANDS = ("conversations", "dispatch", "inbox", "memory", "sender", "probe")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="wx-agent", description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    if not args or args[0] in ("-h", "--help"):
        parser.parse_args(args)
        return 0
    command = parser.parse_args(args[:1]).command
    remaining = args[1:]
    if command == "sender":
        from .macos_sender_driver import main as sender
        return sender(remaining)
    if command == "probe":
        from .sender_guard_probe import main as probe
        return probe(remaining)
    if command == "conversations":
        from .conversations import main as conversations
        return conversations(remaining)
    if command == "dispatch":
        from .dispatch import main as dispatch
        return dispatch(remaining)
    if command == "inbox":
        from .inbox import main as inbox
        return inbox(remaining)
    from .memory import main as memory
    return memory(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
