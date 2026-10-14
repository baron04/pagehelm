"""Local deployment diagnostics, shared by Python composition and the CLI.

Reading reviewed files and matching their release precedes model configuration and
all proxy/network startup. These checks grant no action or business-write authority.
"""
from dataclasses import dataclass
import json

from .model_page_policy import NativePageReview
from .page_guard import NativePageGuard


def _document(path, name):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the {name} policy JSON") from exc


@dataclass(frozen=True)
class ValidatedNativeDeployment:
    policy_json: str | None
    live_forms: bool
    page_review: NativePageReview | None

    @property
    def policy(self): return json.loads(self.policy_json) if self.policy_json else None


def validate_native_deployment(config):
    policy = None
    page_review = None
    if config.executor_mode == "slim":
        if config.native_read_policy is None:
            raise ValueError("slim requires --native-read-policy; select --executor goal for the legacy configuration")
        if (config.native_form_policy is not None or config.native_create_policies
                or config.native_create_class is not None or config.native_filter_policies
                or config.native_action_effect_review):
            raise ValueError("slim cannot combine legacy form/create/filter/action review policies; use --native-page-policy")
        if not config.native_live_forms:
            raise ValueError("slim requires native live forms and the application's own read-only window")
        if config.native_page_policy is None:
            raise ValueError("slim requires --native-page-policy with release-reviewed page handlers")
    elif config.native_page_policy is not None:
        raise ValueError("native page policy belongs to the slim executor")
    fenced = False
    if config.native_read_policy is not None:
        policy = _document(config.native_read_policy, "native read")
        fenced = isinstance(policy, dict) and policy.get("schema_version") == 2
        if fenced:
            if (set(policy) != {"schema_version", "release", "mode", "writing_gets", "scan"}
                    or policy["mode"] != "method-fence" or not config.source_release
                    or policy["release"] != config.source_release or not isinstance(policy["writing_gets"], list)
                    or config.native_filter_policies or config.native_action_effect_review):
                raise ValueError("the method fence policy must match the deployment release")
            NativePageGuard(frozenset(), writing_gets=frozenset(policy["writing_gets"]))
        else:
            if (not isinstance(policy, dict) or set(policy) != {"schema_version", "release", "read_paths"}
                    or policy["schema_version"] != 1 or not config.source_release
                    or policy["release"] != config.source_release or not config.ui_source
                    or not isinstance(policy["read_paths"], list)):
                raise ValueError("native read policy must match the deployment and its UI routes")
            NativePageGuard(frozenset(policy["read_paths"]))
    live = (config.native_live_forms and fenced and config.executor_mode in {"goal", "slim"}
            and config.native_form_policy is None and not config.native_create_policies
            and config.native_create_class is None)
    if config.executor_mode == "slim":
        if not live:
            raise ValueError("slim requires the v2 method fence and the application's own read-only window")
        page_review = NativePageReview.load(_document(config.native_page_policy, "native page"), config.source_release)
    return ValidatedNativeDeployment(json.dumps(policy) if policy is not None else None, live, page_review)
