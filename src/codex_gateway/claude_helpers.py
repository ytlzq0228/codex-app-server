"""Narrow compatibility rules for Claude Code helper requests.

Fingerprints cover the entire final text (whitespace normalized), observed in
Claude Code 2.1.287, plus the verified 2.1.288 status template.
Client headers select compatibility behavior, not trust.
Changing templates fail closed to ordinary conversation handling.
"""
import hashlib

RULE_VERSION = "claude-code-2.1.287-288-v2"
TEMPLATES = {
    "9da3d633ac95c3b2cc58994dd7db87ca77bc47dec10f8255c50327b8f79b1f43": "status_summary",
    "b5667154236ffbd7191ea1eb33247c064e8cb15787a596b6ef9d724cbeb87745": "context_compaction",
}


def dialogue_tail(items):
    """Inspect past trailing reminders without removing them from execution."""
    tail = list(items or [])
    while tail and isinstance(tail[-1], dict) and tail[-1].get("role") in {"system", "developer"}:
        tail.pop()
    return tail[-1] if tail else None


def auxiliary_kind(params, headers):
    if params.get("previous_response_id"):
        return None
    if headers.get("x-app") != ["cli"] or not any(
        value.startswith(("claude-cli/2.1.287 ", "claude-cli/2.1.288 ")) for value in headers.get("user-agent", [])
    ):
        return None
    items = params.get("input")
    if not isinstance(items, list):
        return None
    item = dialogue_tail(items)
    if not isinstance(item, dict) or item.get("role") != "user" or item.get("type", "message") != "message":
        return None
    content = item.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content and all(
        isinstance(p, dict) and p.get("type") in {"text", "input_text"} and isinstance(p.get("text"), str)
        for p in content
    ):
        text = "\n".join(p["text"] for p in content)
    else:
        return None
    kind = TEMPLATES.get(hashlib.sha256(" ".join(text.split()).encode()).hexdigest())
    # Only the status template has been verified from 2.1.288 production traffic.
    if kind == "context_compaction" and not any(v.startswith("claude-cli/2.1.287 ") for v in headers.get("user-agent", [])):
        return None
    return kind
