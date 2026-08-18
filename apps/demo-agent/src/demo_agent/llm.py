"""The LLM provider layer.

Everything targets the **OpenAI-compatible** `/v1/chat/completions` shape, which Ollama
serves natively. That single choice is what makes the provider swappable by configuration
rather than by code: Groq, OpenAI, vLLM, and llama.cpp all speak the same wire format.

Default is Ollama with `qwen3` running locally -- free, open-weight, no API key, no rate
limit, and native tool calling on every model size, which matters because this whole
project exists to intercept tool calls.

The guardrail's own decision path never calls an LLM. Inference speed and availability
therefore affect the *agent*, never enforcement.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen3:latest"

# Not a credential -- ruff flags any name ending in TOKEN.
NO_THINK_TOKEN = "/no_think"  # noqa: S105
"""Qwen3's in-prompt control token for skipping its reasoning phase."""


@dataclass
class ToolCall:
    """One tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    """The model's response to one turn: prose, tool calls, or both."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """The model could not be reached or returned something unusable."""


@dataclass
class LLMConfig:
    """Where inference happens. All of it is environment-configurable.

    Switching to a hosted provider is three variables and no code change:

        GUARDRAIL_LLM_BASE_URL=https://api.groq.com/openai/v1
        GUARDRAIL_LLM_MODEL=qwen/qwen3-32b
        GUARDRAIL_LLM_API_KEY=gsk_...
    """

    base_url: str = field(
        default_factory=lambda: os.environ.get("GUARDRAIL_LLM_BASE_URL", DEFAULT_BASE_URL)
    )
    model: str = field(default_factory=lambda: os.environ.get("GUARDRAIL_LLM_MODEL", DEFAULT_MODEL))
    api_key: str = field(
        # Ollama ignores the value but the OpenAI wire format requires the header.
        default_factory=lambda: os.environ.get("GUARDRAIL_LLM_API_KEY", "ollama")
    )
    temperature: float = 0.0
    """Zero, so a demo run is reproducible. An agent that picks a different tool each
    time makes a governance failure impossible to distinguish from model variance."""

    timeout: float = 600.0
    """Generous: an 8B model on CPU takes tens of seconds per turn, plus a one-off pause
    to load 5 GB of weights on the first call. That is fine here -- inference is nowhere
    near the guardrail's hot path, and the decision path never calls a model at all."""

    disable_thinking: bool = field(
        default_factory=lambda: (
            os.environ.get("GUARDRAIL_LLM_THINKING", "").lower() not in {"1", "true", "yes"}
        )
    )
    """Qwen3 reasons before answering unless told not to.

    Measured on this machine: thinking exceeded a 300s timeout for a five-tool prompt,
    while `/no_think` returned a correct tool call in 34s. Reasoning traces add little
    for tool selection -- the decision is "which function, which arguments" -- so it is
    off by default and re-enabled with GUARDRAIL_LLM_THINKING=1.
    """


class LLMProvider:
    """Minimal OpenAI-compatible chat client with tool calling."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self._http = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "content-type": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LLMProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def available(self) -> bool:
        """Whether inference is reachable. Checked at startup so a failure is reported
        as "Ollama is not running" rather than as a timeout mid-task."""
        try:
            return self._http.get("/models").status_code < 500
        except httpx.HTTPError:
            return False

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Turn:
        """One turn of conversation."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._prepare(messages),
            "temperature": self.config.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        try:
            response = self._http.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(
                f"could not reach the model at {self.config.base_url}: {exc}. "
                "Is `ollama serve` running?"
            ) from exc

        if response.status_code >= 400:
            raise LLMError(f"model returned {response.status_code}: {response.text[:300]}")

        return self._parse(response.json())

    def _prepare(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply the thinking switch without mutating the caller's list.

        `/no_think` is Qwen3's in-prompt control token. Appending it to the system message
        rather than every user turn keeps it out of the conversation the model reasons
        about, and keeps the agent's prompt readable.
        """
        if not self.config.disable_thinking:
            return messages

        prepared = [dict(m) for m in messages]
        for message in prepared:
            if message.get("role") == "system":
                message["content"] = f"{message.get('content', '')}\n\n{NO_THINK_TOKEN}"
                return prepared

        # No system message to attach it to; prepend one rather than silently skipping.
        return [{"role": "system", "content": NO_THINK_TOKEN}, *prepared]

    @staticmethod
    def _parse(body: dict[str, Any]) -> Turn:
        """Turn a chat completion into a Turn.

        Tool-call arguments arrive as a JSON *string*, and a small model occasionally
        emits one that does not parse. That is treated as "no usable tool call" rather
        than raising: the agent can then ask the model to try again, which is a better
        outcome than crashing the run.
        """
        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"model returned no choices: {json.dumps(body)[:300]}")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            name = function.get("name")
            if not name:
                continue

            raw_args = function.get("arguments")
            if isinstance(raw_args, dict):
                arguments = raw_args
            else:
                try:
                    arguments = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    continue
            if not isinstance(arguments, dict):
                continue

            calls.append(
                ToolCall(id=raw.get("id") or f"call_{len(calls)}", name=name, arguments=arguments)
            )

        return Turn(content=content, tool_calls=calls)
