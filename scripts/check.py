#!/usr/bin/env python3
"""Select local/CI checks from changed paths; full qualification is explicit."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CORE = 'src/web_operation_agent/'
RUNNER = 'src/web_operation_agent/tests/run_all.py'
FLAGS = ('needs_python_packages', 'needs_node', 'needs_frontend', 'needs_page_controller',
         'needs_python_browser', 'needs_node_browser', 'needs_nginx')
GROUPS = {
    'answer': ('answer', 'execution', 'goal'),
    'business': ('business', 'execution', 'goal'),
    'composition': ('composition', 'execution', 'transport'),
    'execution': ('execution',),
    'goal': ('goal', 'execution'),
    'modeling': ('modeling', 'execution'),
    'planning': ('planning', 'goal', 'execution'),
    'schemas': ('schemas', 'goal', 'execution'),
    'skills': ('planning', 'goal', 'execution'),
    'state': ('state', 'execution', 'transport'),
    'transport': ('transport', 'execution', 'composition'),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(ROOT), *args], stderr=subprocess.DEVNULL)


def changed_paths(base: str | None, head: str | None) -> tuple[list[str], str]:
    if base is not None:
        try:
            first = git('rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()
            last = git('rev-parse', '--verify', '--end-of-options', (head or 'HEAD') + '^{commit}').decode().strip()
            common = git('merge-base', first, last).decode().strip()
            raw = git('diff', '--no-renames', '--name-only', '-z', common, last, '--')
            if head is None:
                raw += git('diff', '--no-renames', '--name-only', '-z', 'HEAD', '--')
                raw += git('ls-files', '--others', '--exclude-standard', '-z')
            return sorted(set(os.fsdecode(p) for p in raw.split(b'\0') if p)), 'git comparison'
        except subprocess.CalledProcessError:
            return ['__all_product__'], 'base unavailable; conservative product checks'
    raw = git('diff', '--no-renames', '--name-only', '-z', 'HEAD', '--')
    raw += git('ls-files', '--others', '--exclude-standard', '-z')
    return sorted(set(os.fsdecode(p) for p in raw.split(b'\0') if p)), 'working tree including staged/untracked files'


def make_plan(paths: list[str], profile: str = 'dev', reason: str = 'explicit paths') -> dict:
    p = set(paths)
    plan = {'profile': profile, 'reason': reason, 'paths': sorted(p), 'checks': [],
            **dict.fromkeys(FLAGS, False)}
    checks = plan['checks']

    def add(name, argv, *, cwd='.', **flags):
        if not any(row['name'] == name for row in checks):
            checks.append({'name': name, 'argv': argv, 'cwd': cwd})
        for key, value in flags.items():
            plan[key] |= value

    def assets():
        if (profile == 'dev' and not builders and not broad and not any(f.startswith('frontend/') for f in p)
                and all((ROOT / 'src/web_operation_agent/web_runtime' / name).is_file()
                        for name in ('assistant.js', 'trace_replay.html'))):
            return
        add('runtime assets', ['npm', 'run', 'build'], cwd='frontend', needs_frontend=True)
        add('sync runtime assets', ['@python', 'scripts/build_runtime.py', '--skip-build'])
        add('sync trace assets', ['@python', 'scripts/build_panel.py', '--skip-build'])

    if profile == 'release':
        plan.update(dict.fromkeys(FLAGS, True))
        add('full release qualification', ['bash', 'scripts/qualify.sh', '--require-browser'])
        return plan
    if not p:
        return plan
    docs = all((Path(f).suffix.lower() in {'.md', '.txt', '.rst'}
                and not any(part in f for part in ('/skills/', '/policies/'))
                and not (f.startswith(CORE) and Path(f).name != 'README.md'))
               or f.startswith('docs/') for f in p)
    if docs and 'THIRD_PARTY.md' not in p:
        if profile != 'compat':
            add('documentation links', ['@python', 'scripts/check.py', '--syntax-only', *sorted(p)])
        return plan
    tooling = any(f.startswith(('scripts/', '.github/')) or f == 'AGENTS.md' for f in p)
    python_dependencies = any(f in {'pyproject.toml', 'requirements.lock'} or f.endswith('/pyproject.toml') for f in p)
    frontend_dependencies = any(f in {'frontend/package.json', 'frontend/package-lock.json'} for f in p)
    controller_dependencies = any(f in {'evals/pageagent/package.json', 'evals/pageagent/package-lock.json'} for f in p)
    dependencies = python_dependencies or frontend_dependencies or controller_dependencies
    broad = '__all_product__' in p
    packaging = dependencies or any(f in {'scripts/check_wheel_contents.py', 'scripts/check_wheels.sh',
                                         'MANIFEST.in', CORE + '__init__.py'} for f in p)
    builders = any(f in {'scripts/build_runtime.py', 'scripts/build_panel.py', 'frontend/scripts/build-trace.mjs'} for f in p)
    packaging = packaging or builders
    core_paths = [f for f in p if f.startswith(CORE)]
    production = [f for f in core_paths if not f.startswith(CORE + 'tests/')]
    core = bool(core_paths) or broad or python_dependencies
    frontend_paths = [f for f in p if f.startswith('frontend/')]
    story_paths = [f for f in frontend_paths if f.startswith(('frontend/stories/', 'frontend/.storybook/')) or '.stories.' in Path(f).name]
    frontend = bool(set(frontend_paths) - set(story_paths)) or broad or builders
    runtime = ('frontend/vite.runtime.config.ts' in p) or builders or frontend_dependencies or controller_dependencies or any(f.startswith(('frontend/src/runtime/', 'frontend/src/contract/', 'frontend/src/trace/', 'frontend/trace/')) for f in p)
    transport = any(f.startswith((CORE + 'transport/', CORE + 'state/', CORE + 'composition/',
                                  CORE + 'tests/transport/', CORE + 'tests/state/test_traceview.py')) for f in p)
    contacts = any(f.startswith('testapps/contacts/') for f in p) or broad or (profile == 'ci' and python_dependencies)
    yudao = any(f.startswith('examples/yudao/') for f in p) or broad or (profile == 'ci' and python_dependencies)
    odoo = any(f.startswith('examples/odoo/') for f in p)
    if profile == 'compat':
        if core or yudao or odoo or tooling:
            assets()
            add('Python compatibility', ['@python', RUNNER, '--without-browser'], needs_python_packages=True)
        plan['needs_node'] = plan['needs_frontend']
        return plan
    add('changed-file syntax/links', ['@python', 'scripts/check.py', '--syntax-only', *sorted(p)])
    plan['needs_python_packages'] = any(Path(f).suffix in {'.yaml', '.yml'} for f in p)
    if tooling:
        add('check selection regression', ['@python', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-q'], needs_python_packages=True)
    if production or broad or python_dependencies or 'scripts/check_core_boundary.py' in p:
        add('core boundary', ['@python', 'scripts/check_core_boundary.py'])
    if dependencies or any(f in {'scripts/lock_and_sbom.py', 'sbom.json', 'THIRD_PARTY.md'} for f in p):
        add('dependency lock/SBOM', ['@python', 'scripts/lock_and_sbom.py', '--check'])
    if core:
        # Core includes Node VM/static asset contracts, but never implicit Chromium.
        assets()
        command = ['@python', RUNNER, '--without-browser']
        tests = [f for f in core_paths if '/tests/' in f]
        if profile == 'dev' and not broad:
            groups = set()
            for f in production:
                groups.update(GROUPS.get(f[len(CORE):].split('/')[0], ()))
            if production and groups and all(f[len(CORE):].split('/')[0] in GROUPS for f in production):
                for group in sorted(groups):
                    command += ['--group', group]
            elif not production and tests and all(Path(f).name.startswith('test_') and f.endswith('.py') for f in tests):
                for f in sorted(tests):
                    if (ROOT / f).is_file():
                        command += ['--module', f.removeprefix('src/').removesuffix('.py').replace('/', '.')]
        add('core regression', command, needs_python_packages=True)
    if frontend:
        add('frontend lint', ['npm', 'run', 'lint'], cwd='frontend', needs_frontend=True)
        add('frontend unit tests', ['npm', 'test'], cwd='frontend')
        assets()
        if profile == 'ci':
            files = sorted(str(f.relative_to(ROOT / 'frontend')) for f in (ROOT / 'frontend/e2e').glob('*.spec.ts'))
            add('panel Chromium', ['npx', 'playwright', 'test', *files], cwd='frontend', needs_node_browser=True)
    if story_paths:
        add('Storybook build', ['npm', 'run', 'build-storybook'], cwd='frontend', needs_frontend=True)
    if contacts:
        add('Contacts contracts', ['@python', RUNNER, '--start-dir', 'testapps/contacts/tests',
                                  '--top-level', '.', '--without-browser'], needs_python_packages=True)
    if yudao:
        add('Yudao contracts', ['@python', RUNNER, '--start-dir', 'examples/yudao/src/web_operation_agent_yudao/tests',
                               '--top-level', 'examples/yudao/src', '--without-browser'], needs_python_packages=True)
        plan['needs_nginx'] = any('/deploy/' in f or 'nginx' in f for f in p) or broad
    if odoo:
        add('Odoo reference adapter', ['@python', RUNNER, '--start-dir', 'examples/odoo/src/web_operation_agent_odoo/tests',
                                      '--top-level', 'examples/odoo/src', '--without-browser'], needs_python_packages=True)
    # Expensive browser integration follows browser/session boundaries, never copy edits.
    if profile == 'ci' and (runtime or transport or contacts or broad):
        assets()
        add('runtime Chromium', ['@python', RUNNER, '--only-browser', '--strict-skips'], needs_python_packages=True,
            needs_page_controller=True, needs_python_browser=True)
    if profile == 'ci' and contacts:
        add('Contacts Chromium', ['@python', RUNNER, '--start-dir', 'testapps/contacts/tests', '--top-level', '.',
                                 '--only-browser', '--strict-skips'], needs_python_packages=True,
            needs_page_controller=True, needs_python_browser=True)
    # Evaluators are development tools with their own owners, not product dependencies.
    for prefix, directory, top in [('evals/odoo/', 'evals/odoo/tests', '.'),
                                    ('evals/odoo_source/', 'evals/odoo_source/tests', '.'),
                                    ('evals/yudao/', 'evals/yudao/tests', '.')]:
        if any(f.startswith(prefix) for f in p):
            add(prefix.rstrip('/') + ' apparatus', ['@python', '-m', 'unittest', 'discover', '-s', directory,
                                                    '-t', top], needs_python_packages=True)
    if any(f.startswith('evals/pageagent/') for f in p):
        add('PageAgent comparison', ['npm', 'test'], cwd='evals/pageagent', needs_page_controller=True)
    known = lambda f: (f.startswith((CORE, 'frontend/', 'examples/odoo/', 'examples/yudao/', 'testapps/contacts/', 'evals/', 'docs/', 'scripts/', '.github/'))
                       or f in {'AGENTS.md', '.gitignore', '.gitattributes', '.editorconfig', 'LICENSE', 'pyproject.toml', 'requirements.lock', 'sbom.json', 'THIRD_PARTY.md'}
                       or Path(f).suffix.lower() in {'.md', '.txt', '.rst'})
    if any(not known(f) for f in p) and not core:
        assets()
        add('unmapped change: core regression', ['@python', RUNNER, '--without-browser'], needs_python_packages=True)
    if packaging:
        assets()
        add('wheel contents and isolated imports', ['bash', 'scripts/check_wheels.sh'], needs_python_packages=True)
    plan['needs_node'] = plan['needs_frontend'] or plan['needs_page_controller']
    return plan


def syntax(paths: list[str]) -> int:
    for name in paths:
        path = ROOT / name
        if not path.is_file():  # deleted files are still inputs to selection, not syntax targets
            continue
        suffix = path.suffix.lower()
        if suffix == '.py':
            ast.parse(path.read_text(), filename=name)
        elif suffix == '.json':
            json.loads(path.read_text())
        elif suffix == '.sh' or name in {'local', 'deploy'}:
            subprocess.run(['bash', '-n', str(path)], check=True)
        elif suffix in {'.yaml', '.yml'}:
            import yaml
            class ComposeLoader(yaml.SafeLoader):
                pass
            def compose_tag(loader, node):
                if isinstance(node, yaml.SequenceNode):
                    return loader.construct_sequence(node, deep=True)
                if isinstance(node, yaml.MappingNode):
                    return loader.construct_mapping(node, deep=True)
                return loader.construct_scalar(node)
            for tag in ('!reset', '!override'):
                ComposeLoader.add_constructor(tag, compose_tag)
            yaml.load(path.read_text(), Loader=ComposeLoader)
        elif suffix == '.md':
            for target in re.findall(r'\]\(([^\s)]+)(?:\s+[^)]*)?\)', path.read_text()):
                url = urlsplit(target.strip('<>'))
                if url.scheme or not url.path:
                    continue
                if not (path.parent / unquote(url.path)).exists():
                    raise ValueError(f'{name}: missing local link {target}')
    print(f'syntax/links: {len(paths)} changed paths checked')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('dev', 'ci', 'compat', 'release'), default='dev')
    parser.add_argument('--base')
    parser.add_argument('--head', help='explicit commit-only comparison; otherwise include local changes')
    parser.add_argument('--path', action='append', default=[])
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--syntax-only', nargs='+', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.syntax_only:
        return syntax(args.syntax_only)
    paths, reason = (args.path, 'explicit paths') if args.path else changed_paths(args.base, args.head)
    plan = make_plan(paths, args.profile, reason)
    if args.plan:
        print(json.dumps(plan))
        return 0
    if not plan['checks']:
        print('No relevant changes: no checks selected.')
        return 0
    environment = {**os.environ, 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1', 'STORYBOOK_DISABLE_TELEMETRY': '1'}
    environment['PYTHONPATH'] = os.pathsep.join(str(ROOT / f) for f in ('src', 'examples/odoo/src', 'examples/yudao/src', '.'))
    if plan['needs_page_controller']:
        environment['WOA_PAGE_CONTROLLER_MODULE'] = str(ROOT / 'evals/pageagent/node_modules/@page-agent/page-controller/dist/lib/page-controller.js')
    started = time.monotonic()
    for check in plan['checks']:
        command = [sys.executable if value == '@python' else value for value in check['argv']]
        before = time.monotonic()
        print(f"\n[{check['name']}]", flush=True)
        result = subprocess.run(command, cwd=ROOT / check['cwd'], env=environment)
        print(f"{check['name']}: exit {result.returncode}, {time.monotonic() - before:.2f}s", flush=True)
        if result.returncode:
            return result.returncode
    print(f'Completed {len(plan["checks"])} selected checks in {time.monotonic() - started:.2f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
