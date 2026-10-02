"""Offline subset correction; preserve all earlier graders and original artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
from evals.yudao.v7_1 import fixture
from evals.yudao.v7_1_1.regrade import reassess as parent_reassess
from .judge import grade


def reassess(root):
    result=parent_reassess(root)
    cid,kind,_,checks=fixture.CASES[28]
    row=json.loads((root/cid/'result.json').read_text())
    revised=grade(fixture,cid,kind,checks,answer=row['answer'],status=row['status'],url='',
                  page_actions=row['page_actions'],source_events=row['source_events'],writes=row['writes'],
                  api_reads=row['api_reads'],audit=[])
    parent_passed=cid not in result['failures']
    result['regraded_passed']+=int(revised['passed'])-int(parent_passed)
    result['failures']=[x for x in result['failures'] if x!=cid]
    if not revised['passed']: result['failures'].append(cid)
    result['failures'].sort()
    result['changes']=[x for x in result['changes'] if x['case']!=cid]
    if bool(row['passed'])!=revised['passed']:
        result['changes'].append(dict(case=cid,old_passed=bool(row['passed']),new_passed=revised['passed'],
            status=row['status'],failed_semantic_checks=[k for k,v in revised['semantic_checks'].items() if not v]))
    result['judge_version']='v7.1.2'
    checkout=Path(__file__).resolve().parents[3]
    for name in ('judge.py','regrade.py','manifest.json'):
        relative='evals/yudao/v7_1_2/'+name
        result['grader_binding'][relative]=hashlib.sha256((checkout/relative).read_bytes()).hexdigest()
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=reassess(args.input)
    with args.output.open('x') as out:out.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('cases','old_passed','regraded_passed','changes')},ensure_ascii=False))
