"""Zero-model qualification for the frozen v6 goal set; print counts, never prompts.

Read all data through the delegated salesperson API, inspect the same browser session,
verify source slices byte-for-byte and check absent capabilities. No model or write calls.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

from evals.odoo.goals.binding import fixture_signature
from evals.odoo.goals.judge import _check
from evals.odoo.goals.probe import FINAL_STATE_PROBE
from evals.odoo.goals.run_goals import ROOT, load_tasks
from evals.odoo.goals.source_citations import bound_source
from web_operation_agent_odoo.server.executor import OdooBusinessToolExecutor
from web_operation_agent_odoo.server.identity import OdooApiKeyStore
from web_operation_agent_odoo.sourcemap import OdooCompiler, odoo_source_tree


def _check_regexes(doc: dict) -> int:
    count = 0
    for task in doc['tasks']:
        for part in task['parts']:
            for key, pattern in part['check'].items():
                if key.endswith('_regex'):
                    re.compile(pattern)
                    if any(ord(char) < 32 or ord(char) == 127 for char in pattern):
                        raise AssertionError(f'{task["id"]}: control character in {key}')
                    count += 1
    return count


def _read_data(doc: dict, api: OdooBusinessToolExecutor) -> int:
    verified = 0
    for task in doc['tasks']:
        oracle = task.get('truth', {}).get('oracle')
        if oracle is None:
            continue
        model, domain, field = oracle['model'], oracle['domain'], oracle['field']
        rows = api.call(model, 'search_read', [domain], {'fields':[field], 'limit':200, 'order':'id'})
        if oracle.get('operation') == 'count':
            actual = len(rows)
        elif oracle.get('operation') == 'sum':
            actual = round(sum(float(row[field] or 0) for row in rows), 2)
        elif task['delivery'] == 'answer/list':
            actual = [row[field] for row in rows if row[field]]
            actual.sort()
            expected = sorted(oracle['expected'])
            if actual != expected:
                raise AssertionError(f'{task["id"]}: list truth differs from actor API')
        else:
            if len(rows) != 1:
                raise AssertionError(f'{task["id"]}: source row not unique')
            actual = rows[0][field]
        if task['delivery'] != 'answer/list' and actual != oracle['expected']:
            raise AssertionError(f'{task["id"]}: scalar truth differs from actor API')
        pattern = task['parts'][0]['check']['answer_regex']
        if not re.search(pattern, oracle['positive']) or re.search(pattern, oracle['wrong']):
            raise AssertionError(f'{task["id"]}: answer regex does not separate truth from error')
        verified += 1
    return verified


def _citation(location: dict, release: str) -> str:
    return (f"{location['file']}:{location['start_line']}-{location['end_line']}@"
            f"{release} sha256:{location['sha256']}")


def _read_sources(doc: dict, root: Path, release: str) -> int:
    source = bound_source(doc, root, release)
    assert source is not None
    verified = 0
    for task in doc['tasks']:
        truth = task.get('truth', {})
        locations = truth.get('sources', [])
        for loc in locations:
            lines = source.read_lines(loc['file'])
            start, end = loc['start_line'], loc['end_line']
            if not 1 <= start <= end <= len(lines):
                raise AssertionError(f'{task["id"]}: source range')
            body = b''.join(lines[start - 1:end])
            if hashlib.sha256(body).hexdigest() != loc['sha256']:
                raise AssertionError(f'{task["id"]}: source bytes differ')
            if not all(clause in body.decode() for clause in loc['clauses']):
                raise AssertionError(f'{task["id"]}: clause absent from source')
            verified += 1
        for part in task['parts']:
            check = part['check']
            if 'source_regex' not in check:
                continue
            evidence = ' '.join(
                b''.join(source.read_lines(loc['file'])[loc['start_line'] - 1:loc['end_line']]).decode()
                for loc in locations)
            citation = '\n'.join(_citation(loc, doc['source_binding']['release']) for loc in locations)
            if not re.search(check['answer_regex'], evidence):
                raise AssertionError(f'{task["id"]}: source answer sample fails')
            if not _check({'source_regex':check['source_regex']}, {}, [], evidence + '\n' + citation, source=source):
                raise AssertionError(f'{task["id"]}: verified citation fails')
            if _check({'source_regex':check['source_regex']}, {}, [], evidence, source=source):
                raise AssertionError(f'{task["id"]}: uncited answer passes')
    return verified


def _installed_modules(container: str, database: str) -> set[str]:
    result = subprocess.run(['docker','exec',container,'psql','-U','odoo','-d',database,
                             '-Atc',"select name from ir_module_module where state='installed'"],
                            capture_output=True,text=True,timeout=30,check=True)
    return set(result.stdout.splitlines())


def _compiled_source_coverage(doc: dict, root: Path, release: str,
                              installed: set[str]) -> int:
    compiled = OdooCompiler(odoo_source_tree(root.resolve(), release=release),
                            'v6-zero-model', modules=installed).compile()
    refs = [evidence for relation in compiled.relations for evidence in relation.evidence]
    source = bound_source(doc, root, release)
    assert source is not None
    covered = 0
    for task in doc['tasks']:
        checks = [p['check'] for p in task['parts'] if 'source_regex' in p['check']]
        if not checks:
            continue
        citations = []
        for location in task['truth']['sources']:
            match = next(iter(sorted((ref for ref in refs if ref.path == location['file']
                                      and ref.start_line <= location['start_line']
                                      and ref.end_line >= location['end_line']
                                      and ref.end_line - ref.start_line < 512),
                                     key=lambda ref: ref.end_line - ref.start_line)), None)
            if match is None:
                raise AssertionError(f'{task["id"]}: compiled map lacks source rule')
            citations.append(f'{match.path}:{match.start_line}-{match.end_line}@{release} '
                             f'sha256:{match.sha256}')
            covered += 1
        answer = '\n'.join(citations)
        if not all(_check({'source_regex':check['source_regex']}, {}, [], answer,
                          source=source) for check in checks):
            raise AssertionError(f'{task["id"]}: compiled citations fail source regex')
    return covered


def _check_absence(doc: dict, api: OdooBusinessToolExecutor, root: Path,
                   installed: set[str]) -> int:
    count = 0
    for task in doc['tasks']:
        absence = task.get('truth', {}).get('absence')
        if absence is None and task['condition'] != 'absent_rule':
            continue
        if task['condition'] == 'absent_rule':
            # Broad lexical search plus the entrypoint slices verified above. This is
            # scoped to installed modules, not external company policy.
            paths = [root / 'addons' / name for name in installed
                     if (root / 'addons' / name).is_dir()]
            for term in task['truth']['searched_terms']:
                result = subprocess.run(['rg','-i','-l','-g','*.py','-g','*.xml','-g','*.js',
                                         term,*map(str,paths)],capture_output=True,text=True,
                                        timeout=60,check=False)
                if result.returncode == 0:
                    raise AssertionError(f'{task["id"]}: supposed absent policy found')
                if result.returncode != 1:
                    raise AssertionError(f'{task["id"]}: installed source search failed')
        elif absence['kind'] == 'missing_record':
            if api.call(absence['model'], 'search_count', [absence['domain']]):
                raise AssertionError(f'{task["id"]}: supposedly missing record exists')
        elif absence['kind'] == 'missing_field':
            fields = api.call(absence['model'], 'fields_get', [], {'attributes':['type']})
            if absence['field'] in fields:
                raise AssertionError(f'{task["id"]}: supposedly missing field exists')
        elif absence['kind'] == 'unsupported_view':
            catalog = api.call('woa.runtime.descriptor', 'catalog', [], {})
            rows = [action for action in catalog['actions']
                    if action.get('model') == absence['model'] and action.get('readable')]
            if any(absence['view'] in action.get('view_modes', ()) for action in rows):
                raise AssertionError(f'{task["id"]}: supposedly unsupported view exists')
        elif absence['kind'] == 'external_integration':
            if {'delivery','stock'} & installed:
                raise AssertionError(f'{task["id"]}: external integration installed')
        else:
            raise AssertionError(f'{task["id"]}: unknown absence type')
        count += 1
    return count


def _url_for(pattern: str) -> str:
    result = re.sub(r'\(\?:([^)]*)\)', lambda m: m.group(1).split('|')[0], pattern.strip('^$'))
    return result.replace(r'\.', '.')


async def _read_pages(doc: dict, base_url: str, storage_state: Path) -> int:
    from playwright.async_api import async_playwright
    verified = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(storage_state))
        for task in doc['tasks']:
            target = task.get('truth', {}).get('page')
            if target is None:
                continue
            page = await context.new_page()
            try:
                await page.goto(base_url + _url_for(target['path']), wait_until='domcontentloaded')
                await page.wait_for_timeout(450)
                if target['tab']:
                    await page.get_by_role('tab', name=target['tab'], exact=True).click(timeout=5000)
                if target['facet']:
                    if task['id'] == 'aopen05':
                        await page.locator('.o_searchview_facet').filter(has_text='My Quotations').locator('.o_facet_remove').click(timeout=5000)
                    await page.locator('.o_searchview_dropdown_toggler').click(timeout=5000)
                    await page.locator('.o-dropdown--menu:visible').get_by_text(target['facet'], exact=True).first.click(timeout=5000)
                    await page.wait_for_timeout(350)
                if target['highlight']:
                    label = target['highlight'].strip('^$')
                    if task['delivery'] == 'handoff/button':
                        control = page.get_by_role('button', name=label, exact=True).first
                    else:
                        identifier = {'Sales Team':'team_id_',
                                      'Salesperson':'user_id_'}.get(label)
                        control = (page.locator(f'input[id^="{identifier}"]').first if identifier
                                   else page.get_by_role('textbox', name=label, exact=False).first)
                    await control.wait_for(state='visible', timeout=5000)
                    if not await control.is_visible() or not await control.is_enabled():
                        raise AssertionError(f'{task["id"]}: control not available to actor')
                    if target['fill']:
                        await control.evaluate('(e, v) => {e.value = v}', target['fill'])
                    outline = control
                    if target['fill']:
                        field_id = await control.get_attribute('id')
                        if field_id:
                            label_node = page.locator(f'label[for="{field_id}"]:visible').first
                            if await label_node.count():
                                outline = label_node
                    await outline.evaluate('(e) => e.style.setProperty("outline", "4px solid highlight", "important")')
                probe = await page.evaluate(FINAL_STATE_PROBE)
                if not _check(task['parts'][0]['check'], probe, [], ''):
                    raise AssertionError(f'{task["id"]}: target state fails judge')
                verified += 1
            finally:
                await page.close()
        await browser.close()
    return verified


def verify(*, source_root: Path, source_release: str, base_url: str,
           credentials: Path, db_container: str, database: str) -> dict:
    doc = load_tasks('v6')
    before = fixture_signature(db_container, database)
    api = OdooBusinessToolExecutor(base_url=base_url,
       keys=OdooApiKeyStore(credentials / 'api-key.json'), allow_private_http=True)
    regexes = _check_regexes(doc)
    data = _read_data(doc, api)
    sources = _read_sources(doc, source_root, source_release)
    installed = _installed_modules(db_container, database)
    compiled = _compiled_source_coverage(doc, source_root, source_release, installed)
    absent = _check_absence(doc, api, source_root, installed)
    pages = asyncio.run(_read_pages(doc, base_url, credentials / 'storage-state.json'))
    if fixture_signature(db_container, database) != before:
        raise AssertionError('database changed during zero-model qualification')
    return {'tasks':len(doc['tasks']), 'groups':dict(Counter(t['group'] for t in doc['tasks'])),
            'categories':dict(Counter(t['category'] for t in doc['tasks'])),
            'regexes_verified':regexes,'actor_data_verified':data,
            'source_locations_verified':sources,'compiled_source_covered':compiled,
            'absence_verified':absent,
            'actor_pages_verified':pages,'model_calls':0,
            'fixture_signature':before,'task_set_sha256':hashlib.sha256(
                (ROOT/'evals/odoo/tasksets/odoo-goals-zh-v6-holdout.json').read_bytes()).hexdigest(),
            'source_release':source_release}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=ROOT/'.fixture/odoo-source')
    p.add_argument('--source-release',default=None)
    p.add_argument('--base-url',default='http://127.0.0.1:8069')
    p.add_argument('--credentials',type=Path,default=ROOT/'.private/odoo-reader')
    p.add_argument('--db-container',default='woa-odoo-reference-db-1')
    p.add_argument('--database',default='woa_reference')
    p.add_argument('--record',type=Path)
    args=p.parse_args()
    doc=load_tasks('v6')
    report=verify(source_root=args.source_root,
                  source_release=args.source_release or doc['source_binding']['deployment_release'],
                  base_url=args.base_url,credentials=args.credentials,
                  db_container=args.db_container,database=args.database)
    if args.record:
        args.record.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    print(json.dumps({key:value for key,value in report.items()
                      if key not in ('fixture_signature','task_set_sha256','source_release')},
                     sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
