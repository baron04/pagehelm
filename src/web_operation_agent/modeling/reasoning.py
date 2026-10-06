"""Server call-site reasoning policy and provider wire translation.

Ordinary calls stay non-thinking. Only the complete operation-contract planner opts
into bounded reasoning; retries never promote a call or raise its reasoning effort.
The private scenario option travels through ChatPort decorators, never to a provider.
"""
from enum import StrEnum
from urllib.parse import urlsplit


class ReasoningScenario(StrEnum):
    ROUTINE = "routine"
    COMPLEX_PLAN = "complex_plan"


_SCENARIO = "_woa_reasoning_scenario"
REASONING_FIELDS = frozenset({_SCENARIO, "enable_thinking", "thinking_budget",
                              "thinking", "reasoning_effort"})


def scenario_options(scenario: ReasoningScenario) -> dict:
    if not isinstance(scenario, ReasoningScenario):
        raise ValueError("reasoning scenario must be selected by the server")
    return {_SCENARIO: scenario.value}


def request_options(model: str, endpoint: str, options: dict) -> dict:
    """Select the mode by scenario; model/endpoint only choose its supported encoding.

    Unknown model families receive no speculative extension fields. Native framework
    requests own their serialization and do not pass through this ordinary-call helper.
    """
    try:
        scenario = ReasoningScenario(options.get(_SCENARIO, ReasoningScenario.ROUTINE))
    except (TypeError, ValueError):
        raise ValueError("unknown server reasoning scenario") from None
    result = {key: value for key, value in options.items() if key not in REASONING_FIELDS}
    enabled = scenario == ReasoningScenario.COMPLEX_PLAN
    if model in {"qwen3.8-flash", "qwen3.8-max"}:
        result["enable_thinking"] = enabled
        if enabled:
            result["thinking_budget"] = 1024
    elif model in {"deepseek-flash", "deepseek-v4.1-flash", "deepseek-v4-flash",
                   "deepseek-v4-pro"}:
        host = urlsplit(endpoint).hostname or ""
        if host == "aliyuncs.com" or host.endswith(".aliyuncs.com"):
            result["enable_thinking"] = enabled
        elif host == "api.deepseek.com":
            result["thinking"] = {"type": "enabled" if enabled else "disabled"}
        else:
            return result
        if enabled:
            result["reasoning_effort"] = "low" if model in {
                "deepseek-v4.1-flash", "deepseek-flash"} else "high"
    return result
