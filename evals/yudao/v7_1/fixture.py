"""Versioned v7 corrections; original tasks, application data and source stay frozen.

Only the synthetic read-refusal protocol and evaluator-only expectations change.
No expected value, rubric or case identifier is returned by application reads.
"""
from copy import deepcopy

from evals.yudao.v7 import fixture as original
from web_operation_agent_yudao.client import YudaoReadRefused

EVALUATION_VERSION = 'v7.1'
RELEASE = original.RELEASE
AS_OF = original.AS_OF
SOURCE = original.SOURCE
MENUS = original.MENUS
DICTS = original.DICTS
PERMISSIONS = original.PERMISSIONS
CUSTOMERS = original.CUSTOMERS
CONTRACTS = original.CONTRACTS
SUPPLIERS = original.SUPPLIERS
FLOWS = original.FLOWS
documents = original.documents
VARIANTS = original.VARIANTS
PAGE_PATHS = original.PAGE_PATHS
REQUIRES_SOURCE = original.REQUIRES_SOURCE
FORBIDDEN = deepcopy(original.FORBIDDEN)
EXPECTED_STATUSES = deepcopy(original.EXPECTED_STATUSES)
# A scoped, verified absence answers the request even if the product uses refused.
EXPECTED_STATUSES['v7-38'] = ['finished', 'refused']
CASES = [(cid, kind, prompt, list(checks[:-1] if cid == 'v7-19' else checks))
         for cid, kind, prompt, checks in original.CASES]


class Client(original.Client):
    def get(self, path, **params):
        try:
            return super().get(path, **params)
        except PermissionError as exc:
            if self.variant == 'restricted_contracts' and path.startswith('crm/contract/'):
                raise YudaoReadRefused('actor cannot query contracts', code=403,
                                      reason='actor cannot query contracts') from exc
            raise
