"""Same-actor choices for the two reviewed native read contracts; no source URLs run."""
import re

from web_operation_agent.business.forms import FormChoice
from web_operation_agent.business.errors import (
    BusinessToolRequestError, BusinessToolAuthenticationError,
    BusinessToolPermissionError, BusinessToolUnavailableError)
from ..reads.client import YudaoReadRefused


class NativeFormChoices:
    def __init__(self, verifier):
        self.verifier = verifier

    def __call__(self, fact, state, context):
        verifier = getattr(self.verifier, "active", self.verifier)
        if verifier is None or not self.verifier.validate(state, context=context):
            raise BusinessToolRequestError("the form choice actor or entry changed")
        try:
            if fact.choice_source == "dictionary":
                # This fixed read contract matches getIntDictOptions' reviewed primitive
                # conversion. Do not infer URLs, privileges or arbitrary calls from source.
                raw = verifier.client.get("system/dict-data/simple-list")
                if not isinstance(raw, list):
                    return ()
                rows = [r for r in raw if isinstance(r, dict) and r.get("dictType") == fact.choice_reference]
                pairs = [(r.get("value"), r.get("label")) for r in rows]
            elif fact.choice_source == "users":
                rows = verifier.client.get("system/user/simple-list")
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    return ()
                pairs = [(r.get("id"), r.get("nickname")) for r in rows]
            else:
                pairs = fact.choices
            if not 1 <= len(pairs) <= 50:
                return ()
            result = []
            for value, label in pairs:
                if fact.choice_source in {"dictionary", "users"}:
                    if isinstance(value, bool) or not re.fullmatch(r"(?:0|[1-9][0-9]*)", str(value)):
                        return ()
                    if int(value) > 2**53 - 1:
                        return ()
                    value = str(int(value))
                result.append(FormChoice(value, label))
            if (len({r.value for r in result}) != len(result) or len({r.label for r in result}) != len(result)
                    or not self.verifier.validate(state, context=context)):
                return ()
            return tuple(result)
        except YudaoReadRefused as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError("the form choice credential expired") from exc
            if exc.permission_denied:
                raise BusinessToolPermissionError("the application refused the form choice read") from exc
            raise BusinessToolUnavailableError("the application form choice read failed") from exc
        except (OSError, TimeoutError) as exc:
            raise BusinessToolUnavailableError("the application form choice read failed") from exc
        except (ValueError, TypeError):
            return ()
