"""Selection must skip unrelated work without silently accepting broken collection."""
from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('change_checks', ROOT / 'scripts/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class SelectionTests(unittest.TestCase):
    def names(self, paths, profile='ci'):
        plan = check.make_plan(paths, profile)
        return {row['name'] for row in plan['checks']}, plan

    def test_docs_do_not_install_runtime_or_browsers(self):
        names, plan = self.names(['README.md', 'docs/guides/a.md'])
        self.assertEqual(names, {'documentation links'})
        self.assertFalse(any(plan[key] for key in check.FLAGS))
        self.assertEqual(check.make_plan(['README.md'], 'compat')['checks'], [])

    def test_unchanged_tree_has_no_checks(self):
        self.assertEqual(check.make_plan([])['checks'], [])

    def test_local_core_change_selects_dependent_groups(self):
        names, plan = self.names(['src/web_operation_agent/modeling/client.py'], 'dev')
        command = next(c['argv'] for c in plan['checks'] if c['name'] == 'core regression')
        self.assertIn('--without-browser', command)
        self.assertIn('execution', command)
        self.assertIn('modeling', command)
        self.assertNotIn('runtime Chromium', names)
        self.assertNotIn('Contacts contracts', names)

    def test_core_ci_runs_whole_core_once_without_comparison_tools(self):
        names, plan = self.names(['src/web_operation_agent/modeling/client.py'])
        command = next(c['argv'] for c in plan['checks'] if c['name'] == 'core regression')
        self.assertNotIn('--group', command)
        self.assertNotIn('runtime Chromium', names)
        self.assertFalse(any('apparatus' in n or 'Contacts' in n for n in names))
        self.assertEqual(sum(c['name'] == 'core regression' for c in plan['checks']), 1)

    def test_session_boundary_selects_runtime_browser(self):
        names, plan = self.names(['src/web_operation_agent/transport/remote.py'])
        self.assertIn('runtime Chromium', names)
        self.assertTrue(plan['needs_page_controller'])
        self.assertTrue(plan['needs_python_browser'])
        self.assertFalse(plan['needs_node_browser'])

    def test_frontend_does_not_retest_server_lifecycle_or_storybook(self):
        names, plan = self.names(['frontend/src/components/Thread.tsx'])
        self.assertIn('panel Chromium', names)
        self.assertNotIn('runtime Chromium', names)
        self.assertNotIn('Storybook build', names)
        command = next(c['argv'] for c in plan['checks'] if c['name'] == 'panel Chromium')
        self.assertNotIn('e2e/storybook.spec.ts', command)

    def test_browser_runtime_auth_changes_select_cross_boundary_tests(self):
        names, _ = self.names(['frontend/src/runtime/boot.ts'])
        self.assertIn('runtime Chromium', names)

    def test_story_changes_build_and_test_storybook(self):
        names, plan = self.names(['frontend/src/stories/Panel.stories.tsx'])
        self.assertIn('Storybook build', names)
        browser = next(c for c in plan['checks'] if c['name'] == 'panel Chromium')
        self.assertIn('e2e/storybook.spec.ts', browser['argv'])

    def test_compat_never_runs_browser_or_javascript_tests(self):
        names, plan = self.names(['src/web_operation_agent/execution/session.py'], 'compat')
        self.assertIn('Python compatibility', names)
        self.assertFalse(plan['needs_python_browser'] or plan['needs_node_browser'])
        self.assertNotIn('frontend unit tests', names)

    def test_unknown_and_missing_baseline_do_not_return_empty_success(self):
        names, _ = self.names(['new-subsystem/program.py'])
        self.assertIn('unmapped change: core regression', names)
        with patch.object(check, 'git', side_effect=check.subprocess.CalledProcessError(1, ['git'])):
            paths, reason = check.changed_paths('0' * 40, 'HEAD')
        names, plan = self.names(paths)
        self.assertIn('core regression', names)
        self.assertIn('panel Chromium', names)
        self.assertIn('conservative', reason)

    def test_deleted_changed_test_falls_back_to_core(self):
        _, plan = self.names(['src/web_operation_agent/tests/execution/test_deleted.py'], 'dev')
        command = next(c['argv'] for c in plan['checks'] if c['name'] == 'core regression')
        self.assertNotIn('--module', command)

    def test_evaluation_changes_only_run_their_own_suite(self):
        names, plan = self.names(['evals/odoo/reporting/summary.py'])
        self.assertIn('evals/odoo apparatus', names)
        self.assertNotIn('core regression', names)
        self.assertFalse(plan['needs_python_browser'] or plan['needs_node_browser'])

    def test_packaging_runs_wheel_installation_check(self):
        names, _ = self.names(['pyproject.toml'])
        self.assertIn('wheel contents and isolated imports', names)

    def test_python_dependency_changes_do_not_run_node_browser_suite(self):
        names, plan = self.names(['requirements.lock'])
        self.assertIn('core regression', names)
        self.assertIn('runtime Chromium', names)
        self.assertNotIn('panel Chromium', names)
        self.assertFalse(plan['needs_node_browser'])
        names, _ = self.names(['sbom.json'])
        self.assertEqual(names, {'changed-file syntax/links', 'dependency lock/SBOM'})

    def test_release_is_explicit_and_only_delegates_once(self):
        names, plan = self.names([], 'release')
        self.assertEqual(names, {'full release qualification'})
        self.assertTrue(all(plan[key] for key in check.FLAGS))

    def test_notice_and_asset_builders_cannot_look_like_plain_documentation(self):
        names, _ = self.names(['THIRD_PARTY.md'])
        self.assertIn('dependency lock/SBOM', names)
        names, _ = self.names(['scripts/build_runtime.py'])
        self.assertIn('runtime assets', names)
        self.assertIn('wheel contents and isolated imports', names)
        self.assertIn('runtime Chromium', names)
        names, _ = self.names(['frontend/trace/trace_replay.html'])
        self.assertIn('runtime Chromium', names)
        names, _ = self.names(['scripts/check_core_boundary.py'])
        self.assertIn('core boundary', names)

    def test_local_base_comparison_also_includes_uncommitted_work(self):
        replies = [b'a' * 40, b'b' * 40, b'a' * 40, b'', b'src/new.py\0', b'new.md\0']
        with patch.object(check, 'git', side_effect=replies):
            paths, _ = check.changed_paths('main', None)
        self.assertEqual(paths, ['new.md', 'src/new.py'])
        replies = [b'a' * 40, b'b' * 40, b'a' * 40, b'committed.py\0']
        with patch.object(check, 'git', side_effect=replies) as git:
            paths, _ = check.changed_paths('main', 'HEAD')
        self.assertEqual(paths, ['committed.py'])
        self.assertEqual(git.call_count, 4)

    def test_runtime_skill_documents_are_not_plain_docs(self):
        names, _ = self.names(['examples/yudao/src/web_operation_agent_yudao/skills/query/SKILL.md'])
        self.assertIn('Yudao contracts', names)
        self.assertNotIn('documentation links', names)

    def test_missing_markdown_link_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('[missing](missing.md)')
            with patch.object(check, 'ROOT', root), self.assertRaises(ValueError):
                check.syntax(['README.md'])




class BrowserCollectionTests(unittest.TestCase):
    def test_browser_prerequisites_are_explicitly_marked(self):
        # Prevent a newly added/renamed browser class from leaking into the fast lane.
        import ast
        files = sorted((ROOT / 'src/web_operation_agent/tests').rglob('test_*.py'))
        files += sorted((ROOT / 'testapps/contacts/tests').glob('test_*.py'))
        for path in files:
            name = str(path.relative_to(ROOT))
            tree = ast.parse((ROOT / name).read_text())
            for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
                class_marked = any(isinstance(d, ast.Name) and d.id == 'browser_only' for d in cls.decorator_list)
                for method in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                    uses_browser = (method.name.startswith('test_') or method.name in {'setUp', 'setUpClass'}) and any(isinstance(n, ast.Name) and n.id in {'sync_playwright', '_chromium_launches'}
                                       for n in ast.walk(method))
                    method_marked = any(isinstance(d, ast.Name) and d.id == 'browser_only' for d in method.decorator_list)
                    self.assertTrue(not uses_browser or class_marked or method_marked,
                                    f'{name}:{cls.name}.{method.name} needs browser_only')


class LedgerGuardTests(unittest.TestCase):
    def test_inherited_ledger_is_never_used_and_environment_is_restored(self):
        import contextlib
        import io
        import os
        runner_spec = importlib.util.spec_from_file_location('test_guard_runner', ROOT / check.RUNNER)
        runner = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inherited.jsonl'
            path.write_text('original')
            scratch_paths = []

            class Case(unittest.TestCase):
                def test_meter(self):
                    target = Path(os.environ['WOA_SPEND_LEDGER'])
                    scratch_paths.append(target)
                    target.write_text('synthetic')

            with patch.dict(os.environ, {'WOA_SPEND_LEDGER': str(path)}), \
                    patch.object(runner, 'DEFAULT_LEDGER', str(path)), \
                    patch.object(unittest.TestLoader, 'loadTestsFromNames', return_value=unittest.TestSuite([Case('test_meter')])), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(['--module', 'fixture']), 0)
                self.assertEqual(os.environ['WOA_SPEND_LEDGER'], str(path))
            self.assertEqual(path.read_text(), 'original')
            self.assertNotEqual(scratch_paths[0], path)
            self.assertFalse(scratch_paths[0].exists())

    def test_empty_browser_selection_is_not_a_pass(self):
        import contextlib
        import io
        runner_spec = importlib.util.spec_from_file_location('empty_guard_runner', ROOT / check.RUNNER)
        runner = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(runner)
        with patch.object(unittest.TestLoader, 'loadTestsFromNames', return_value=unittest.TestSuite()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(['--module', 'fixture', '--only-browser']), 2)


if __name__ == '__main__':
    unittest.main()
