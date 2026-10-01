"""Explicit experiment profiles; never imported by the product.

DeepSeek rates are CNY list prices retrieved 2026-10-02 from
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ . The off-peak
experiment is admitted only outside the published weekday peak hours (UTC).
This conservative window does not assume any holiday discount.
"""
from copy import deepcopy
from datetime import datetime, timezone

from web_operation_agent.modeling.client import PRICES, options_for
from web_operation_agent.modeling.spend import SpendMeter

DEEPSEEK_RATES = {
    'deepseek-flash': {'input_per_m': 1., 'cache_read_per_m': .02,
                       'output_per_m': 4., 'currency': 'CNY'},
    'deepseek-v4-pro': {'input_per_m': 4.5, 'cache_read_per_m': .15,
                        'output_per_m': 13.5, 'currency': 'CNY'},
}


def off_peak(now=None):
    now = now or datetime.now(timezone.utc)
    return now.weekday() >= 5 or not (1 <= now.hour < 4 or 6 <= now.hour < 10)


def register_profile(model, profile):
    if model in DEEPSEEK_RATES:
        PRICES[model] = dict(DEEPSEEK_RATES[model], taken='2026-10-02 official CNY off-peak; evaluation only')
        if profile != 'fast':
            raise ValueError('DeepSeek comparison requires the registered non-thinking profile')
        return {'thinking': {'type': 'disabled'}}
    if profile == 'production':
        return options_for(model)
    if model == 'qwen3.8-flash':
        return {'enable_thinking': False, 'parallel_tool_calls': False}
    raise ValueError('no reviewed evaluation profile for this model')


class EvaluationMeter(SpendMeter):
    def __init__(self, *, model, **kwargs):
        self.evaluation_model = model
        super().__init__(**kwargs)

    def within_allowance(self):
        # Prevent dispatch across a price band rather than misprice a call.
        return (self.evaluation_model not in DEEPSEEK_RATES or off_peak()) and super().within_allowance()

    def record(self, **kwargs):
        if self.evaluation_model in DEEPSEEK_RATES:
            usage = deepcopy(kwargs['usage'])
            # Older DeepSeek responses expose cache hits only at top level.
            # Do not silently discard paid cache hits or overwrite a conflict.
            if 'prompt_cache_hit_tokens' in usage:
                details = usage.setdefault('prompt_tokens_details', {})
                if not isinstance(details, dict):
                    raise ValueError('invalid provider prompt details')
                cached = usage['prompt_cache_hit_tokens']
                if 'cached_tokens' in details and details['cached_tokens'] != cached:
                    raise ValueError('conflicting provider cached token counts')
                details['cached_tokens'] = cached
            kwargs['usage'] = usage
        return super().record(**kwargs)
