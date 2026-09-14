"""提示词层：system prompt 三明治、请求级环境变量、用户输入消毒（spec §8.3）。

`defaults` 只放字面量、不 import 任何 coreman 模块，`settings_schema` 才能 import 它
拿默认值而不成环（本包的 `__init__` 不碰 settings_schema，同理）。
"""

from coreman.core.prompting.env_vars import build_env, env_keys_for_log
from coreman.core.prompting.sanitize import sanitize_parts, sanitize_user_input
from coreman.core.prompting.system_prompt import (
    PromptSegments,
    Speaker,
    build_system_prompt,
    load_segments,
    speaker_header,
)

__all__ = [
    "PromptSegments",
    "Speaker",
    "build_env",
    "build_system_prompt",
    "env_keys_for_log",
    "load_segments",
    "sanitize_parts",
    "sanitize_user_input",
    "speaker_header",
]
