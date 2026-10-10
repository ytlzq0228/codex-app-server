from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from .multimodal import IMAGE_TYPES, content_parts, user_parts


class OpenAIRequestModel(BaseModel):
    # OpenAI adds optional fields over time; accept them for SDK compatibility.
    model_config = ConfigDict(extra="allow")


class InputMessage(OpenAIRequestModel):
    role: Literal["user", "assistant", "system", "developer", "tool"]
    content: str | list[dict[str, Any]]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(str(part.get("text", "")) for part in self.content if part.get("type") in {"input_text", "output_text", "text"})

    def has_non_text_content(self) -> bool:
        return isinstance(self.content, list) and any(part.get("type") not in {"input_text", "output_text", "text"} for part in self.content)


class ResponseStreamOptions(OpenAIRequestModel):
    include_obfuscation: bool = True


class ResponseRequest(OpenAIRequestModel):
    _upstream_model: str | None = PrivateAttr(default=None)
    # Server-only: never populated from client JSON or persisted as request input.
    _execution_input_text: str | None = PrivateAttr(default=None)
    _execution_input_items: list[Any] | None = PrivateAttr(default=None)
    _execution_auto_resume: bool = PrivateAttr(default=False)
    _recovery_response_id: str | None = PrivateAttr(default=None)
    model: str
    input: str | dict[str, Any] | list[Any]
    instructions: str | None = None
    previous_response_id: str | None = None
    stream: bool = False
    stream_options: ResponseStreamOptions | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    metadata: dict[str, str] | None = None
    store: bool | None = None
    temperature: float | None = None
    top_p: float | None = None
    reasoning: dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    truncation: Literal["auto", "disabled"] | None = None
    service_tier: str | None = None
    safety_identifier: str | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    parallel_tool_calls: bool | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    include: list[str] | None = None
    background: bool | None = None
    conversation: str | dict[str, Any] | None = None
    prompt: str | dict[str, Any] | None = None
    max_tool_calls: int | None = None
    user: str | None = None
    top_logprobs: int | None = None

    @model_validator(mode="after")
    def ensure_input(self) -> "ResponseRequest":
        if isinstance(self.input, str) and not self.input.strip():
            raise ValueError("input must not be empty")
        if isinstance(self.input, list) and not self.input:
            raise ValueError("input must not be empty")
        if isinstance(self.input, dict) and not self.input:
            raise ValueError("input must not be empty")
        return self

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and part.get("type") in {None, "text", "input_text", "output_text"}:
                value = part.get("text")
                if isinstance(value, str):
                    texts.append(value)
        return "\n".join(texts)

    @classmethod
    def _item_text(cls, item: Any) -> str:
        if isinstance(item, str):
            return item
        if not isinstance(item, dict):
            return ""
        item_type = item.get("type")
        role = str(item.get("role") or "user").upper()
        if item_type in {"input_text", "output_text", "text"}:
            return str(item.get("text") or "")
        if item_type in {"function_call_output", "computer_call_output", "custom_tool_call_output"}:
            output = item.get("output")
            text = output if isinstance(output, str) else cls._content_text(output)
            return f"TOOL OUTPUT:\n{text}" if text else ""
        if item_type in {"function_call", "custom_tool_call"}:
            name = item.get("name") or "tool"
            arguments = item.get("arguments") or item.get("input") or ""
            return f"ASSISTANT TOOL CALL {name}:\n{arguments}"
        text = cls._content_text(item.get("content"))
        return f"{role}:\n{text}" if text else ""

    @classmethod
    def _item_is_unsupported(cls, item: Any) -> bool:
        if isinstance(item, str):
            return False
        if not isinstance(item, dict):
            return True
        item_type = item.get("type")
        if item_type in {"input_file", "computer_screenshot", "item_reference"}:
            return True
        if item_type not in {None, "message", "additional_tools", "input_text", "output_text", "text", "input_image", "image_url", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
            return True
        if item_type in IMAGE_TYPES:
            content_parts([item])
        elif item_type in {None, "message"}:
            content_parts(item.get("content"))
        return False

    def unsupported(self) -> tuple[str, str] | None:
        from .client_tools import validate, ToolProtocolError
        try:
            validate(self)
        except ToolProtocolError as exc:
            return "tools", str(exc)
        items = self.input if isinstance(self.input, list) else [self.input]
        try:
            if any(self._item_is_unsupported(item) for item in items):
                return "input", "This input item type is not supported by the gateway"
        except ValueError as exc:
            return "input", str(exc)
        if self.background:
            return "background", "Background responses are not supported"
        if self.conversation is not None:
            return "conversation", "The conversation parameter is not supported; use previous_response_id"
        if self.prompt is not None:
            return "prompt", "Prompt templates are not supported"
        from .providers import provider_for
        if self.top_logprobs is not None or (provider_for(self.model) != "claude" and self.include and "message.output_text.logprobs" in self.include):
            return "top_logprobs", "Token log probabilities are not supported by this Codex gateway"
        return None

    def output_schema(self) -> dict[str, Any] | None:
        output_format = (self.text or {}).get("format")
        if not isinstance(output_format, dict):
            return None
        format_type = output_format.get("type", "text")
        if format_type == "json_schema":
            schema = output_format.get("schema")
            if schema is None and isinstance(output_format.get("json_schema"), dict):
                schema = output_format["json_schema"].get("schema")
            return schema if isinstance(schema, dict) else None
        if format_type == "json_object":
            return {"type": "object"}
        return None

    def input_text(self) -> str:
        if self._execution_input_text is not None:
            return self._execution_input_text
        items = self.input if isinstance(self.input, list) else [self.input]
        messages = [self._item_text(item) for item in items]
        if self.instructions:
            messages.insert(0, self.instructions)
        return "\n\n".join(text for text in messages if text)

    def worker_input(self) -> list[dict[str, Any]]:
        """Keep image positions and role boundaries in flattened conversation input."""
        items = self._execution_input_items
        if items is None:
            items = self.input if isinstance(self.input, list) else [self.input]
        result = []
        if self.instructions and self._execution_input_items is None:
            result.append({"type": "text", "text": self.instructions})
        for item in items:
            if isinstance(item, dict):
                kind = item.get("type")
                if kind == "additional_tools":
                    continue
                if kind in IMAGE_TYPES:
                    result.extend(user_parts([item]))
                    continue
                if kind in {None, "message", "function_call_output", "custom_tool_call_output"}:
                    tool = kind in {"function_call_output", "custom_tool_call_output"}
                    content = item.get("output" if tool else "content")
                    parts = user_parts(content)
                    if any(p["type"] == "image" for p in parts):
                        label = "TOOL OUTPUT" if tool else str(item.get("role") or "user").upper()
                        result.append({"type": "text", "text": label + ":\n"})
                        result.extend(parts)
                        continue
            if text := self._item_text(item):
                result.append({"type": "text", "text": text})
        # Preserve the exact existing text-only prompt and execution delta behavior.
        if not any(p["type"] == "image" for p in result):
            return [{"type": "text", "text": self.input_text()}]
        return result


class ChatMessage(OpenAIRequestModel):
    role: Literal["user", "assistant", "system", "developer", "tool", "function"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    function_call: dict[str, Any] | None = None
    refusal: str | None = None

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if not self.content:
            return self.refusal or ""
        texts: list[str] = []
        for part in self.content:
            if part.get("type") in {"text", "input_text", "output_text"}:
                texts.append(str(part.get("text", "")))
        return "\n".join(texts)

    def has_non_text_content(self) -> bool:
        return isinstance(self.content, list) and any(part.get("type") not in {"text", "input_text", "output_text"} for part in self.content)


class ChatStreamOptions(OpenAIRequestModel):
    include_usage: bool = False
    include_obfuscation: bool = True


class ChatCompletionRequest(OpenAIRequestModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    stream_options: ChatStreamOptions | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = None
    top_p: float | None = None
    n: int = Field(default=1, gt=0)
    stop: str | list[str] | None = None
    user: str | None = None
    seed: int | None = None
    response_format: dict[str, Any] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    service_tier: str | None = None
    parallel_tool_calls: bool | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    functions: list[dict[str, Any]] | None = None
    function_call: Any = None
    metadata: dict[str, str] | None = None
    store: bool | None = None
    prediction: dict[str, Any] | None = None
    modalities: list[str] | None = None
    audio: dict[str, Any] | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    safety_identifier: str | None = None

    @model_validator(mode="after")
    def ensure_messages(self) -> "ChatCompletionRequest":
        if not self.messages:
            raise ValueError("messages must not be empty")
        if not any(message.text().strip() or isinstance(message.content, list) and message.content
                   for message in self.messages):
            raise ValueError("messages must contain text or image content")
        return self

    def unsupported(self) -> tuple[str, str] | None:
        if self.n != 1:
            return "n", "Only n=1 is supported"
        from .providers import provider_for
        if (provider_for(self.model) != "claude" and self.tool_choice not in (None, "none", "auto")) or self.function_call not in (None, "none", "auto"):
            return ("tool_choice" if self.tool_choice not in (None, "none", "auto") else "function_call"), "Forced tool calling is not supported by this Codex gateway"
        if self.modalities and self.modalities != ["text"]:
            return "modalities", "Only text output is supported"
        if self.logprobs or self.top_logprobs is not None:
            return "logprobs", "Token log probabilities are not supported by this Codex gateway"
        if self.prediction is not None:
            return "prediction", "Predicted output is not supported by this Codex gateway"
        if self.functions or any(message.function_call for message in self.messages):
            return "functions", "Legacy function_call format is unsupported; use tools/tool_calls"
        from .client_tools import FORBIDDEN, ToolProtocolError, validate
        if any((self.model_extra or {}).get(k) is not None for k in FORBIDDEN):
            return "config", "Client overrides of Worker security policy are forbidden"
        try:
            request = self.to_response_request()
            validate(request)
        except (ToolProtocolError, ValueError, TypeError, KeyError) as exc:
            return "tools", str(exc)
        if unsupported := request.unsupported():
            return ("messages" if unsupported[0] == "input" else unsupported[0]), unsupported[1]
        return None

    def to_response_request(self) -> ResponseRequest:
        items = []
        for message in self.messages:
            if message.role == "tool":
                items.append({"type":"function_call_output", "call_id":message.tool_call_id, "output":message.content if message.content is not None else message.text()})
            else:
                if message.content or message.text():
                    items.append({"role":message.role, "content":message.content if message.content is not None else message.text()})
                for call in message.tool_calls or []:
                    if call.get("type") != "function":
                        raise ValueError("Only function tool_calls are supported in Chat Completions")
                    items.append({"type":"function_call", "call_id":call["id"], "name":call["function"]["name"], "arguments":call["function"]["arguments"]})
        tools = []
        for tool in self.tools or []:
            if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                raise ValueError("Chat Completions supports only function tools")
            tools.append({**tool["function"], "type":"function"})
        choice = self.tool_choice
        if isinstance(choice, dict) and choice.get("type") == "function" and set(choice) == {"type", "function"}:
            function = choice["function"]
            if not isinstance(function, dict) or set(function) != {"name"}:
                raise ValueError("Invalid forced function choice")
            choice = {"type": "function", "name": function["name"]}
        return ResponseRequest(
            model=self.model, input=items, stream=self.stream, tools=tools, tool_choice=choice,
            max_output_tokens=self.max_completion_tokens or self.max_tokens,
            temperature=self.temperature, top_p=self.top_p, metadata=self.metadata,
            store=self.store, reasoning={"effort": self.reasoning_effort} if self.reasoning_effort else None,
            service_tier=self.service_tier, safety_identifier=self.safety_identifier or self.user,
            prompt_cache_key=self.prompt_cache_key, prompt_cache_retention=self.prompt_cache_retention,
            text={"format": self.response_format} if self.response_format else None,
        )


class BackendResult(BaseModel):
    usage_accounting: dict[str, Any] | None = None
    text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    thread_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class BackendStreamEvent(BaseModel):
    usage_accounting: dict[str, Any] | None = None
    tool_call: dict[str, Any] | None = None
    delta: str = ""
    thread_id: str | None = None
    done: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
