"""yudao (ruoyi-vue-pro) integration for web-operation-agent: goal data only.

This distribution owns everything yudao-specific: the admin-API client, the schema
compiled from the account's menus, the API documentation and the dictionaries, and the
optional reader of the admin UI's source. The core resolves; the core package imports
none of it.
"""
from .client import YudaoAccount, YudaoClient
from .goal import YudaoGoalData
from .ui_source import UiSource

__version__ = "0.2.0"

__all__ = ["UiSource", "YudaoAccount", "YudaoClient", "YudaoGoalData"]
