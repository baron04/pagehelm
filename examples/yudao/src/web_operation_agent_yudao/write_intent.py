"""Yudao's current-turn wording policy for Skill write availability.

This is an application convention, not write authorization. Every admitted Skill
operation still needs the ordinary effect projection and fresh user approval.
"""
from dataclasses import dataclass
import re


_DRAFT = re.compile(
    r"先填|只填|预填|填好|填上|填成|填一下|草稿|准备给|"
    r"(?:不(?:要|用|必|需要|能|得|许)?|无(?:需|须)|(?<!分)(?<!特)别|请勿|切勿|暂不)"
    r"(?:再|直接|立即|马上|现在|就)?(?:提交|保存)"
    r"|(?:(?<![帮替给为])我(?:来|再|自己(?:来|再)?)|由我(?:来|再)?|自己(?:来|再)?)(?:提交|保存)"
)
_COMMIT_AFTER_DRAFT = re.compile(
    r"(?:先填|预填|填好|填上|填成|草稿).{0,80}?"
    r"(?:再|然后|随后|接着|并|之后|后)(?:由你|帮我|直接|立即|马上|现在|就)?(?:提交|保存)"
)
_SAVE = re.compile(
    r"新建|新增|创建|添加|开一张|改成|改为|换成|更换|调成|设为|清空|记一条|补条|记好|"
    r"写一条跟进|审批|审核|删除|关闭|启用|停用|分配|领取|放入公海|提交|保存"
)
_NEGATIVE_PREFIX = re.compile(r"(?:不(?:要|用|必|需要)?|别|请勿|切勿|无(?:需|须)|暂不)$")
_QUESTION_PREFIX = re.compile(r"(?:怎么|如何|怎样|能否|能不能|是否|为什么|几个|多少)$")
_DRAFT_FOLLOWUP = re.compile(r"改|调|填|补|清空|换|设为|加上|删掉")
_DRAFT_STATUS_SUFFIX = re.compile(r"状态|[)）、，,]")


@dataclass(frozen=True)
class WriteIntent:
    mode: str
    phrase: str = ""


def classify_write_intent(text: str, *, prior_draft: bool = False) -> WriteIntent:
    """Return draft, save, or neutral; ambiguity never grants write authority."""
    compact = re.sub(r"\s+", "", text or "")
    draft = next((match for match in _DRAFT.finditer(compact)
                  if match.group() != "草稿" or not (
                      compact[max(0, match.start() - 1):match.start()] in {"(", "（"}
                      or _DRAFT_STATUS_SUFFIX.match(compact[match.end():]))), None)
    commit = _COMMIT_AFTER_DRAFT.search(compact)
    if draft and not commit:
        return WriteIntent("draft", draft.group())
    if (prior_draft and _DRAFT_FOLLOWUP.search(compact)
            and not re.search(r"提交|保存|新建|新增|创建", compact)):
        return WriteIntent("draft", "沿用上一轮草稿")
    for match in _SAVE.finditer(compact):
        prefix = compact[max(0, match.start() - 4):match.start()]
        suffix = compact[match.end():match.end() + 6]
        if (_NEGATIVE_PREFIX.search(prefix) or _QUESTION_PREFIX.search(prefix)
                or match.group() in {"换成", "更换"} and (
                    prefix.endswith(("已", "已经", "可以", "能否"))
                    or re.match(r"(?:后|过|了?(?:吗|么))", suffix))
                or match.group() in {"提交", "保存"} and (
                    prefix.endswith(("已", "未", "没"))
                    or re.match(r"(?:的|状态|了?(?:几|多少|哪些))", suffix))
                or match.group() in {"新建", "新增", "创建"} and (
                    prefix.endswith("打开") or re.match(r"了?(?:几|多少|哪些|什么)", suffix))
                or match.group() in {"审核", "审批", "关闭", "启用", "停用"}
                   and (prefix.endswith("未") or re.match(r"(?:的|状态|中|[（(]草稿[)）]|了?吗|了?么|了吗|吗|么)", suffix))):
            continue
        return WriteIntent("save", match.group())
    return WriteIntent("neutral")


def preceding_draft_handoff(turns) -> bool:
    """Only a completed form handoff carries draft intent into the next turn."""
    active = False
    for turn in turns or ():
        active = (turn.get("status") == "finished"
                  and "请检查表单" in str(turn.get("assistant") or "")
                  and classify_write_intent(str(turn.get("user") or ""),
                                            prior_draft=active).mode == "draft")
    return active
