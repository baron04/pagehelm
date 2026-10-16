"""Local deployment diagnostics, shared by Python composition and the CLI.

Reading reviewed files and matching their release precedes model configuration and
all proxy/network startup. These checks grant no action or business-write authority.
"""
from dataclasses import dataclass
import hashlib
import json

from .model_page_policy import NativePageReview
from .money_review import VerifiedMoneyReview
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
    money_review: VerifiedMoneyReview | None = None

    @property
    def policy(self): return json.loads(self.policy_json) if self.policy_json else None


def validate_native_deployment(config):
    if config.native_read_policy is None:
        raise ValueError("the native host requires --native-read-policy in v2 method-fence mode")
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
    if (page_review.fence_digest and hashlib.sha256(config.native_read_policy.read_bytes()).hexdigest()
            != page_review.fence_digest):
        raise ValueError("native page catalog was generated for a different method fence")
    money_review = None
    money_path = getattr(config, "native_money_policy", None)
    if money_path is not None:
        if config.ui_source is None:
            raise ValueError("verified money policy requires a deployment UI source")
        from .ui_source import UiSource
        ui = UiSource.read(config.ui_source)
        money_review = VerifiedMoneyReview.load(money_path, ui_source=config.ui_source,
                                                release=config.source_release, ui=ui)
    return ValidatedNativeDeployment(json.dumps(policy), True, page_review, money_review)
