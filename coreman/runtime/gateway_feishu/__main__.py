import asyncio

from coreman.runtime.gateway_feishu.service import GatewayFeishuService

asyncio.run(GatewayFeishuService().run())
