"""Evaluator-only v7.1 corrections, with explicit semantic checks and limitations.

This is deterministic screening, not a general natural-language semantic judge.
The original judge and grades are retained; no oracle enters the product context.
"""
import json
import re
from decimal import Decimal
from pathlib import Path

from evals.yudao.qualification.judge import CITATION, grade as original_grade
from . import fixture

VERSION = 'v7.1'
_RUBRIC = json.loads((Path(__file__).parents[1] / 'v7' / 'cases.json').read_text())
_CASES = {case['id']: case for case in _RUBRIC['questions']}
_ROWS = {'contract': fixture.CONTRACTS, 'customer': fixture.CUSTOMERS,
         'supplier': fixture.SUPPLIERS, 'flow': fixture.FLOWS}
_TRUNCATED = re.compile(r'仅列|只列|只显示|仅显示|前\s*\d+\s*(?:条|个|份)|first\s+\d+', re.I)
_MAXIMUM = re.compile(
    r'(?:最高|最大|maximum|\bmax\b)[^\n。；;]{0,40}?'
    r'(?<![\d.])(?P<value>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>元|分|CNY|RMB)', re.I)


def named_records(prose, rows):
    """Longest exact names first: a branch name must not also match its parent."""
    names = sorted({row['name'] for row in rows}, key=lambda name: (-len(name), name))
    if not names:
        return set()
    pattern = re.compile('|'.join(re.escape(name) for name in names))
    return {match.group() for match in pattern.finditer(prose)}


def _list_rule(cid):
    case = _CASES[cid]
    scope = case.get('oracle_scope', {})
    if scope.get('mode') in ('list', 'both'):
        return scope['entity'], case['expected_record_ids']
    if cid == 'v7-36':
        rows = fixture.CONTRACTS
        maximum = max(row['totalPrice'] for row in rows if row['totalPrice'] is not None)
        return 'contract', [row['id'] for row in rows if row['totalPrice'] == maximum]
    if cid == 'v7-64':
        return 'supplier', [row['id'] for row in fixture.SUPPLIERS if row['status'] == 1]
    return None


def grade(f, cid, kind, checks, *, answer, status, url, page_actions,
          source_events, writes, api_reads=(), audit=()):
    if getattr(f, 'EVALUATION_VERSION', '') != VERSION:
        raise ValueError('v7.1 grading requires the versioned fixture')
    result = original_grade(f, cid, kind, checks, answer=answer, status=status,
                            url=url, page_actions=page_actions,
                            source_events=source_events, writes=writes)
    prose = CITATION.sub('', answer)
    semantic = {}
    rule = _list_rule(cid)
    if rule:
        entity, ids = rule
        rows = _ROWS[entity]
        expected = {row['name'] for row in rows if row['id'] in ids}
        semantic['exact_record_names'] = named_records(prose, rows) == expected
        semantic['complete_list'] = not bool(_TRUNCATED.search(prose))
        listed = re.findall(r'^\s*(?:\d+[.)、]|[-*])\s+(.+)$', prose, re.M)
        semantic['no_unknown_listed_record'] = all(named_records(line, rows) for line in listed)
    if cid == 'v7-36':
        expected = Decimal(max(row['totalPrice'] for row in fixture.CONTRACTS
                               if row['totalPrice'] is not None)) / 100
        amounts = [(Decimal(m['value'].replace(',', '').replace('，', ''))
                    / (100 if m['unit'] == '分' else 1))
                   for m in _MAXIMUM.finditer(prose)]
        semantic['maximum_with_unit'] = bool(amounts) and all(x == expected for x in amounts)
        semantic['not_frequency'] = not bool(re.search(r'最多的是|最常见|most frequent', prose, re.I))
    if cid == 'v7-38':
        semantic['absence_not_inability'] = bool(re.search(
            r'不存在|没有(?:找到|匹配|符合|这)|未找到|无匹配|not found|no matching|'
            r'(?<![\d.])(?:0|零)\s*(?:条|份|笔|records?)', prose, re.I))
        semantic['not_permission_refusal'] = not bool(re.search(
            r'没有权限|无权限|无法查询|不能查询|不支持|not authorized|cannot query', prose, re.I))
        semantic['read_performed'] = 'crm/contract/page' in api_reads
        semantic['no_navigation'] = page_actions == 0
    if cid in f.PAGE_PATHS:
        expected_url = 'http://localhost' + f.PAGE_PATHS[cid]
        settled = [event for event in audit if event.get('event') == 'goal_page_settled']
        semantic['rendered_page_receipt'] = bool(settled) and all(
            event.get('reached') is True and not event.get('unmet')
            and event.get('url') == expected_url
            and event.get('render_state') == 'ready'
            and event.get('active_view') == ('detail' if '/detail/' in expected_url else 'list')
            and ('/detail/' not in expected_url
                 or event.get('record_route') == f.PAGE_PATHS[cid]) for event in settled)
    result['passed'] = bool(result['passed'] and all(semantic.values()))
    result['semantic_checks'] = semantic
    result['judge_version'] = VERSION
    return result
