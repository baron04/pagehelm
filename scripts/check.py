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
import tempfile
import time
import tomllib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CORE = 'src/web_operation_agent/'
RUNNER = 'src/web_operation_agent/tests/run_all.py'
FLAGS = ('needs_python_packages', 'needs_node', 'needs_frontend', 'needs_docs_site',
         'needs_page_controller', 'needs_python_browser', 'needs_node_browser', 'needs_nginx')
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
# Builders write the assets later checks read, so they run first and in order.
ASSET_CHECKS = ('runtime assets', 'sync runtime assets', 'sync trace assets')


YUDAO_SRC = 'examples/yudao/src/'
YUDAO_TESTS = YUDAO_SRC + 'web_operation_agent_yudao/tests/'


def yudao_modules(paths: list[str]) -> list[str] | None:
    """Yudao test modules importing a changed module, directly or transitively; None means all."""
    if paths and all(f.startswith('examples/yudao/deploy/') for f in paths):
        return ['web_operation_agent_yudao.tests.test_deploy_commands',
                'web_operation_agent_yudao.tests.test_local_deployment']
    package = ROOT / YUDAO_SRC / 'web_operation_agent_yudao'
    if not package.is_dir() or any(not (f.startswith(YUDAO_SRC + 'web_operation_agent_yudao/') and f.endswith('.py')
                                         and (ROOT / f).is_file()) for f in paths):
        return None
    name = lambda f: Path(f).relative_to(ROOT / YUDAO_SRC).with_suffix('').as_posix().replace('/', '.').removesuffix('.__init__')
    imports = {}
    for path in package.rglob('*.py'):
        module, edges = name(path), set()
        parts = module.split('.')
        if parts[-1] != 'web_operation_agent_yudao':
            edges.add('.'.join(parts[:-1]))  # importing a submodule runs its package
        current = parts if path.name == '__init__.py' else parts[:-1]
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = '.'.join(current[:len(current) - node.level + 1] + ([node.module] if node.module else [])) \
                    if node.level else node.module or ''
                targets = [base] + [f'{base}.{alias.name}' for alias in node.names]
            edges.update(t for t in targets if t.startswith('web_operation_agent_yudao'))
        imports[module] = edges
    changed = {name(ROOT / f) for f in paths}
    tests = []
    for module in sorted(m for m in imports if m.startswith('web_operation_agent_yudao.tests.test_')):
        seen, stack = set(), [module]
        while stack:
            item = stack.pop()
            if item not in seen:
                seen.add(item)
                stack.extend(imports.get(item, ()))
        if seen & changed:
            tests.append(module)
    return tests or None


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
        if (profile == 'dev' and not builders and not broad
                and not any(f.startswith('frontend/') and not f.startswith('frontend/docs/') for f in p)
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
            if any(f.startswith('docs/') for f in p):
                add('documentation site', ['npm', 'run', 'check:site'], cwd='frontend/docs', needs_docs_site=True)
                plan['needs_node'] = plan['needs_docs_site']
        return plan
    tooling = any(f.startswith(('scripts/', '.github/')) or f == 'AGENTS.md' for f in p)
    python_dependencies = any(f in {'pyproject.toml', 'requirements.lock'} or f.endswith('/pyproject.toml') for f in p)
    frontend_dependencies = any(f in {'frontend/package.json', 'frontend/package-lock.json'} for f in p)
    docs_dependencies = any(f in {'frontend/docs/package.json', 'frontend/docs/package-lock.json'} for f in p)
    product_dependencies = python_dependencies or frontend_dependencies
    dependencies = product_dependencies or docs_dependencies
    broad = '__all_product__' in p
    packaging = product_dependencies or any(f in {'scripts/check_wheel_contents.py', 'scripts/check_wheels.sh',
                                                 'MANIFEST.in', CORE + '__init__.py'} for f in p)
    builders = any(f in {'scripts/build_runtime.py', 'scripts/build_panel.py', 'frontend/scripts/build-trace.mjs'} for f in p)
    packaging = packaging or builders
    core_paths = [f for f in p if f.startswith(CORE)]
    production = [f for f in core_paths if not f.startswith(CORE + 'tests/')]
    core = bool(core_paths) or broad or python_dependencies
    docs_site_paths = [f for f in p if f.startswith('frontend/docs/')]
    frontend_paths = [f for f in p if f.startswith('frontend/') and not f.startswith('frontend/docs/')]
    story_paths = [f for f in frontend_paths if f.startswith(('frontend/stories/', 'frontend/.storybook/')) or '.stories.' in Path(f).name]
    frontend = bool(set(frontend_paths) - set(story_paths)) or broad or builders
    docs_site = bool(docs_site_paths) or any(f.startswith('docs/') for f in p) or broad
    runtime = ('frontend/vite.runtime.config.ts' in p) or builders or frontend_dependencies or any(f.startswith(('frontend/src/runtime/', 'frontend/src/contract/', 'frontend/src/trace/', 'frontend/trace/')) for f in p)
    transport = any(f.startswith((CORE + 'transport/', CORE + 'state/', CORE + 'composition/',
                                  CORE + 'tests/transport/', CORE + 'tests/state/test_traceview.py')) for f in p)
    contacts = any(f.startswith('testapps/contacts/') for f in p) or broad or (profile == 'ci' and python_dependencies)
    yudao = any(f.startswith('examples/yudao/') for f in p) or broad or (profile == 'ci' and python_dependencies)
    if profile == 'compat':
        if core or yudao or tooling:
            assets()
            add('Python compatibility', ['@python', RUNNER, '--without-browser'], needs_python_packages=True)
        plan['needs_node'] = plan['needs_frontend']
        return plan
    add('changed-file syntax/links', ['@python', 'scripts/check.py', '--syntax-only', *sorted(p)])
    plan['needs_python_packages'] = any(Path(f).suffix in {'.yaml', '.yml'} for f in p)
    if tooling:
        add('check selection regression', ['@python', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-q'], needs_python_packages=True)
    if (production or broad or python_dependencies or 'scripts/check_core_boundary.py' in p
            or any(f.startswith('frontend/src/runtime/') for f in p)):
        add('core boundary', ['@python', 'scripts/check_core_boundary.py'])
    if (core or yudao or contacts or frontend or bool(docs_site_paths) or tooling or broad
            or any(f in {'.env.example', '.gitignore', 'SECURITY.md'} for f in p)
            or any(f.startswith(('.agents/', 'evals/')) for f in p)):
        add('repository hygiene', ['@python', 'scripts/check_repo_hygiene.py'])
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
        frontend_config = any(
            Path(f).name.startswith(('eslint.config.', 'vite.', 'tsconfig'))
            or f in {'frontend/src/styles/design_tokens.json', 'frontend/scripts/build-tokens.mjs', 'frontend/src/presentation.json'}
            for f in p
        )
        rel_frontend = sorted(f.removeprefix('frontend/') for f in frontend_paths
                              if f not in story_paths and (ROOT / f).is_file())
        if profile == 'dev' and not (broad or builders or frontend_dependencies or frontend_config) and rel_frontend:
            lint_files = [f for f in rel_frontend if Path(f).suffix.lower() in {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'}]
            if lint_files:
                add('frontend lint', ['npx', 'eslint', *lint_files], cwd='frontend', needs_frontend=True)
            add('frontend unit tests', ['npx', 'vitest', 'related', *rel_frontend, '--run', '--passWithNoTests'],
                cwd='frontend', needs_frontend=True)
        else:
            add('frontend lint', ['npm', 'run', 'lint'], cwd='frontend', needs_frontend=True)
            add('frontend unit tests', ['npm', 'test'], cwd='frontend')
        assets()
        if profile == 'ci':
            files = sorted(str(f.relative_to(ROOT / 'frontend')) for f in (ROOT / 'frontend/e2e').glob('*.spec.ts'))
            add('panel Chromium', ['npx', 'playwright', 'test', *files], cwd='frontend', needs_node_browser=True)
    if story_paths:
        add('Storybook build', ['npm', 'run', 'build-storybook'], cwd='frontend', needs_frontend=True)
    if docs_site:
        add('documentation site', ['npm', 'run', 'check:site'], cwd='frontend/docs', needs_docs_site=True)
    if contacts:
        add('Contacts contracts', ['@python', RUNNER, '--start-dir', 'testapps/contacts/tests',
                                  '--top-level', '.', '--without-browser'], needs_python_packages=True)
    if yudao:
        command = ['@python', RUNNER, '--start-dir', YUDAO_TESTS.rstrip('/'), '--top-level', YUDAO_SRC.rstrip('/'),
                   '--without-browser']
        selected = yudao_modules([f for f in p if f.startswith('examples/yudao/')]) if profile == 'dev' and not broad else None
        for module in selected or ():
            command += ['--module', module]
        add('Yudao contracts', command, needs_python_packages=True)
        plan['needs_nginx'] = any('/deploy/' in f or 'nginx' in f for f in p) or broad
    # Expensive browser integration follows browser/session boundaries, never copy edits.
    if profile == 'ci' and (runtime or transport or contacts or broad):
        assets()
        add('runtime Chromium', ['@python', RUNNER, '--only-browser', '--strict-skips'], needs_python_packages=True,
            needs_page_controller=True, needs_python_browser=True)
    if profile == 'ci' and contacts:
        add('Contacts Chromium', ['@python', RUNNER, '--start-dir', 'testapps/contacts/tests', '--top-level', '.',
                                 '--only-browser', '--strict-skips'], needs_python_packages=True,
            needs_page_controller=True, needs_python_browser=True)
    if profile == 'ci' and yudao:
        assets()
        add('Yudao Chromium', ['@python', RUNNER, '--start-dir', YUDAO_TESTS.rstrip('/'),
                               '--top-level', YUDAO_SRC.rstrip('/'), '--only-browser', '--strict-skips'],
            needs_python_packages=True, needs_page_controller=True, needs_python_browser=True)
    # Evaluators are development tools with their own owners, not product dependencies.
    for prefix, directory, top in [('evals/yudao/', 'evals/yudao/tests', '.')]:
        if any(f.startswith(prefix) for f in p):
            add(prefix.rstrip('/') + ' apparatus', ['@python', '-m', 'unittest', 'discover', '-s', directory,
                                                    '-t', top], needs_python_packages=True)
    known = lambda f: (f.startswith((CORE, '.agents/', 'frontend/', 'examples/yudao/', 'testapps/contacts/', 'evals/', 'docs/', 'scripts/', '.github/'))
                       or f in {'AGENTS.md', '.gitignore', '.gitattributes', '.editorconfig', '.env.example',
                                'LICENSE', 'SECURITY.md', 'pagehelm.example.toml', 'pyproject.toml',
                                'requirements.lock', 'sbom.json', 'THIRD_PARTY.md'}
                       or Path(f).suffix.lower() in {'.md', '.txt', '.rst'})
    if any(not known(f) for f in p) and not core:
        assets()
        add('unmapped change: core regression', ['@python', RUNNER, '--without-browser'], needs_python_packages=True)
    if packaging:
        assets()
        add('wheel contents and isolated imports', ['bash', 'scripts/check_wheels.sh'], needs_python_packages=True)
    plan['needs_node'] = plan['needs_frontend'] or plan['needs_docs_site'] or plan['needs_page_controller']
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
        elif suffix == '.toml':
            tomllib.loads(path.read_text())
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
    parser.add_argument('--only', action='append', default=[], help='run only this planned check (repeatable)')
    parser.add_argument('--keep-going', action='store_true', help='run every check even after a failure')
    parser.add_argument('--serial', action='store_true', help='run checks one at a time with live output')
    parser.add_argument('--jobs', type=int, default=os.cpu_count() or 1, help='parallel test modules per Python suite')
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
    environment['PYTHONPATH'] = os.pathsep.join(str(ROOT / f) for f in ('src', 'examples/yudao/src', '.'))
    if plan['needs_page_controller']:
        environment['WOA_PAGE_CONTROLLER_MODULE'] = str(ROOT / 'frontend/node_modules/@page-agent/page-controller/dist/lib/page-controller.js')
    checks = [c for c in plan['checks'] if not args.only or c['name'] in args.only]
    if unknown := sorted(set(args.only) - {c['name'] for c in plan['checks']}):
        print(f'--only names not in the plan: {unknown}', file=sys.stderr)
        return 2
    return execute(checks, environment, jobs=args.jobs, parallel=not args.serial, keep_going=args.keep_going)


def execute(checks: list[dict], environment: dict, *, jobs: int, parallel: bool, keep_going: bool) -> int:
    """Run asset builders, then the other checks concurrently, then each browser lane alone."""
    started = time.monotonic()
    failed = []

    def command(check, share):
        argv = [sys.executable if value == '@python' else value for value in check['argv']]
        return argv + ['--jobs', str(share)] if RUNNER in argv and share > 1 else argv

    def run(check, log, share=jobs):
        before = time.monotonic()
        result = subprocess.run(command(check, share), cwd=ROOT / check['cwd'], env=environment,
                                stdout=log, stderr=subprocess.STDOUT)
        return result.returncode, time.monotonic() - before

    def report(check, code, seconds, log=None):
        if log is not None:
            log.seek(0)
            print(f"\n[{check['name']}]\n" + log.read().decode(errors='replace'), end='', flush=True)
        print(f"{check['name']}: exit {code}, {seconds:.2f}s", flush=True)
        if code:
            failed.append(check['name'])

    builders = [c for c in checks if c['name'] in ASSET_CHECKS]
    rest = [c for c in checks if c['name'] not in ASSET_CHECKS]
    # Browser lanes parallelise inside themselves; beside other work their timing-sensitive tests flake.
    browsers = [c for c in rest if 'Chromium' in c['name']]
    others = [c for c in rest if c not in browsers]
    waves = [[c] for c in builders] + ([others] if parallel else [[c] for c in others]) + [[c] for c in browsers]
    for wave in filter(None, waves):
        if failed and not keep_going:
            return finish(checks, failed, started)
        if len(wave) > 1:
            print(f"\nrunning concurrently: {', '.join(c['name'] for c in wave)}", flush=True)
        logs = {c['name']: tempfile.TemporaryFile() for c in wave} if len(wave) > 1 else {}
        # Concurrent suites split the processors; the frontend runner keeps its own pool beside them.
        share = max(1, jobs // max(1, sum(RUNNER in c['argv'] for c in wave) + any(c['cwd'] == 'frontend' for c in wave)))
        with ThreadPoolExecutor(len(wave)) as pool:
            if not logs:
                print(f"\n[{wave[0]['name']}]", flush=True)
            pending = {pool.submit(run, c, logs.get(c['name']), share): c for c in wave}
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    check = pending.pop(future)
                    report(check, *future.result(), logs.get(check['name']))
        for log in logs.values():
            log.close()
    return finish(checks, failed, started)


def finish(checks: list[dict], failed: list[str], started: float) -> int:
    elapsed = time.monotonic() - started
    if failed:
        print(f'FAILED {len(failed)} of {len(checks)} checks in {elapsed:.2f}s: {", ".join(failed)}')
        return 1
    print(f'Completed {len(checks)} selected checks in {elapsed:.2f}s')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT / 'src'))
    from web_operation_agent import python_is_supported, supported_python_text
    if not python_is_supported():
        print(f'需要 Python {supported_python_text()}；请使用 .venv/bin/python scripts/check.py。',
              file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
