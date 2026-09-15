"""chat 处理器按阶段拆包之后的结构约束：兼容入口不变、各阶段都被组合进来。"""

from __future__ import annotations

import inspect

from coreman.runtime.worker import chat, chat_handler
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.classify import ClassifyStage
from coreman.runtime.worker.chat.content import ContentStage
from coreman.runtime.worker.chat.converse import ConverseStage
from coreman.runtime.worker.chat.finalize import FinalizeStage
from coreman.runtime.worker.chat.intake import IntakeStage
from coreman.runtime.worker.chat.opening import OpenStage
from coreman.runtime.worker.chat.session_switch import SessionSwitchStage
from coreman.runtime.worker.choice_submit import ChoiceSubmitHandler

STAGES = (
    IntakeStage,
    SessionSwitchStage,
    OpenStage,
    ContentStage,
    ConverseStage,
    ClassifyStage,
    FinalizeStage,
)


def test_compat_module_reexports_the_same_objects() -> None:
    for name in chat.__all__:
        assert getattr(chat_handler, name) is getattr(chat, name)
    assert chat_handler.tasks.__name__ == "coreman.core.bus.tasks"


def test_handler_composes_every_stage_and_implements_cross_stage_calls() -> None:
    handler = chat.ChatTaskHandler
    assert all(issubclass(handler, stage) for stage in STAGES)
    assert issubclass(ChoiceSubmitHandler, handler)
    # base 里跨阶段调用只是类型声明；运行时必须由某个阶段真正实现。
    for name in ("_sessions", "_classify", "_finalize", "_icon_url", "_push_if_proactive"):
        owner = next(cls for cls in handler.__mro__ if name in vars(cls))
        assert owner in STAGES, (name, owner)
        assert not hasattr(ChatStageBase, name)


def test_each_stage_method_is_defined_exactly_once() -> None:
    seen: dict[str, type] = {}
    for stage in STAGES:
        for name, member in vars(stage).items():
            if inspect.isfunction(member) or isinstance(member, staticmethod):
                assert name not in seen, (name, seen.get(name), stage)
                seen[name] = stage
    assert {"_intake", "_open", "_build_content", "_converse", "_classify", "_finalize"} <= set(
        seen
    )
