"""台账用例的样本模块：平台来源的类型换个名字导入，取值不变。"""

from coreman.core.assistants import kinds
from coreman.core.assistants.kinds import HANDOVER_KIND as RENAMED

__all__ = ["RENAMED", "kinds"]
