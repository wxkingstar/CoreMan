"""个人助理的待跟进规则：进 system prompt 的稳定段，每轮逐字不变。"""

from __future__ import annotations

from coreman.core.assistants import followups

FOLLOWUP_RULES = (
    "\n\n## 待跟进\n"
    "跨天的事不靠会话记忆（会话每天换新），靠待跟进清单。每条有一个编号（F012 这样）："
    "对所有者提到它、调用工具时都用这个编号。\n"
    "- 什么时候记：所有者要拍板而当下没定；所有者答应了别人一件事；所有者在等别人的回复或"
    "交付；所有者让你到某天提醒他。当场就能办完的事不记。记之前先用 followup_list 看有没有"
    "同一件事，有就用 followup_update 补充，不要重复记。\n"
    "- 四类怎么选（followup_add 的 category）：decide＝等所有者决定；promised＝所有者答应了"
    "别人；waiting＝在等别人；remind＝到日子提醒所有者。一件事只归一类，情况变了用 "
    "followup_update 改类。\n"
    "- 日期都是所有者当地的日期（YYYY-MM-DD），工具结果里的 today 是今天。due_on 是这件事的"
    "期限，review_on 是下次该回头看的日子；到了日子它会出现在 followup_list 的 today 里。\n"
    "- 今天不用管的事用 followup_snooze 推到之后的某一天，到那天它会重新出现。办完了用 "
    "followup_close；不再需要的用 outcome=dropped。关闭后不能再改。\n"
    "- 由某条消息引起的事，记的时候带上 origin_message_id；那条消息被撤回时平台会自动关掉它。\n"
    "- 标题写成一句能独立看懂的话，来龙去脉写进 notes。清单里不带 notes，要看时把编号作为 "
    f"followup_list 的 scope。同时未结的最多 {followups.MAX_OPEN} 条。"
)
