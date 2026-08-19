"""Provider portability.

The project claims repeatedly that switching inference providers is a configuration
change rather than a code change. Until now nothing verified that: the agent tests use a
scripted LLM, which bypasses `LLMProvider._parse` entirely -- so the one function that
actually absorbs provider differences had no direct coverage.

These tests fix that by parsing recorded response bodies and asserting all three
providers yield an identical `Turn`.

**Fixture provenance, stated plainly:**

* `ollama_qwen3_tool_call.json` was **captured from a real local call** to
  `qwen3:latest` through Ollama's OpenAI-compatible endpoint.
* `groq_*.json` and `openai_*.json` are **constructed from the providers' documented
  response schemas**, since calling them needs API keys this project deliberately does
  not have. They encode the real shape differences that matter -- Groq's `x_groq` and
  timing fields, OpenAI's `refusal`/`annotations`/`service_tier`, and `content: null`
  where Ollama sends `""`.

The value is the same either way: the parser must tolerate unknown extra fields and must
not depend on any provider-specific one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from demo_agent.llm import LLMConfig, LLMError, LLMProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"

TOOL_CALL_FIXTURES = [
    "ollama_qwen3_tool_call.json",
    "groq_qwen3_tool_call.json",
    "openai_tool_call.json",
]


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Cross-provider equivalence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", TOOL_CALL_FIXTURES)
def test_every_provider_yields_the_same_tool_call(fixture: str) -> None:
    """The core portability claim: same request, same parsed result, any provider."""
    turn = LLMProvider._parse(_load(fixture))

    assert turn.wants_tools
    assert len(turn.tool_calls) == 1

    call = turn.tool_calls[0]
    assert call.name == "email_send"
    assert call.arguments == {"to": "partner@external.com", "subject": "Q3 Report"}
    assert call.id


def test_all_providers_parse_to_identical_arguments() -> None:
    """Asserted across providers together, not just per-fixture.

    A per-provider test could pass while the three still disagreed with each other, and
    disagreement is exactly what would break a provider swap.
    """
    parsed = [LLMProvider._parse(_load(name)).tool_calls[0] for name in TOOL_CALL_FIXTURES]

    assert len({json.dumps(c.arguments, sort_keys=True) for c in parsed}) == 1
    assert len({c.name for c in parsed}) == 1


@pytest.mark.parametrize("fixture", TOOL_CALL_FIXTURES)
def test_provider_specific_extra_fields_are_ignored(fixture: str) -> None:
    """Groq sends `x_groq`, OpenAI sends `refusal` and `service_tier`, Ollama sends
    `reasoning` and an `index` inside each tool call. None may affect parsing, and a
    provider adding a field tomorrow must not break the agent."""
    body = _load(fixture)
    body["choices"][0]["message"]["some_future_field"] = {"nested": True}
    body["a_brand_new_top_level_key"] = 123

    turn = LLMProvider._parse(body)

    assert turn.tool_calls[0].name == "email_send"


def test_null_content_is_normalised_to_empty_string() -> None:
    """Groq and OpenAI send `content: null` alongside tool calls; Ollama sends `""`.

    The agent concatenates content into the transcript, so a None leaking through would
    raise at a point far away from its cause.
    """
    for name in TOOL_CALL_FIXTURES:
        turn = LLMProvider._parse(_load(name))
        assert isinstance(turn.content, str)


def test_a_plain_answer_produces_no_tool_calls() -> None:
    """The turn that ends a run: the model explaining a refusal, with nothing to execute."""
    turn = LLMProvider._parse(_load("plain_answer_no_tools.json"))

    assert not turn.wants_tools
    assert "db-bulk-delete" in turn.content


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


def test_unparseable_tool_arguments_are_dropped_not_raised() -> None:
    """Small models occasionally emit invalid JSON in `arguments`.

    Dropping the call lets the agent ask the model to try again; raising would end the
    run over a recoverable formatting slip.
    """
    body = _load("ollama_qwen3_tool_call.json")
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not valid json"

    turn = LLMProvider._parse(body)

    assert turn.tool_calls == []


def test_arguments_already_decoded_as_a_dict_are_accepted() -> None:
    """Some OpenAI-compatible proxies hand back a decoded object rather than a string."""
    body = _load("groq_qwen3_tool_call.json")
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {
        "to": "partner@external.com",
        "subject": "Q3 Report",
    }

    turn = LLMProvider._parse(body)

    assert turn.tool_calls[0].arguments["to"] == "partner@external.com"


def test_a_tool_call_without_a_name_is_skipped() -> None:
    body = _load("openai_tool_call.json")
    del body["choices"][0]["message"]["tool_calls"][0]["function"]["name"]

    assert LLMProvider._parse(body).tool_calls == []


def test_arguments_that_are_not_an_object_are_skipped() -> None:
    """`arguments: "[1,2,3]"` parses as JSON but cannot be splatted into a function."""
    body = _load("openai_tool_call.json")
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "[1,2,3]"

    assert LLMProvider._parse(body).tool_calls == []


def test_a_response_with_no_choices_is_an_error() -> None:
    """Unlike a malformed tool call, this is not recoverable by retrying the turn."""
    with pytest.raises(LLMError, match="no choices"):
        LLMProvider._parse({"id": "x", "choices": []})


def test_missing_tool_calls_key_is_treated_as_a_plain_answer() -> None:
    body = _load("plain_answer_no_tools.json")
    body["choices"][0]["message"].pop("tool_calls", None)

    assert LLMProvider._parse(body).tool_calls == []


# ---------------------------------------------------------------------------
# Configuration is what selects the provider
# ---------------------------------------------------------------------------


def test_provider_is_selected_entirely_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The portability claim at the configuration level: no code path branches on
    provider identity, so pointing at Groq is three environment variables."""
    monkeypatch.setenv("GUARDRAIL_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("GUARDRAIL_LLM_MODEL", "qwen/qwen3-32b")
    monkeypatch.setenv("GUARDRAIL_LLM_API_KEY", "gsk_test")

    config = LLMConfig()

    assert config.base_url == "https://api.groq.com/openai/v1"
    assert config.model == "qwen/qwen3-32b"
    assert config.api_key == "gsk_test"


def test_defaults_target_local_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """No configuration at all must mean free local inference, not a paid API."""
    for name in ("GUARDRAIL_LLM_BASE_URL", "GUARDRAIL_LLM_MODEL", "GUARDRAIL_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    config = LLMConfig()

    assert "localhost:11434" in config.base_url
    assert config.model.startswith("qwen3")


def test_thinking_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured: Qwen3's reasoning phase blew past a 300s timeout on a five-tool prompt,
    while /no_think answered correctly in 34s."""
    monkeypatch.delenv("GUARDRAIL_LLM_THINKING", raising=False)

    assert LLMConfig().disable_thinking is True


def test_thinking_can_be_re_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARDRAIL_LLM_THINKING", "1")

    assert LLMConfig().disable_thinking is False


def test_no_think_token_is_appended_to_the_system_message() -> None:
    provider = LLMProvider(LLMConfig(disable_thinking=True))
    original = [
        {"role": "system", "content": "You are an ops assistant."},
        {"role": "user", "content": "delete everything"},
    ]

    prepared = provider._prepare(original)

    assert prepared[0]["content"].endswith("/no_think")
    assert original[0]["content"] == "You are an ops assistant.", "caller's list was mutated"
    provider.close()


def test_a_system_message_is_created_when_none_exists() -> None:
    """Silently skipping the switch would leave thinking on and blow the timeout."""
    provider = LLMProvider(LLMConfig(disable_thinking=True))

    prepared = provider._prepare([{"role": "user", "content": "hello"}])

    assert prepared[0]["role"] == "system"
    assert "/no_think" in prepared[0]["content"]
    provider.close()
