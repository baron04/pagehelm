"""Uncomment every business module in a pinned yudao (ruoyi-vue-pro) checkout.

Upstream ships with only system and infra enabled; the other modules are
commented out in the root pom and in yudao-server's dependency list.
Idempotent: running it twice leaves the files unchanged.

Modules named after the checkout path stay disabled. (`ai` had to at v2026.08, whose
Spring AI Alibaba auto-configuration referenced a class the pinned Spring AI did not
ship; at v2026.09 it starts.) `iot` must stay disabled without a
TDengine instance: its TDengineTableInitRunner exits the server when the
time-series store is absent. `report` stays disabled: it wraps the closed-source
JimuReport starter, whose Spring AI 1.0.2 dependency downgrades
swagger-annotations to 2.2.30 and breaks /v3/api-docs with NoSuchMethodError.

Usage: enable_modules.py <server-checkout> [skipped-module ...]
"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
skipped = {f"yudao-module-{m}" for m in sys.argv[2:]}
line = re.compile(r"^(\s*)<!--\s*(.*?)\s*-->\s*$")


def module_of(text: str) -> str | None:
    m = re.search(r"yudao-module-[a-z]+", text)
    return m.group(0) if m else None


def uncomment(path: Path, keep) -> int:
    """Uncomment kept lines; a <dependency> block follows its artifactId."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out, changed = [], 0
    for i, raw in enumerate(lines):
        m = line.match(raw)
        if m:
            window = " ".join(lines[max(0, i - 2): i + 4]) if "yudao-module" not in raw else raw
            if module_of(window) in skipped and (m.group(2) != "</dependency>" or module_of(" ".join(lines[i - 3:i])) in skipped):
                out.append(raw)
                continue
        if m and keep(m.group(2)):
            out.append(m.group(1) + m.group(2))
            changed += 1
        else:
            out.append(raw)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


pom = root / "pom.xml"
n1 = uncomment(pom, lambda s: s.startswith("<module>yudao-module-"))
server = root / "yudao-server" / "pom.xml"
dep = re.compile(r"^(<dependency>|</dependency>|<groupId>cn\.iocoder\.boot</groupId>|<artifactId>yudao-module-[\w-]+</artifactId>|<version>\$\{revision\}</version>)$")
n2 = uncomment(server, lambda s: bool(dep.match(s)))
print(f"root modules uncommented: {n1}; server dependency lines uncommented: {n2}")
