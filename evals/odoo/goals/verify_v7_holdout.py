"""Zero-model oracle check for a v7 task file against the isolated reference fixture."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from evals.odoo.goals.binding import fixture_signature
from evals.odoo.goals.judge import _check, score
from evals.odoo.goals.run_goals import ROOT, load_tasks
from evals.odoo.goals.source_citations import bound_source
from evals.odoo.goals.verify_v6_holdout import _installed_modules, _read_pages, _url_for
from web_operation_agent_odoo.server.executor import OdooBusinessToolExecutor
from web_operation_agent_odoo.server.identity import OdooApiKeyStore


def _actor_data(task: dict, api: OdooBusinessToolExecutor) -> None:
    oracle = task['truth']['oracle']
    rows = api.call(oracle['model'], 'search_read', [oracle['domain']],
                    {'fields':[oracle['field']], 'limit':200, 'order':'id'})
    if oracle.get('operation') == 'count':
        actual = len(rows)
    elif oracle.get('operation') == 'sum':
        actual = round(sum(float(row[oracle['field']] or 0) for row in rows),2)
    elif task['delivery'] == 'answer/list':
        actual = sorted(row[oracle['field']] for row in rows if row[oracle['field']])
    else:
        if len(rows) != 1:
            raise AssertionError(f"{task['id']}: record not unique")
        actual = rows[0][oracle['field']]
    expected = sorted(oracle['expected']) if isinstance(actual,list) and task['delivery']=='answer/list' else oracle['expected']
    if actual != expected:
        raise AssertionError(f"{task['id']}: actor data differs: {actual!r} != {expected!r}")


def _absence(task: dict, api: OdooBusinessToolExecutor, installed: set[str]) -> None:
    truth = task['truth']
    if 'clarification' in truth:
        spec = truth['clarification']
        rows = api.call(spec['model'],'search_read',[spec['domain']],
                        {'fields':['name'],'limit':200,'order':'id'})
        available = {row['name'] for row in rows}
        if len(available) < 2 or not set(spec['candidate_names']) <= available:
            raise AssertionError(f"{task['id']}: ambiguity candidates differ from actor data")
        return
    spec = truth['absence']
    kind = spec['kind']
    if kind == 'missing_record':
        if api.call(spec['model'],'search_count',[spec['domain']]):
            raise AssertionError(f"{task['id']}: supposedly absent record exists")
    elif kind == 'missing_field':
        fields=api.call(spec['model'],'fields_get',[],{'attributes':['type']})
        if spec['field'] in fields:
            raise AssertionError(f"{task['id']}: supposedly absent field exists")
    elif kind == 'no_permission':
        if api.call(spec['model'],'check_access_rights',[spec.get('operation','read')],
                    {'raise_exception':False}):
            raise AssertionError(f"{task['id']}: actor has forbidden access")
    elif kind == 'unsupported_view':
        catalog=api.call('woa.runtime.descriptor','catalog',[],{})
        if any(a.get('model')==spec['model'] and 'map' in a.get('view_modes',())
               for a in catalog['actions'] if a.get('readable')):
            raise AssertionError(f"{task['id']}: map view exists")
    elif kind == 'external_integration':
        if {'delivery','stock'} & installed:
            raise AssertionError(f"{task['id']}: delivery integration installed")
    else:
        raise AssertionError(f"{task['id']}: unsupported absence witness {kind}")


def _sample(task: dict, *, source=None) -> None:
    truth=task['truth']
    if 'oracle' in truth:
        yes, no = truth['oracle']['positive'], truth['oracle']['wrong']
    elif 'sources' in truth:
        yes, no = truth['positive'], truth['wrong']
        citations=[]
        for loc in truth['sources']:
            yes += '\n'+b''.join(source.read_lines(loc['file'])[
                loc['start_line']-1:loc['end_line']]).decode()
            citations.append(f"{loc['file']}:{loc['start_line']}-{loc['end_line']}@"
                             f"{truth['release']} sha256:{loc['sha256']}")
        yes += '\n'+'\n'.join(citations)
        no += '\n'+'\n'.join(citations)
    elif 'clarification' in truth:
        yes='请确认是哪一张：'+'、'.join(truth['clarification']['candidate_names'])+'？'
        no='请确认 S99999？'
    elif 'absence' in truth:
        terms=task['parts'][0]['check'].get('explain_keywords',[])
        yes='没有权限或没有记录：'+'、'.join(terms)
        no='已完成。'
    elif 'page' in truth:
        page=truth['page']
        probe={'path':_url_for(page['path']),'breadcrumb_last':page['breadcrumb'],
               'highlighted_controls':[page['highlight'].strip('^$')]
               if page['highlight'] else []}
        good=score(task,probe=probe,visited=[],answer='已打开并标出目标。',
                   finished=True,write_detected=False)
        bad=score(task,probe={'path':'/odoo/wrong','breadcrumb_last':'Wrong',
                              'highlighted_controls':[]},visited=[],
                  answer='已打开并标出目标。',finished=True,write_detected=False)
        if good.outcome!='correct' or bad.outcome!='false_claim':
            raise AssertionError(f"{task['id']}: page oracle sample fails")
        return
    else:
        return
    good=score(task,probe={},visited=[],answer=yes,finished=True,write_detected=False,source=source)
    bad=score(task,probe={},visited=[],answer=no,finished=True,write_detected=False,source=source)
    if not good.success or bad.success:
        raise AssertionError(f"{task['id']}: oracle answer fails or obvious error passes: {good.outcome}/{bad.outcome}")


def verify(doc: dict, *, source_root: Path, source_release: str,
           base_url: str, credentials: Path, db_container: str, database: str,
           pages: bool = True) -> dict:
    before=fixture_signature(db_container,database)
    api=OdooBusinessToolExecutor(base_url=base_url,
        keys=OdooApiKeyStore(credentials/'api-key.json'),allow_private_http=True)
    source=bound_source(doc,source_root,source_release)
    installed=_installed_modules(db_container,database)
    counts=Counter()
    for task in doc['tasks']:
        if task.get('rubric_version') != 7 or len(task['parts']) != 1:
            raise AssertionError(f"{task['id']}: rubric/part mismatch")
        for key,value in task['parts'][0]['check'].items():
            if key.endswith('_regex'):
                re.compile(value)
        truth=task['truth']
        if 'oracle' in truth:
            _actor_data(task,api);_sample(task);counts['actor_data']+=1
        elif 'sources' in truth:
            for loc in truth['sources']:
                lines=source.read_lines(loc['file'])
                body=b''.join(lines[loc['start_line']-1:loc['end_line']])
                if hashlib.sha256(body).hexdigest()!=loc['sha256']:
                    raise AssertionError(f"{task['id']}: source digest differs")
                if not all(clause in body.decode() for clause in loc['clauses']):
                    raise AssertionError(f"{task['id']}: source clause absent")
                counts['source_locations']+=1
            _sample(task,source=source);counts['source_rules']+=1
        elif 'absence' in truth or 'clarification' in truth:
            _absence(task,api,installed);_sample(task);counts['limitations']+=1
        elif 'page' in truth:
            _sample(task);counts['page_targets']+=1
        else:
            raise AssertionError(f"{task['id']}: no oracle")
    if pages:
        count=asyncio.run(_read_pages(doc,base_url,credentials/'storage-state.json'))
        if count!=counts['page_targets']:
            raise AssertionError('page walk incomplete')
        counts['actor_pages']=count
    after=fixture_signature(db_container,database)
    if after!=before:
        raise AssertionError('fixture changed during oracle check')
    return {'tasks':len(doc['tasks']),'groups':dict(Counter(t['group'] for t in doc['tasks'])),
            'verified':dict(counts),'model_calls':0,'fixture_signature':before}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task-file',type=Path)
    p.add_argument('--source-root',type=Path,default=ROOT/'.fixture/odoo-source')
    p.add_argument('--base-url',default='http://127.0.0.1:8069')
    p.add_argument('--credentials',type=Path,default=ROOT/'.private/odoo-reader')
    p.add_argument('--db-container',default='woa-odoo-reference-db-1')
    p.add_argument('--database',default='woa_reference')
    p.add_argument('--skip-pages',action='store_true')
    args=p.parse_args()
    doc=json.loads(args.task_file.read_text()) if args.task_file else load_tasks('v7a')
    print(json.dumps(verify(doc,source_root=args.source_root,
        source_release=doc['source_binding']['deployment_release'],base_url=args.base_url,
        credentials=args.credentials,db_container=args.db_container,database=args.database,
        pages=not args.skip_pages),ensure_ascii=False,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
