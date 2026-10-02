"""Regrade all frozen v7 results offline, without changing them or calling models.

python -m evals.yudao.v7_1.regrade --input LOCAL_RAW --output NEW_SUMMARY.json
"""
import argparse
import hashlib
import json
from pathlib import Path

from . import fixture
from .judge import grade


def reassess(root):
    hashes, changes, failed, results = {}, [], [], []
    for cid, kind, prompt, checks in fixture.CASES:
        path = root / cid / 'result.json'
        body = path.read_bytes()
        row = json.loads(body)
        if row['case'] != cid or row['prompt'] != prompt or row['kind'] != kind:
            raise ValueError('historical case binding mismatch: ' + cid)
        hashes[cid + '/result.json'] = hashlib.sha256(body).hexdigest()
        audit_path = path.with_name('audit.jsonl')
        audit = []
        if audit_path.exists():
            audit_body = audit_path.read_bytes()
            hashes[cid + '/audit.jsonl'] = hashlib.sha256(audit_body).hexdigest()
            audit = [json.loads(line) for line in audit_body.splitlines()]
        observations = [e for e in audit if e.get('event') in ('goal_page_settled', 'observation')]
        # Never manufacture a page URL from an expected route.
        url = row.get('url', observations[-1].get('url', '') if observations else '')
        revised = grade(fixture, cid, kind, checks, answer=row['answer'], status=row['status'],
                        url=url, page_actions=row['page_actions'], source_events=row['source_events'],
                        writes=row['writes'], api_reads=row['api_reads'], audit=audit)
        old, new = bool(row['passed']), revised['passed']
        reasons = [key for key, value in revised['semantic_checks'].items() if not value]
        result = {'case': cid, 'old_passed': old, 'new_passed': new,
                  'failed_semantic_checks': reasons, 'status': row['status']}
        results.append(result)
        if old != new:
            changes.append(result)
        if not new:
            failed.append(cid)
    files = ['evals/yudao/qualification/judge.py', 'evals/yudao/v7/fixture.py',
             'evals/yudao/v7/rubric.json', 'evals/yudao/v7_1/fixture.py',
             'evals/yudao/v7_1/judge.py', 'evals/yudao/v7_1/regrade.py']
    checkout = Path(__file__).resolve().parents[3]
    binding = {name: hashlib.sha256((checkout / name).read_bytes()).hexdigest() for name in files}
    return {'judge_version': 'v7.1', 'cases': len(results),
            'old_passed': sum(r['old_passed'] for r in results),
            'regraded_passed': sum(r['new_passed'] for r in results),
            'changes': changes, 'failures': failed,
            'harness_errors': [r['case'] for r in results if r['status'] == 'harness_error'],
            'model_calls': 0, 'cost': 0,
            'limitations': 'Offline deterministic screening; unchanged answers, not a product improvement or full semantic audit. Original harness errors remain failures.',
            'grader_binding': binding,
            'historical_artifacts_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = reassess(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as out:
        out.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('cases', 'old_passed', 'regraded_passed', 'changes', 'harness_errors', 'model_calls')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
