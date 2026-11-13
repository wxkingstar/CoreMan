"""个人助理专用的任务类型名。单独成模块：`feishu_personal.policy` 与 worker 都要引用，不能成环。"""

from __future__ import annotations

# 批准之后由平台执行暂存的写操作：不调模型。
APPROVAL_EXECUTE_KIND = "assistant_approval_execute"
# 把审批单的结局作为一轮投回助理会话。
APPROVAL_RESULT_KIND = "assistant_approval_result"

# 平台自己发起、在助理会话里跑的一轮：没有入站事件，只能经 `platform_turns.enqueue` 入队。
# 盯消息攒下的事项交回主会话。
HANDOVER_KIND = "assistant_handover"
# 子任务或公共员工的结论投回主会话。
JOB_RESULT_KIND = "assistant_job_result"
# 下班时把当天的进展整理进知识库。
COMPILE_KIND = "assistant_compile"
# 子任务自己的一轮（会话键 `<主会话键>#job:<编号>`）。
SUBTASK_KIND = "assistant_subtask"
# 冷启动时让助理从所有者自己发出的消息里归纳口吻样本（会话键 `<主会话键>#job:cold-start`）。
COLD_START_KIND = "assistant_cold_start"
# 调度公共员工：跑在目标员工那边，不是助理自己的轮次，来源校验另定。
BOT_HELP_KIND = "assistant_bot_help"
PLATFORM_TURN_KINDS = (
    HANDOVER_KIND,
    JOB_RESULT_KIND,
    COMPILE_KIND,
    SUBTASK_KIND,
    COLD_START_KIND,
)
# 没有入站事件也能跑的任务类型：平台来源的几种，加上在这样的轮次里提交的审批单的结果回合。
# 不带入站事件的这些类型只有 `platform_turns.enqueue` 能入队（`bus.tasks.enqueue` 把关）。
ORIGIN_KINDS = (*PLATFORM_TURN_KINDS, APPROVAL_RESULT_KIND)


def unattended(kind: str) -> bool:
    """这类任务的一轮是不是无人在场：平台触发，所有者此刻不在对话里，输入可能来自别人。

    只看任务类型，不看有没有入站事件：审批结果回合沿用提交那一轮的入站事件，照样无人在场。
    无人在场的轮次能力要收紧（开轮让路、不挂以所有者身份签发的凭据、授权工具只留查询、
    不得改动置顶页），各处一律按这一个判断，不要各自再写条件。
    """
    return kind in ORIGIN_KINDS
