"""别人写的字进提示词时的引用：逐行加「> 」前缀，换掉平台用来做块标记的方括号。

知识库的页面、交回事项的摘录、子任务的结论都可能带着别人的话。不加前缀的话，里面自己写一行
块结束标记、后面再跟一行「平台规则」，读起来就像平台说的。各处共用这一个实现：换行的认法
不能有的地方宽、有的地方窄。
"""

from __future__ import annotations

from typing import Any

from coreman.core.prompting.sanitize import sanitize_user_input

QUOTE = "> "
# 块标记与条目编号用的方括号：引文里的换成形近的另一种，「【…】」就只有平台写的。
BRACKETS = str.maketrans("【】", "〔〕")


def quoted(text: Any) -> str:
    """一段文字作为引用：各种换行统一后逐行加前缀，丢掉冒充平台标签的行，换掉方括号。

    换行按 `str.splitlines` 认：回车、行分隔符（U+2028）、段分隔符（U+2029）、下一行（U+0085）
    等在终端和模型眼里都是换行，只按 `\\n` 分的话它们后面的内容就成了没有前缀的一行。
    """
    body = "\n".join(str(text or "").splitlines())
    lines = sanitize_user_input(body).strip().translate(BRACKETS).split("\n")
    return "\n".join((QUOTE + item).rstrip() for item in lines)
