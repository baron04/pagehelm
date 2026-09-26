"""Rules of the deployment's source, as index entries on Odoo entities.

The compiler (`sourcemap.OdooCompiler`) records, with a citation into the exact source
lines, the buttons a form declares and the conditions on them, the fields a form shows and
the conditions on those, the refusals a model method raises and the condition each is
under, the window a method's action opens, and the fields a model declares with their help
text. This module turns those into `IndexRule`s in the page's own words: which control,
when it shows, what it opens, what refuses what. A window a control opens (a wizard) brings
its own refusals and field meanings along, attached to the entity whose page has the
control, since that is where a person meets them. Nothing here reads source at turn time;
the citation carries the lines' hash, computed when the map was compiled against the bound
release.
"""
from __future__ import annotations

from web_operation_agent.goal.index import IndexRule
from web_operation_agent.schemas.planning import SourceMap, SourceRelation

from .text import _ModelMeta

_MAX_CONDITION = 400
_MAX_MESSAGE = 160
#: Rules a window brings to the entity whose control opens it.
_MAX_WINDOW_RULES = 24


def _citation(relation: SourceRelation, release: str) -> str:
    ref = relation.evidence[0]
    return f"{ref.path}:{ref.start_line}-{ref.end_line}@{release} sha256:{ref.sha256}"


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _field_label(meta: _ModelMeta | None, name: str, strings: dict[str, str]) -> str:
    if meta is not None:
        info = meta.fields.get(name) or {}
        if info.get("string"):
            return str(info["string"])
    return strings.get(name) or name


def rules_for(source_map: SourceMap, *, release: str, metas: dict[str, _ModelMeta],
              models: tuple[str, ...]) -> dict[str, tuple[IndexRule, ...]]:
    """The rules about each model, in the order they help a person: what a control
    needs to be shown and what it opens, what a field needs to be changed, what an action
    refuses, what a field means. Rule ids are stable within one compiled map (r1, r2,
    ...). A rule's `subject` is the operation it concerns (the method a control calls,
    the action it opens), so that the rules of one operation can be answered together."""
    wanted = set(models)
    out: dict[str, list[IndexRule]] = {m: [] for m in wanted}
    counter = 0
    #: action xml id -> (model its window shows, its name, the action's own relation)
    windows: dict[str, tuple[str, str, SourceRelation | None]] = {}
    #: model.method -> (model of the window it opens, action id, action name, relation)
    opens: dict[str, tuple[str, str, str, SourceRelation]] = {}
    #: model.method whose window opens only under a condition in the method
    sometimes: set[str] = set()
    #: model -> field name -> declared string, for models the index has no metadata of
    strings: dict[str, dict[str, str]] = {}
    guards: dict[str, list[SourceRelation]] = {}
    helps: dict[str, list[SourceRelation]] = {}
    #: model.method -> the method name whose window it returns, and its relation
    via: list[tuple[str, str, SourceRelation]] = []
    for relation in source_map.relations:
        attrs = relation.attrs or {}
        if relation.kind == "request_handler" and attrs.get("action_id"):
            windows[str(attrs["action_id"])] = (relation.object, str(attrs.get("name") or ""),
                                                relation)
        elif relation.kind == "service_parameter" and attrs.get("direction") == "out":
            if attrs.get("opens_via"):
                via.append((relation.subject, str(attrs["opens_via"]), relation))
                continue
            opens[relation.subject] = (str(attrs.get("opens") or ""),
                                       str(attrs.get("opens_action") or ""),
                                       str(attrs.get("opens_name") or ""), relation)
            if attrs.get("conditional"):
                sometimes.add(relation.subject)
        elif relation.kind == "service_entity":
            if attrs.get("string"):
                strings.setdefault(relation.subject, {})[relation.object] = str(attrs["string"])
            if attrs.get("help"):
                helps.setdefault(relation.subject, []).append(relation)
        elif relation.kind == "service_guard":
            guards.setdefault(relation.subject.rpartition(".")[0], []).append(relation)

    # A method returning another method's action opens what that one opens, when
    # exactly one model's method of that name opens a window.
    for subject, method, relation in via:
        targets = [value for key, value in opens.items() if key.rpartition(".")[2] == method]
        if len(targets) == 1 and subject not in opens:
            shown, action_id, name, _target_relation = targets[0]
            opens[subject] = (shown, action_id, name, relation)

    def add(model: str, kind: str, about: str, text: str, relation: SourceRelation,
            subject: str = "") -> None:
        nonlocal counter
        if model not in wanted or not text:
            return
        counter += 1
        out[model].append(IndexRule(id=f"r{counter}", kind=kind, about=about, text=text,
                                    citation=_citation(relation, release), subject=subject))

    def window_of(model: str, method_or_action: str, target_type: str):
        """(window model, window name, the relations that declare it) a control leads
        to: the action record, the method's lines that open it, or both."""
        if target_type == "action":
            shown, name, declared = windows.get(method_or_action, ("", "", None))
            return shown, name, [r for r in (declared,) if r is not None]
        found = opens.get(f"{model}.{method_or_action}")
        if found is None:
            return "", "", []
        shown, action_id, name, relation = found
        declared = [relation]
        if action_id and action_id in windows:
            shown = shown or windows[action_id][0]
            name = name or windows[action_id][1]
            if windows[action_id][2] is not None:
                declared.append(windows[action_id][2])
        return shown, name, declared

    def refusal(model: str, relation: SourceRelation, *, prefix: str = "",
                subject: str = "") -> None:
        attrs = relation.attrs or {}
        method = relation.subject.rpartition(".")[2]
        message = _short(relation.object, _MAX_MESSAGE)
        condition = _short(attrs.get("condition") or "", _MAX_CONDITION)
        sig = (model, "guard", prefix, method, message, condition)
        if sig in seen:
            return
        seen.add(sig)
        via = str(attrs.get("via") or "")
        says = (f"refuses with the message of {via}" if via
                else f'refuses with "{message}"')
        add(model, "refusal", f"{prefix}{method}",
            f"{prefix}{method} {says}" + (f" when {condition}" if condition else ""),
            relation, subject=subject or via or method)

    def meaning(model: str, relation: SourceRelation, *, prefix: str = "",
                subject: str = "", where: str = "") -> None:
        attrs = relation.attrs or {}
        name = relation.object
        label = str(attrs.get("string") or _field_label(metas.get(relation.subject), name,
                                                         strings.get(relation.subject, {})))
        sig = (model, "help", prefix, name)
        if sig in seen:
            return
        seen.add(sig)
        add(model, "meaning", f"{prefix}{label}",
            f'{where}Field "{label}" ({name}): {_short(attrs["help"], _MAX_MESSAGE)}',
            relation, subject=subject)

    seen: set[tuple] = set()
    brought: set[tuple] = set()
    def owner_of(attrs: dict) -> str:
        """The model a view element is about: the view's own, or, inside an embedded
        x2many view, the model of those lines."""
        model = str(attrs.get("model") or "")
        within = str(attrs.get("within") or "")
        if within and model in metas:
            relation = str((metas[model].fields.get(within) or {}).get("relation") or "")
            if relation:
                return relation
        return model

    for relation in source_map.relations:
        attrs = relation.attrs or {}
        model = owner_of(attrs) if relation.kind in ("form_target", "form_field") \
            else str(attrs.get("model") or "")
        if relation.kind == "form_target" and attrs.get("label"):
            label = str(attrs["label"])
            conditions = []
            if attrs.get("invisible"):
                conditions.append(f"hidden when {_short(attrs['invisible'], _MAX_CONDITION)}")
            if attrs.get("readonly"):
                conditions.append(f"disabled when {_short(attrs['readonly'], _MAX_CONDITION)}")
            if attrs.get("groups"):
                conditions.append(f"only for members of {_short(attrs['groups'], 120)}")
            target_type = str(attrs.get("target_type") or "object")
            operation = relation.object
            shown, name, declared = window_of(model, relation.object, target_type)
            what = (f"opens {relation.object}" if target_type == "action"
                    else f"calls {relation.object}")
            maybe = target_type == "object" and f"{model}.{relation.object}" in sometimes
            if shown:
                what += (f", which {'may open' if maybe else 'opens'} the "
                         f"{name + ' ' if name else ''}window of {shown}")
            style = str(attrs.get("class") or "")
            look = (" (btn-primary, highlighted)" if "btn-primary" in style
                    else " (btn-secondary)" if "btn-secondary" in style else "")
            key = (model, "button", label, tuple(conditions), what, look,
                   relation.evidence[0].path, relation.evidence[0].start_line)
            if key in seen:
                continue
            seen.add(key)
            text = f'Button "{label}"{look} {what}' + ("; " + "; ".join(conditions) if conditions else "")
            if attrs.get("confirm"):
                text += f'; asks first: "{_short(attrs["confirm"], _MAX_MESSAGE)}"'
            add(model, "visibility" if conditions else "target", label, text, relation,
                subject=operation)
            for declaration in declared:
                # Where the window is declared -- the method's lines that open it, the
                # action record that names it -- each cited on its own.
                sig = (model, "opens", relation.object, declaration.evidence[0].path,
                       declaration.evidence[0].start_line)
                if sig in seen:
                    continue
                seen.add(sig)
                add(model, "target", label,
                    f'{relation.object} (Button "{label}") {"may open" if maybe else "opens"} the '
                    f'{name + " " if name else ""}window of {shown}'
                    + (" as a dialog" if (declaration.attrs or {}).get("target") == "new" else ""),
                    declaration, subject=operation)
            # A window's own rules come along, on the entity whose control opens it.
            if shown and shown != model and (model, shown) not in brought:
                brought.add((model, shown))
                where = f'In the {name or label} window ({shown}): '
                count = 0
                for guard in guards.get(shown, ()):
                    if count >= _MAX_WINDOW_RULES:
                        break
                    refusal(model, guard, prefix=f"{label} › ", subject=operation)
                    count += 1
                for help_relation in helps.get(shown, ()):
                    if count >= _MAX_WINDOW_RULES:
                        break
                    meaning(model, help_relation, prefix=f"{label} › ", subject=operation,
                            where=where)
                    count += 1
        elif relation.kind == "form_field" and any(attrs.get(k) for k in ("readonly", "required", "invisible")):
            if (not attrs.get("readonly") and not attrs.get("required")
                    and str(attrs.get("invisible")).strip() in ("1", "True", "true")):
                # A field the form always hides is bookkeeping, not a rule a person meets.
                continue
            name = relation.object
            label = _field_label(metas.get(model), name, strings.get(model, {}))
            parts = []
            for key, word in (("readonly", "readonly when"), ("required", "required when"),
                              ("invisible", "hidden when")):
                if attrs.get(key):
                    parts.append(f"{word} {_short(attrs[key], _MAX_CONDITION)}")
            # The same condition in another view of the form (its embedded list and its
            # embedded form) is kept with its own lines: the reply cites both.
            sig = (model, "field", name, tuple(parts), relation.evidence[0].path,
                   relation.evidence[0].start_line)
            if sig in seen:
                continue
            seen.add(sig)
            add(model, "editability", label, f'Field "{label}" ({name}): ' + "; ".join(parts), relation)
        elif relation.kind == "service_guard":
            refusal(relation.subject.rpartition(".")[0], relation)
        elif relation.kind == "service_entity" and attrs.get("help"):
            meaning(relation.subject, relation)
    return {m: tuple(rules) for m, rules in out.items()}


__all__ = ["rules_for"]
