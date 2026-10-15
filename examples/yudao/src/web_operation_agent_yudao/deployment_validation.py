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
    if config.native_read_policy is None:
        raise ValueError("the native host requires --native-read-policy in v2 method-fence mode")
    if not config.native_live_forms:
        raise ValueError("the native host requires live forms and the application's own read-only window")
    if config.native_page_policy is None:
        raise ValueError("the native host requires --native-page-policy with release-reviewed page handlers")
    policy = _document(config.native_read_policy, "native read")
    if (not isinstance(policy, dict)
            or set(policy) != {"schema_version", "release", "mode", "writing_gets", "scan"}
            or policy["schema_version"] != 2 or policy["mode"] != "method-fence"
            or not config.source_release or policy["release"] != config.source_release
            or not isinstance(policy["writing_gets"], list)):
        raise ValueError("the v2 method fence policy must match the deployment release")
    NativePageGuard(frozenset(), writing_gets=frozenset(policy["writing_gets"]))
    page_review = NativePageReview.load(_document(config.native_page_policy, "native page"), config.source_release)
    return ValidatedNativeDeployment(json.dumps(policy), True, page_review)
