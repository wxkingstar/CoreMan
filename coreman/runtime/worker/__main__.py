import asyncio

from coreman.runtime.worker.approval_resume import ApprovalResumeHandler
from coreman.runtime.worker.assistant_approvals import (
    AssistantApprovalExecuteHandler,
    AssistantApprovalResultHandler,
)
from coreman.runtime.worker.assistant_cold_start import AssistantColdStartHandler
from coreman.runtime.worker.assistant_compile import AssistantCompileHandler
from coreman.runtime.worker.assistant_handover import AssistantHandoverHandler
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.choice_submit import ChoiceSubmitHandler
from coreman.runtime.worker.commands import CommandHandler
from coreman.runtime.worker.credential_resume import CredentialResumeHandler
from coreman.runtime.worker.cron_handler import CronRunHandler
from coreman.runtime.worker.escalation_media import EscalationMediaHandler
from coreman.runtime.worker.group_observe import GroupObserveHandler
from coreman.runtime.worker.relay_switch import RelaySwitchHandler
from coreman.runtime.worker.service import WorkerService
from coreman.runtime.worker.skill_install import SkillInstallHandler

asyncio.run(
    WorkerService(
        handlers={
            "command": CommandHandler(),
            "cron_run": CronRunHandler(),
            "skill_install": SkillInstallHandler(),
            "escalation_media": EscalationMediaHandler(),
            "chat": ChatTaskHandler(),
            "card_action": CardActionHandler(),
            "choice_submit": ChoiceSubmitHandler(),
            "credential_resume": CredentialResumeHandler(),
            "approval_resume": ApprovalResumeHandler(),
            "assistant_approval_execute": AssistantApprovalExecuteHandler(),
            "assistant_approval_result": AssistantApprovalResultHandler(),
            "assistant_handover": AssistantHandoverHandler(),
            "assistant_compile": AssistantCompileHandler(),
            "assistant_cold_start": AssistantColdStartHandler(),
            "relay_switch": RelaySwitchHandler(),
            "group_observe": GroupObserveHandler(),
        }
    ).run()
)
