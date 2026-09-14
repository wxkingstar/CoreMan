import asyncio

from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.choice_submit import ChoiceSubmitHandler
from coreman.runtime.worker.commands import CommandHandler
from coreman.runtime.worker.cron_handler import CronRunHandler
from coreman.runtime.worker.escalation_media import EscalationMediaHandler
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
            "relay_switch": RelaySwitchHandler(),
        }
    ).run()
)
