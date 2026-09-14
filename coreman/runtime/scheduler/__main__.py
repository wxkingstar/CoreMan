import asyncio

from coreman.runtime.scheduler.service import SchedulerService

asyncio.run(SchedulerService().run())
