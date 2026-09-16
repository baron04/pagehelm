"""The registry is checked against the code, not trusted.

Two things are enforced here. Every fallible source has its consumers written down, and every
function that reads one is among them, so a consumer cannot be added without being counted. And
none of those functions fills a missing observation in with an empty value.

That the consumers then read the value rather than merely tolerate it is what the metamorphic
tests in :mod:`evals.odoo.tests.test_evaluator` show.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from evals.odoo import capture, consumers

REPOSITORY = Path(__file__).resolve().parents[3]
#: What an observation is never filled in with. ``.get(name)`` without a default is a read that
#: may answer nothing; ``.get(name, [])`` and ``value or []`` are answers invented for it.
_EMPTY = (ast.List, ast.Dict, ast.Set)
_EMPTY_CONSTANTS = ([], {}, 0, 0.0, "", False, None)


def _is_empty(node: ast.AST) -> bool:
    if isinstance(node, _EMPTY) and not getattr(node, "elts", None) and not getattr(node, "keys",
                                                                                    None):
        return True
    return isinstance(node, ast.Constant) and any(
        node.value is value or (type(node.value) is type(value) and node.value == value)
        for value in _EMPTY_CONSTANTS)


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

    def test_a_function_that_reads_a_source_is_in_the_registry(self):
        """A consumer added without being counted is exactly how a source came to be read in
        one place by rules nobody applied in another."""
        registered = {(consumer.module, consumer.function)
                      for listed in consumers.CONSUMERS.values() for consumer in listed}
        registered |= set(consumers.SHARED_READERS)
        for relative in consumers.REGISTERED_MODULES:
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

    def test_no_consumer_fills_a_missing_observation_in(self):
        for source, listed in consumers.CONSUMERS.items():
            for consumer in listed:
                _, functions = _module(consumer.module)
                node = functions[consumer.function]
                for child in ast.walk(node):
                    if (isinstance(child, ast.BoolOp) and isinstance(child.op, ast.Or)
                            and not _from_task_parameters(child.values[0])):
                        for value in child.values[1:]:
                            self.assertFalse(
                                _is_empty(value),
                                f"{consumer.module}:{consumer.function} fills a value in with "
                                f"an empty one at line {child.lineno}")
                    if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "get" and len(child.args) == 2
                            and not _from_task_parameters(child.func.value)):
                        self.assertFalse(
                            _is_empty(child.args[1]),
                            f"{consumer.module}:{consumer.function} reads a missing value as an "
                            f"empty one at line {child.lineno}")


if __name__ == "__main__":
    unittest.main()
