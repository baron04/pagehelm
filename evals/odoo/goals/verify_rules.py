"""Recheck frozen rule provenance without a model or database writes; print no prompts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evals.odoo.goals.run_goals import TASKSETS, load_tasks
from evals.odoo.goals.source_citations import bound_source


def verify(root: Path, release: str) -> dict:
    doc = load_tasks('v6-rules')
    path, digest = TASKSETS['v6-rules']
    seal = json.loads(path.with_suffix('.seal.json').read_text())
    if seal['task_set_sha256'] != 'sha256:' + digest:
        raise ValueError('rule seal does not match task set')
    source = bound_source(doc, root, release)
    count = 0
    for task in doc['tasks']:
        for location in task['truth']['sources']:
            lines = source.read_lines(location['file'])
            start, end = location['start_line'], location['end_line']
            if start < 1 or end < start or end > len(lines):
                raise ValueError('truth range outside source')
            body = b''.join(lines[start - 1:end])
            if hashlib.sha256(body).hexdigest() != location['sha256']:
                raise ValueError('truth slice hash changed')
            if not all(clause in body.decode('utf-8') for clause in location['clauses']):
                raise ValueError('truth clause is absent')
            count += 1
    return {'tasks_verified': len(doc['tasks']), 'source_locations_verified': count,
            'release': doc['source_binding']['release'], 'model_calls': 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--source-release', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.source_root, args.source_release), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
