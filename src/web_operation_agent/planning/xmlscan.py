"""A minimal XML reader that keeps line numbers.

`xml.etree` throws away positions, and the only ways to get them back are private APIs that
have moved between CPython releases. Citations are the whole point of a SourceMap -- a
relation without a checkable line span is worth nothing -- so the tree is built directly on
`xml.parsers.expat`, whose `CurrentLineNumber` is documented and stable.

Namespaces are stripped: every descriptor dialect this compiler reads (site-conf, widget,
services) declares exactly one default namespace per file and never mixes vocabularies, so
the prefix carries no information and only makes the matching code noisy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from xml.parsers import expat


@dataclass
class Node:
    tag: str
    attrs: dict
    start_line: int
    end_line: int = 0
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None

    def find_all(self, tag: str) -> list["Node"]:
        """Every descendant with this tag, in document order."""
        out = []
        stack = list(reversed(self.children))
        while stack:
            n = stack.pop()
            if n.tag == tag:
                out.append(n)
            stack.extend(reversed(n.children))
        return out

    def kids(self, tag: str) -> list["Node"]:
        return [c for c in self.children if c.tag == tag]

    def first(self, tag: str) -> "Node | None":
        for c in self.children:
            if c.tag == tag:
                return c
        return None


class XMLScanError(Exception):
    pass


def parse(text: str) -> Node:
    root = Node(tag="#document", attrs={}, start_line=1)
    stack = [root]
    p = expat.ParserCreate(namespace_separator="\x01")

    def start(name, attrs):
        tag = name.rsplit("\x01", 1)[-1]
        clean = {k.rsplit("\x01", 1)[-1]: v for k, v in attrs.items()}
        node = Node(tag=tag, attrs=clean, start_line=p.CurrentLineNumber, parent=stack[-1])
        stack[-1].children.append(node)
        stack.append(node)

    def end(_name):
        node = stack.pop()
        node.end_line = p.CurrentLineNumber

    p.StartElementHandler = start
    p.EndElementHandler = end
    try:
        p.Parse(text.encode("utf-8"), True)
    except expat.ExpatError as e:
        raise XMLScanError(str(e))
    root.end_line = p.CurrentLineNumber
    return root
