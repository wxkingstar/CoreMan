"""个人助理专用的任务类型名。单独成模块：`feishu_personal.policy` 与 worker 都要引用，不能成环。"""

from __future__ import annotations

# 批准之后由平台执行暂存的写操作：不调模型。
APPROVAL_EXECUTE_KIND = "assistant_approval_execute"
# 把审批单的结局作为一轮投回助理会话。
APPROVAL_RESULT_KIND = "assistant_approval_result"
