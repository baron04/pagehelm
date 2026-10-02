"""Offline v7.1.1 supplement; the only additional rule is task53 permission copy."""
import argparse
import hashlib
import json
from pathlib import Path

from evals.yudao.v7_1 import fixture
from evals.yudao.v7_1.regrade import reassess as parent_reassess
from .judge import grade


def reassess(root):
    result = parent_reassess(root)
    row = json.loads((root/'v7-53/result.json').read_text())
    cid, kind, _, checks = fixture.CASES[52]
    revised = grade(fixture, cid, kind, checks, answer=row['answer'], status=row['status'],
                    url='', page_actions=row['page_actions'], source_events=row['source_events'],
                    writes=row['writes'], api_reads=row['api_reads'], audit=[])
    parent_passed = cid not in result['failures']
    result['regraded_passed'] += int(revised['passed']) - int(parent_passed)
    if not revised['passed'] and cid not in result['failures']:
        result['failures'].append(cid)
        result['failures'].sort()
    elif revised['passed'] and cid in result['failures']:
        result['failures'].remove(cid)
    result['changes'] = [change for change in result['changes'] if change['case'] != cid]
    if bool(row['passed']) != revised['passed']:
        result['changes'].append({'case': cid, 'old_passed': bool(row['passed']),
            'new_passed': revised['passed'], 'status': row['status'],
            'failed_semantic_checks': [k for k, v in revised['semantic_checks'].items() if not v]})
    result['judge_version'] = 'v7.1.1'
    checkout = Path(__file__).resolve().parents[3]
    for name in ('judge.py', 'regrade.py', 'manifest.json'):
        relative = 'evals/yudao/v7_1_1/' + name
        result['grader_binding'][relative] = hashlib.sha256((checkout/relative).read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = reassess(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as out:
        out.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('cases','old_passed','regraded_passed','changes')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
