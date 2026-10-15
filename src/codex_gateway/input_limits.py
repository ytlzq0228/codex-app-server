"""Codex turn input limits, applied after execution-history reduction."""

CODEX_MAX_INPUT_CHARACTERS = 1_048_576
INPUT_TOO_LONG_CODE = "input_too_long"


def input_length_error(items):
    size = sum(len(item.get("text", "")) for item in items if item.get("type") == "text")
    if size > CODEX_MAX_INPUT_CHARACTERS:
        return (f"Input contains {size} characters; Codex accepts at most "
                f"{CODEX_MAX_INPUT_CHARACTERS}. Shorten or summarize the conversation "
                "and tool outputs before retrying. Retrying unchanged input will fail.")
    return None


def is_input_length_error(message):
    return "input exceeds the maximum length" in message.lower()
