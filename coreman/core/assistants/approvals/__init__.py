"""个人助理的审批闸门。停用流程（1F）与 MCP 入口从这里取用。"""

from coreman.core.assistants.approvals.service import submit, withdraw_pending

__all__ = ["submit", "withdraw_pending"]
