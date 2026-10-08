"""Relay native Worker questions through the client's declared form tool."""
from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

from .client_tools import ToolProtocolError, public_call


def async_questions(item):
    if (not isinstance(item, dict)
            or item.get("type") not in {"agentMessage", "AgentMessage"}
            or item.get("delivery") != "async" or "questions" not in item):
        return None
    questions = item["questions"]
    if not isinstance(questions, list) or not questions:
        raise ToolProtocolError("Worker returned invalid async questions")
    for question in questions:
        if (not isinstance(question, dict) or not isinstance(question.get("title"), str)
                or not question["title"].strip()
                or set(question) - {"title", "options"}):
            raise ToolProtocolError("Worker returned invalid async questions")
        if "options" in question and (not isinstance(question["options"], list)
                or not question["options"]
                or any(not isinstance(option, str) or not option.strip()
                       for option in question["options"])):
            raise ToolProtocolError("Worker returned invalid async question options")
    return questions


def question_call(specs, questions):
    candidates = [spec for spec in specs
                  if spec["name"] == "request_user_input_async"
                  and spec["namespace"] in {None, "functions"}
                  and spec["kind"] == "function"]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ToolProtocolError("Ambiguous client async question tool")
    spec = candidates[0]
    arguments = {"questions": questions}
    try:
        validator = validators.validator_for(spec["schema"])
        validator.check_schema(spec["schema"])
        validator(spec["schema"]).validate(arguments)
    except (SchemaError, ValidationError) as exc:
        raise ToolProtocolError("Worker questions do not match the client form schema") from exc
    return public_call(specs, {"tool": spec["alias"], "arguments": arguments})


def question_text(questions):
    return "\n\n".join(question["title"] + "".join(
        "\n- " + option for option in question.get("options", []))
        for question in questions) + "\n\n"
