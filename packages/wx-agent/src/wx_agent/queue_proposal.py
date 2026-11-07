"""Pure local proposal construction and exact native-tool receipt verification.

Models supply semantic decisions. Identity and snapshot digests come from the
reader, and no function here loads a gateway, writes state, or sends a message.
"""
import hashlib
import shlex

from .codex_plugin import PluginError
from .macos_sender_driver import Refused, validate_text

FINAL_MARKER = "WXCLI_PROPOSAL_READY"
MAX_PROPOSAL_OUTPUT = 8192


def bind_proposal(file, ref, semantics):
    # Lazy import keeps the snapshot CLI independent of credential-bearing
    # configuration and avoids a module import cycle with its CLI dispatcher.
    from .queued_snapshot import encoded, snapshot_payload
    payload = snapshot_payload(file, ref)
    if not isinstance(semantics, dict):
        raise PluginError("invalid_proposal_semantics")
    decision = semantics.get("decision")
    allowed = {"decision", "reason"}
    required = set(allowed)
    if decision == "reply":
        required |= {"text", "addressed_to_us", "source_cursors"}
        allowed |= required
    elif decision == "draft":
        required.add("draft")
        allowed |= {"draft", "condition", "due_at"}
    elif decision == "defer":
        allowed |= {"condition", "due_at"}
    elif decision != "skip":
        raise PluginError("invalid_proposal_decision")
    if not required <= set(semantics) or set(semantics) - allowed:
        raise PluginError("invalid_proposal_semantics")
    for key, limit in (("reason", 512), ("condition", 512), ("draft", 65536)):
        if key in semantics and (not isinstance(semantics[key], str) or not semantics[key].strip()
                                 or "\0" in semantics[key] or len(semantics[key].encode()) > limit):
            raise PluginError("invalid_proposal_text")
    if "due_at" in semantics and (type(semantics["due_at"]) is not int or semantics["due_at"] < 0):
        raise PluginError("invalid_proposal_due_at")
    if decision == "reply":
        if semantics["addressed_to_us"] is not True:
            raise PluginError("reply_not_addressed")
        try:
            validate_text(semantics["text"])
        except (Refused, TypeError) as exc:
            raise PluginError("invalid_reply_text") from exc
        sources = semantics["source_cursors"]
        if (not isinstance(sources, list) or not 1 <= len(sources) <= 100
                or any(type(cursor) is not int or cursor <= 0 for cursor in sources)
                or len(set(sources)) != len(sources)):
            raise PluginError("invalid_reply_sources")
    result = {"protocol_version": 1, "ref": ref,
              "snapshot_sha256": hashlib.sha256(payload).hexdigest(), **semantics}
    if len(encoded(result)) + 1 > MAX_PROPOSAL_OUTPUT:
        raise PluginError("proposal_output_too_large")
    return result


def proposal_prefix(cli, note):
    return shlex.join([str(cli), "propose", "--file", note["snapshot_file"], "--ref", note["ref"]])


def proposal_delimiter(note):
    return "WXCLI_PROPOSAL_" + note["ref"][:24].upper()


def parse_proposal_script(command, cli, note):
    """Only a fixed quoted heredoc can supply data to the local proposer.

    No shell program is evaluated here. The matching delimiter must occur once
    at the end, so an injected terminator or trailing shell action is rejected.
    """
    from .queued_snapshot import strict_json
    delimiter = proposal_delimiter(note)
    lines = command.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    prefix = proposal_prefix(cli, note) + " <<'" + delimiter + "'"
    if not lines or lines[0] != prefix:
        return None
    if len(lines) < 3 or lines[-1] != delimiter or delimiter in lines[1:-1]:
        raise PluginError("noncanonical_proposal_command")
    raw = "\n".join(lines[1:-1])
    if len(raw.encode()) > MAX_PROPOSAL_OUTPUT:
        raise PluginError("proposal_input_too_large")
    value = strict_json(raw)
    if not isinstance(value, dict):
        raise PluginError("invalid_proposal_semantics")
    return value


def receipt_from_item(item, route, note, cli):
    from .queued_snapshot import encoded, strict_json
    if item.get("type") != "commandExecution":
        return None
    command = item.get("command", "")
    if not isinstance(command, str):
        return None
    try:
        args = shlex.split(command)
    except (ValueError, TypeError):
        return None
    if len(args) == 3 and args[0] == "/bin/zsh" and args[1] in ("-c", "-lc"):
        command = args[2]
    semantics = parse_proposal_script(command, cli, note)
    if semantics is None:
        return None
    if (item.get("cwd") != str(route.project) or item.get("status") != "completed"
            or item.get("source", "agent") not in ("agent", "unifiedExecStartup", "unifiedExecInteraction")
            or type(item.get("exitCode")) is not int or item["exitCode"] != 0):
        raise PluginError("proposal_receipt_invalid")
    expected = bind_proposal(note["snapshot_file"], note["ref"], semantics)
    actual = strict_json(item.get("aggregatedOutput", ""))
    if encoded(actual) != encoded(expected):
        raise PluginError("proposal_output_changed")
    return expected
