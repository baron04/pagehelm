"""Provider canary contract for P7.4b preflight, qualified here with scripted responses.

Before any paid development batch, the same fixed canary is sent five times through the
evaluation provider path, under the frozen request contract every arm's request must satisfy:
the same temperature, output cap and model options. Three things must hold on all five: the
response has the contracted OpenAI-compatible schema, the returned model string is identical,
and a fixed semantic extraction of the answer to a question with one correct result succeeds.
Free text is never hashed, since two correct responses rarely match byte for byte. Passing shows
only that this provider, alias and date window looked stable at preflight; an alias is not a
snapshot.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gateway import GatewayBudgets

CANARY_PROMPT = ("Answer with digits only: what is 17 multiplied by 23? "
                 "Reply with the number and nothing else.")
CANARY_ANSWER = 391
REQUIRED_CALLS = 5


def canary_request(budgets: GatewayBudgets) -> dict[str, Any]:
    """The fixed canary with the frozen temperature, output cap and model options of every arm."""
    request = {"model": budgets.requested_model, "temperature": budgets.temperature,
               "max_tokens": budgets.max_output_tokens, **budgets.model_options,
               "messages": [{"role": "user", "content": CANARY_PROMPT}]}
    if budgets.request_refusal(request) is not None:
        raise ValueError("the canary does not satisfy the frozen request contract")
    return request


def response_schema(response: Any) -> tuple[str, ...]:
    """The contracted structural shape of a completion, independent of its text."""
    if not isinstance(response, dict):
        return ("not_an_object",)
    choices = response.get("choices")
    message = (choices[0].get("message") if isinstance(choices, list) and choices
               and isinstance(choices[0], dict) else None)
    usage = response.get("usage")
    return (
        "model:str" if isinstance(response.get("model"), str) else "model:missing",
        "choices:list" if isinstance(choices, list) and len(choices) >= 1 else "choices:bad",
        "message.content:str" if isinstance(message, dict)
        and isinstance(message.get("content"), str) else "message.content:missing",
        "usage.prompt_tokens:int" if isinstance(usage, dict)
        and isinstance(usage.get("prompt_tokens"), int) else "usage.prompt_tokens:missing",
        "usage.completion_tokens:int" if isinstance(usage, dict)
        and isinstance(usage.get("completion_tokens"), int) else "usage.completion_tokens:missing",
    )


EXPECTED_SCHEMA = ("model:str", "choices:list", "message.content:str",
                   "usage.prompt_tokens:int", "usage.completion_tokens:int")


def semantic_answer(response: dict[str, Any]) -> int | None:
    """The one integer in the answer text, or None; reasoning wrappers are stripped first."""
    try:
        text = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    text = re.sub(r"<think>.*?</think>", " ", str(text), flags=re.S)
    numbers = re.findall(r"(?<![\d.])\d{1,6}(?![\d.])", text.replace(",", ""))
    return int(numbers[0]) if len(numbers) == 1 else None


def evaluate(responses: list[Any]) -> dict[str, Any]:
    schemas = [response_schema(item) for item in responses]
    models = [item.get("model") if isinstance(item, dict) else None for item in responses]
    answers = [semantic_answer(item) if isinstance(item, dict) else None for item in responses]
    checks = {
        "five_calls": len(responses) == REQUIRED_CALLS,
        "schema_5_of_5": all(schema == EXPECTED_SCHEMA for schema in schemas),
        "model_string_5_of_5": len(set(models)) == 1 and isinstance(models[0], str),
        "semantic_answer_5_of_5": all(answer == CANARY_ANSWER for answer in answers),
    }
    return {"kind": "p74_provider_canary", "checks": checks,
            "model_strings": sorted({str(model) for model in models}),
            "status": "READY" if all(checks.values()) else "FAILED",
            "limitation": "a stable alias at preflight does not prove unchanged weights"}


class PreflightStopped(Exception):
    """``send`` could not account for a call, so nothing more may be sent."""


def run_preflight(send: Callable[[dict[str, Any]], dict[str, Any]], *,
                  budgets: GatewayBudgets) -> dict[str, Any]:
    """Send the canary five times through ``send`` (the provider path) and evaluate.

    A failed call fails the preflight and is never retried. A call that could not be accounted
    stops it at once, and the remaining canaries are not sent.
    """
    responses = []
    stopped = None
    for _ in range(REQUIRED_CALLS):
        try:
            responses.append(send(canary_request(budgets)))
        except PreflightStopped as exc:
            stopped = str(exc) or "stopped"
            break
        except Exception as exc:
            responses.append({"error": type(exc).__name__})
    return {**evaluate(responses), "stopped": stopped}
