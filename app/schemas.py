from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _OpenAIModel(BaseModel):
    """Base for OpenAI v1 mirrors. Allows extra fields so we passthrough
    OpenAI-vendor extensions without 422 errors, but emits them verbatim."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Tool blocks
# ---------------------------------------------------------------------------


class FunctionDefinition(_OpenAIModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None


class ToolDefinition(_OpenAIModel):
    type: Literal["function"]
    function: FunctionDefinition


ToolChoice = Literal["none", "auto", "required"] | dict[str, Any]


class ToolCallFunction(_OpenAIModel):
    name: str
    arguments: str  # JSON-encoded string per OpenAI spec


class ToolCall(_OpenAIModel):
    id: str
    type: Literal["function"]
    function: ToolCallFunction


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------


class ChatMessage(_OpenAIModel):
    role: Literal[
        "system", "user", "assistant", "tool", "developer"
    ]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# Chat completions request
# ---------------------------------------------------------------------------


class ChatCompletionRequest(_OpenAIModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    user: str | None = None
    response_format: dict[str, Any] | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: ToolChoice | None = None
    parallel_tool_calls: bool | None = None
    stream_options: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Chat completion (non-streaming) response
# ---------------------------------------------------------------------------


class ChatCompletionUsage(_OpenAIModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionChoice(_OpenAIModel):
    index: int
    message: ChatMessage
    finish_reason: Literal[
        "stop",
        "length",
        "tool_calls",
        "content_filter",
        "function_call",
    ] | None = None
    logprobs: dict[str, Any] | None = None


class ChatCompletionResponse(_OpenAIModel):
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage | None = None
    system_fingerprint: str | None = None


# ---------------------------------------------------------------------------
# Chat completion chunk (streaming)
# ---------------------------------------------------------------------------


class ChatCompletionDelta(_OpenAIModel):
    role: Literal[
        "system", "user", "assistant", "tool"
    ] | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatCompletionChunkChoice(_OpenAIModel):
    index: int
    delta: ChatCompletionDelta
    finish_reason: Literal[
        "stop", "length", "tool_calls", "content_filter"
    ] | None = None
    logprobs: dict[str, Any] | None = None


class ChatCompletionChunk(_OpenAIModel):
    id: str
    object: Literal["chat.completion.chunk"]
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: ChatCompletionUsage | None = None
    system_fingerprint: str | None = None


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class EmbeddingsRequest(_OpenAIModel):
    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: Literal["float", "base64"] | None = None
    dimensions: int | None = None
    user: str | None = None


class EmbeddingItem(_OpenAIModel):
    object: Literal["embedding"]
    index: int
    embedding: list[float] | str  # str when encoding_format == "base64"


class EmbeddingsUsage(_OpenAIModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(_OpenAIModel):
    object: Literal["list"]
    data: list[EmbeddingItem]
    model: str
    usage: EmbeddingsUsage


# ---------------------------------------------------------------------------
# Models list
# ---------------------------------------------------------------------------


class ModelEntry(_OpenAIModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "local"


class ModelsListResponse(_OpenAIModel):
    object: Literal["list"] = "list"
    data: list[ModelEntry]
