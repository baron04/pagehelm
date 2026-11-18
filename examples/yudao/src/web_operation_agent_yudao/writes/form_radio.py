"""Literal radio facts and shallow allowlisted constant imports, never script execution."""
import re

from web_operation_agent.planning.sourcetree import AccessDenied


def literal(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value in {"true", "false"} or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        return value
    if re.fullmatch(r"'[^'\\\n]{1,500}'|\"[^\"\\\n]{1,500}\"", value):
        return value[1:-1]
    return None


def constant_imports(tree, text):
    """At most eight direct view-local TS files; no evaluation, recursion or enumeration."""
    wanted = set(re.findall(r':value=[\'"]([A-Za-z][A-Za-z0-9_]*)\.[A-Z][A-Z0-9_]*[\'"]', text))
    imports = re.findall(r"import\s*\{([^}]+)\}\s*from\s*['\"](@/views/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+)['\"]", text)
    found, dependencies = {}, {}
    for names, address in imports:
        names = [n.strip() for n in names.split(',')]
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', n) for n in names):
            continue
        used = wanted.intersection(names)
        path = 'ui/src/' + address[2:] + '.ts'
        if not used or any(part in {'.', '..'} for part in path.split('/')):
            continue
        if len(dependencies) >= 8 and path not in dependencies:
            continue
        try:
            source = tree.read_text(path)
        except (AccessDenied, OSError, UnicodeError):
            continue
        dependencies[path] = source
        for name in used:
            blocks = re.findall(r'export\s+const\s+' + re.escape(name) + r'\s*=\s*\{([^{}]+)\}\s*as\s+const\b', source)
            entries = {}
            if len(blocks) == 1:
                body = re.sub(r'//[^\n]*', '', blocks[0]).strip().rstrip(',')
                for item in body.split(','):
                    match = re.fullmatch(r'\s*([A-Z][A-Z0-9_]*)\s*:\s*(.*?)\s*', item)
                    if not match or match[1] in entries or literal(match[2]) is None:
                        entries = {}
                        break
                    entries[match[1]] = literal(match[2])
            # A second import with the same binding is ambiguous, even if bytes agree.
            found[name] = None if name in found else entries
    return {name + '.' + key: value for name, entries in found.items() if entries
            for key, value in entries.items()}, tuple(sorted(dependencies.items()))


def radio_choices(control, constants):
    choices = []
    for option in control.children:
        if option.tag not in {'el-radio', 'el-radio-button'} or option.children:
            return None
        attrs = option.attrs
        if any(k.startswith(('v-', '@', ':disabled', 'v-bind')) or k == 'disabled' for k in attrs):
            return None
        if ('value' in attrs) == (':value' in attrs):
            return None
        value = attrs.get('value') if 'value' in attrs else literal(attrs.get(':value'))
        if value is None:
            value = constants.get(attrs.get(':value'))
        label = ' '.join(option.text.split())
        if not isinstance(value, str) or not label or '{{' in label or len(label) > 120:
            return None
        choices.append((value, label))
    if (not 1 <= len(choices) <= 50 or len({v for v, _ in choices}) != len(choices)
            or len({l for _, l in choices}) != len(choices)):
        return None
    return tuple(choices)
