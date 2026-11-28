"""分类阶段：只看 Outcome 决定 chat_logs / tasks 状态与给用户的终稿。"""

from __future__ import annotations

from dataclasses import replace

from coreman.core.api_tasks.service import SILENT_ASK_CODE, SILENT_ASK_MESSAGE, SILENT_KEY
from coreman.core.chat.rate_limit import (
    RATE_LIMITED_CODE,
    RATE_LIMITED_MESSAGE,
    is_rate_limited,
)
from coreman.core.i18n.messages import msg
from coreman.core.relay.client import (
    IncompleteResultError,
    RelayBusyError,
    RelayInterruptedError,
)
from coreman.core.wecom.cards import question_brief
from coreman.runtime.worker.chat.base import ChatStageBase
from coreman.runtime.worker.chat.models import Outcome, Prepared, Verdict
from coreman.runtime.worker.context import TaskContext


class ClassifyStage(ChatStageBase):
    """流结束分类。"""

    def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        """流结束分类，并在这唯一的产出口过一遍出站密钥闸门。

        终稿会沿四条路出去：`writer.complete`、超时看护的完成/终止通知、`_push_if_proactive`
        的主动推送，以及 `chat_logs`。在这里过闸比在每条路上各加一次可靠——漏掉任何一条，
        一枚还活着的令牌就进了聊天记录。
        """
        verdict = self._classify_outcome(ctx, pre, out)
        if verdict.log_status in ("success", "error") and is_rate_limited(verdict.final_text):
            # 终稿是「额度用完」的告示，不是这一轮该有的结果：按失败收尾，不配完成标记。
            # 驱动按正文回传（成功）和按出错回传的都走到这里，各类轮次不用各自再认一遍。
            verdict = Verdict(
                "error",
                "failed",
                RATE_LIMITED_CODE,
                RATE_LIMITED_MESSAGE,
                verdict.final_text.removesuffix(msg("done_suffix", ctx.locale)),
            )
        if verdict.log_status == "ask_user" and ctx.task.payload.get(SILENT_KEY):
            # 静默执行的业务系统任务没有人能答题：不进 waiting_user、不开选择卡，按失败结束，
            # 题面留在回复里交给业务系统。
            verdict = replace(
                verdict,
                log_status="error",
                task_status="failed",
                error_code=SILENT_ASK_CODE,
                error_message=SILENT_ASK_MESSAGE,
            )
        return replace(verdict, final_text=ctx.redact(verdict.final_text) or "")

    def _classify_outcome(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        """流结束分类。

        relay 自带的错误与零事件流排在通用异常之前：驱动回错后不补 finish chunk、或整条流
        一个事件都没有时，客户端抛的是「未确认终态」，这时要按 relay 的原话 / 空回复分类，
        不能落成一句通用的「连接出现错误」，把原因文本和会话链接一起丢掉。
        """
        locale, text, url = ctx.locale, pre.writer.pending_text, pre.session_url
        if out.cancelled:
            return self._cancelled(ctx, text, out.reason)
        if out.collaboration_handoff:
            return Verdict("success", "succeeded", None, None, "")
        if out.relay_error:
            return Verdict(
                "error",
                "failed",
                "x_relay_error",
                "运行时以正文回传了错误",
                text or msg("relay_error_text", locale),
            )
        unconfirmed = isinstance(out.error, IncompleteResultError)
        if (out.error is None or unconfirmed) and not out.ask_user and out.counted == 0:
            return Verdict(
                "error",
                "failed",
                "empty_stream",
                "运行时未返回任何事件",
                msg("empty_stream", locale, url=url),
            )
        if unconfirmed:
            return self._incomplete(ctx, pre, text)
        if isinstance(out.error, RelayBusyError):
            # 节点满载排队超时：还没开始执行，告诉用户是「忙」而不是「连接出错」。
            return Verdict(
                "error",
                "failed",
                "runtime_busy",
                f"RelayBusyError: {out.error}",
                msg("runtime_busy", locale, relay=pre.relay_label or pre.relay.name),
            )
        if isinstance(out.error, RelayInterruptedError):
            detail = msg("runtime_control_lost", locale) if out.error.code == "control_lost" else ""
            error_message = f"RelayInterruptedError: {out.error}"
            final_text = msg("run_interrupted", locale, relay=pre.relay_label or pre.relay.name)
            if detail:
                error_message += f"; {detail}"
            return Verdict(
                "error",
                "failed",
                "run_interrupted",
                error_message,
                final_text,
            )
        if out.error is not None:
            name = type(out.error).__name__
            return Verdict(
                "error",
                "failed",
                name,
                f"{name}: {out.error}",
                msg("relay_error", locale, relay=pre.relay_label or pre.relay.name),
            )
        if out.ask_user:
            if not out.questions:
                # 问卷解析出来是空的：这一轮什么也问不出来，按失败收尾让用户换个说法。
                return Verdict(
                    "error",
                    "failed",
                    "ask_user_invalid",
                    "AskUserQuestion 参数无效",
                    text
                    + msg("ask_user_invalid", locale)
                    + msg("session_link_suffix", locale, url=url),
                )
            brief = question_brief(
                out.questions[0], index=0, total=len(out.questions), locale=locale
            )
            # 终稿带上首题的完整说明：卡片上的选项被截到 11 个字，只看卡片选不明白。
            return Verdict(
                "ask_user", "succeeded", None, None, f"{text}\n\n{brief}" if text else brief
            )
        if out.finish_reason not in {"stop", "end_turn", "completed"}:
            return self._incomplete(ctx, pre, text)
        if out.background and not out.live_joined:
            # 主回合结束、后台任务还在跑：不配 ✅，写明后台在做、做完逐条回复。
            from coreman.core.chat import background_runs

            n = len(background_runs.running(out.background_tasks))
            return Verdict(
                "success",
                "succeeded",
                None,
                None,
                text + msg("background_started_suffix", locale, n=n),
            )
        # 本人接着发的一轮、后台还有任务在跑：这一轮答完了，事情还没做完，不配 ✅。
        still_running = self._live_running_suffix(pre, out, locale)
        if out.live_joined:
            # 驱动在这条流里报了后台任务在这一轮里完成：CLI 多半把完成通知并进了这一轮（也可能
            # 另起一轮，由观察任务单独发），所以只说「可能」。
            from coreman.core.chat import background_runs

            merged = background_runs.finished_since(out.live_join_tasks, out.background_tasks)
            if merged and out.text_events:
                which = "、".join(background_runs.number(i) for i, _ in merged)
                note = msg("live_merged_note", locale, which=which)
                return Verdict(
                    "success",
                    "succeeded",
                    None,
                    None,
                    text + note + (still_running or msg("done_suffix", locale)),
                )
        if out.text_events == 0:
            body = (
                msg("no_text_with_tools", locale, n=out.tool_events, url=url)
                if out.tool_events
                else self._empty_success_text(ctx)
            )
            return Verdict("success", "succeeded", None, None, text + body + still_running)
        return Verdict(
            "success",
            "succeeded",
            None,
            None,
            text + (still_running or msg("done_suffix", locale)),
        )

    def _live_running_suffix(self, pre: Prepared, out: Outcome, locale: str) -> str:
        """本人接着发的一轮收尾时后台还在跑的项：写明还有几项、做完回复在哪条消息下。

        没有在跑的（不是这类轮次，或后台任务都在这一轮里结束了）返回空串，照常收尾。
        """
        if not out.live_joined:
            return ""
        from coreman.core.chat import background_runs

        n = len(background_runs.running(out.background_tasks))
        if not n:
            return ""
        if pre.live_origin:
            return msg("live_running_suffix", locale, n=n, origin=pre.live_origin)
        return msg("live_running_suffix_plain", locale, n=n)

    def _incomplete(self, ctx: TaskContext, pre: Prepared, text: str) -> Verdict:
        """没拿到确认终态：已经流出的正文照留（驱动常把失败原因写在里面），后面接一句错误说明。"""
        notice = msg("relay_error", ctx.locale, relay=pre.relay_label or pre.relay.name)
        return Verdict(
            "error",
            "failed",
            "incomplete_result",
            "未确认执行完成",
            f"{text.rstrip()}\n\n{notice}" if text.strip() else notice,
        )

    def _empty_success_text(self, ctx: TaskContext) -> str:
        """一个字也没说、一个工具也没调，却是正常收尾时给用户的交代。"""
        return msg("no_text_no_tools", ctx.locale)

    def _cancelled(self, ctx: TaskContext, text: str, reason: str | None) -> Verdict:
        if reason == "collaboration_budget_exhausted":
            notice = "本轮调用已达到安全上限，为避免重复消耗已停止。尚未确认任务完成。"
            return Verdict(
                "stopped",
                "cancelled",
                reason,
                "协作调用达到安全上限",
                # 已经写出的进度照留，群里才看得出停在哪一步。
                f"{text.rstrip()}\n\n{notice}" if text.strip() else notice,
            )
        if reason == "user_stop":
            return Verdict(
                "stopped",
                "cancelled",
                "user_stop",
                "用户主动停止",
                text + msg("task_stopped_suffix", ctx.locale),
            )
        if reason == "superseded":
            return Verdict(
                "stopped",
                "cancelled",
                "superseded",
                "已被新消息替代",
                text + msg("superseded_suffix", ctx.locale),
            )
        return Verdict(
            "timeout",
            # 硬 TTL 是平台主动掐的超时，任务态要与「用户取消」分开，运营才看得出差别。
            "timed_out" if reason == "hard_ttl" else "cancelled",
            reason or "cancelled",
            "任务被取消（超时）",
            text or msg("worker_lost", ctx.locale),
        )
