"""The registry is checked against the code, not trusted.

Two things are enforced here. Every fallible source has its consumers written down, and every
function that reads one is among them, so a consumer cannot be added without being counted. And
none of those functions fills a missing observation in with an empty value.

That the consumers then read the value rather than merely tolerate it is what the metamorphic
tests in :mod:`evals.odoo.tests.judging.test_evaluator` show.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from evals.odoo.judging import capture, consumers

REPOSITORY = Path(__file__).resolve().parents[4]
#: What an observation is never filled in with. ``.get(name)`` without a default is a read that
#: may answer nothing; ``.get(name, [])`` and ``value or []`` are answers invented for it. Null
#: is not among them: it is how this apparatus writes "nobody read it", so producing it is the
#: rule rather than the mistake.
#: The empty tuple is among them, so `value if read else ()` fails the rule as
#: `value if read else []` does.
_EMPTY = (ast.List, ast.Dict, ast.Set, ast.Tuple)
_EMPTY_CONSTANTS = ([], {}, (), 0, 0.0, "", False)


#: The builders of the same empty values: the check reads the calls that produce them as well as
#: the literals, so ``list()`` fails where ``[]`` does.
_EMPTY_BUILDERS = frozenset({"list", "dict", "set", "tuple", "str", "frozenset"})


def _is_empty(node: ast.AST) -> bool:
    if isinstance(node, _EMPTY) and not getattr(node, "elts", None) and not getattr(node, "keys",
                                                                                    None):
        return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _EMPTY_BUILDERS and not node.args and not node.keywords):
        return True
    return isinstance(node, ast.Constant) and node.value is not None and any(
        type(node.value) is type(value) and node.value == value for value in _EMPTY_CONSTANTS)


#: Receivers whose absent fields are part of a sealed task's definition rather than a lost
#: observation: the task parameters a task set declares. What an observation holds is what this
#: check is about, and a task that declares no leak fields has none to check.
_TASK_PARAMETERS = ("p", "params")


def _from_task_parameters(node: ast.AST) -> bool:
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        node = node.func.value
    if isinstance(node, ast.Attribute):
        return node.attr in _TASK_PARAMETERS
    return isinstance(node, ast.Name) and node.id in _TASK_PARAMETERS


#: Names that read the observation algebra wherever they are reached from.
_READS = frozenset({"read", "of_record", "Present", "Unavailable"})
#: Modules that define or describe the algebra rather than consume it.
_NOT_CONSUMERS = frozenset({"evals/odoo/judging/capture.py", "evals/odoo/judging/consumers.py"})


def _capture_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """What this module calls the algebra: the names bound to the module, and the names of the
    algebra's own functions imported directly."""
    modules, direct = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "capture":
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (node.module or "").split(".")[-1] == "capture":
                    direct.add(alias.asname or alias.name)
                elif alias.name == "capture":
                    modules.add(alias.asname or alias.name)
    return modules, direct


def _capture_readers() -> list[str]:
    """Every apparatus module that reads a fallible source, found by reading the tree rather than
    by asking the registry this check exists to test."""
    found = []
    for path in sorted(REPOSITORY.glob("evals/**/*.py")):
        relative = path.relative_to(REPOSITORY).as_posix()
        if relative in _NOT_CONSUMERS or "/tests/" in relative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, direct = _capture_aliases(tree)
        if not modules and not direct:
            continue
        reads = any(
            (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
             and node.value.id in modules and node.attr in _READS)
            or (isinstance(node, ast.Name) and node.id in direct & _READS)
            for node in ast.walk(tree))
        if reads:
            found.append(relative)
    return found


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Every function in a module by its qualified name, nested ones included."""
    found: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                found[name] = child
                walk(child, f"{name}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _module(relative: str) -> tuple[ast.AST, dict[str, ast.AST]]:
    tree = ast.parse((REPOSITORY / relative).read_text(encoding="utf-8"))
    return tree, _functions(tree)


class RegistryTests(unittest.TestCase):
    def test_every_deliberate_substitution_names_a_consumer_and_a_reason(self):
        registered = {(consumer.module, consumer.function)
                      for listed in consumers.CONSUMERS.values() for consumer in listed}
        for module, function, expression, reason in consumers.DELIBERATE_SUBSTITUTIONS:
            self.assertIn((module, function), registered)
            self.assertTrue(expression.strip(), (module, function))
            self.assertGreater(len(reason.strip()), 40, (module, function, expression))

    def test_every_fallible_source_has_its_consumers_written_down(self):
        self.assertEqual(set(consumers.CONSUMERS), set(capture.SOURCES))
        for source, listed in consumers.CONSUMERS.items():
            self.assertTrue(listed, source)
            for consumer in listed:
                self.assertTrue(consumer.decides.strip(), consumer)

    def test_every_listed_consumer_exists_where_it_says(self):
        for source, listed in consumers.CONSUMERS.items():
            for consumer in listed:
                _, functions = _module(consumer.module)
                self.assertIn(consumer.function, functions, (source, consumer))

    def test_the_registry_covers_every_module_that_reaches_the_algebra(self):
        """The modules scanned are found in the tree, not derived from the registry itself, so a
        module reading the algebra is scanned before anyone has listed it, under whatever name it
        imports the algebra by, and the registry must already cover it. It covers more besides:
        a module that reads what a capture holds without naming the algebra is a consumer too,
        and is listed by hand."""
        unlisted = sorted(set(_capture_readers()) - set(consumers.REGISTERED_MODULES))
        self.assertEqual(unlisted, [], "a module reaches the algebra and the registry omits it")

    def test_a_function_that_reads_a_source_is_in_the_registry(self):
        """A consumer added without being counted is exactly how a source came to be read in
        one place by rules nobody applied in another."""
        registered = {(consumer.module, consumer.function)
                      for listed in consumers.CONSUMERS.values() for consumer in listed}
        registered |= set(consumers.SHARED_READERS)
        # Scanned by the union, so a module that reaches the algebra is checked before anyone
        # has listed it rather than because someone already did.
        for relative in sorted(set(consumers.REGISTERED_MODULES) | set(_capture_readers())):
            _, functions = _module(relative)
            for name, node in functions.items():
                reads = any(
                    isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "capture"
                    and call.func.attr in {"read", "of_record", "Present", "Unavailable"}
                    for call in ast.walk(node) if isinstance(call, ast.Call))
                if reads:
                    outer = name.split(".<locals>.")[0]
                    self.assertTrue((relative, name) in registered
                                    or (relative, outer) in registered,
                                    f"{relative}:{name} reads a source and is not registered")

    def test_every_declared_substitution_is_written_where_it_says(self):
        """A reason for an expression nobody writes any more exempts nothing, and hides that the
        substitution it described is gone. Each one is matched against the source."""
        for module, function, expression, reason in consumers.DELIBERATE_SUBSTITUTIONS:
            with self.subTest(module=module, function=function):
                self.assertTrue(reason.strip())
                tree, functions = _module(module)
                self.assertIn(function, functions, module)
                written = {ast.unparse(child) for child in ast.walk(functions[function])
                           if isinstance(child, ast.IfExp)}
                self.assertIn(expression, written)

    def test_no_consumer_fills_a_missing_observation_in(self):
        """The check reads the conditional form of the substitution as well as `or []` and
        `.get(name, [])`."""
        # The allowlist exempts one written expression from the conditional rule only; the
        # other two rules apply to every registered consumer.
        allowed = {(module, function, expression) for module, function, expression, _reason
                   in consumers.DELIBERATE_SUBSTITUTIONS}
        for source, listed in consumers.CONSUMERS.items():
            for consumer in listed:
                _, functions = _module(consumer.module)
                node = functions[consumer.function]
                for child in ast.walk(node):
                    declared = isinstance(child, ast.IfExp) and (
                        consumer.module, consumer.function, ast.unparse(child)) in allowed
                    if (isinstance(child, ast.IfExp) and not declared
                            and not _from_task_parameters(child.test)):
                        for value in (child.body, child.orelse):
                            self.assertFalse(
                                _is_empty(value),
                                f"{consumer.module}:{consumer.function} reads a missing value as "
                                f"an empty one at line {child.lineno}")
                    if (isinstance(child, ast.BoolOp) and isinstance(child.op, ast.Or)
                            and not _from_task_parameters(child.values[0])):
                        for value in child.values[1:]:
                            self.assertFalse(
                                _is_empty(value),
                                f"{consumer.module}:{consumer.function} fills a value in with "
                                f"an empty one at line {child.lineno}")
                    if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                            and child.func.attr in {"get", "setdefault"} and len(child.args) == 2
                            and not _from_task_parameters(child.func.value)):
                        self.assertFalse(
                            _is_empty(child.args[1]),
                            f"{consumer.module}:{consumer.function} reads a missing value as an "
                            f"empty one at line {child.lineno}")
                    # The same substitution: the attribute a record lacks is the observation
                    # nobody made, and a default answers for it.
                    if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                            and child.func.id == "getattr" and len(child.args) == 3
                            and not _from_task_parameters(child.args[0])):
                        self.assertFalse(
                            _is_empty(child.args[2]),
                            f"{consumer.module}:{consumer.function} reads a missing attribute as "
                            f"an empty one at line {child.lineno}")


if __name__ == "__main__":
    unittest.main()
