"""自主玩手机的行为流程与分享冷却文案（纯标准库，便于单测与复用）。

流程口径：行动由模型自己的兴趣驱动，先决定点不点开、要不要继续；中途不感兴趣
直接退出，不为了分享硬看完；看完/玩完后再单独判断会话是否也可能感兴趣，值得才
分享或评论/点赞（分享与互动可以都做，没有先后）。分享按目标会话冷却，冷却时长
由 ``auto.share_cooldown_minutes`` 配置。
"""

from __future__ import annotations

from .share_policy import normalize_share_cooldown_minutes

FLOW_RULE = (
    "行动由你自己的兴趣驱动：先按人格、心情与好奇心决定要不要打开、要不要继续，"
    "不是因为「会话可能感兴趣」才去刷；感兴趣就继续，中途不感兴趣直接退出换内容"
    "（不用为了分享硬看完）；看完/玩完后再单独判断会话是否也可能感兴趣：值得就分享，"
    "想互动就评论/点赞。分享与互动可以都做、没有先后，不分享也正常。"
)

_SHARE_BASE = (
    "看和分享是两次独立判断：先决定是否点开/继续看（感兴趣只说明值得看，不触发分享），"
    "看完后再单独判断是否值得分享；只有确实值得对方看才 phone_share_to_user，且必须带内容链接或截图"
    "（链接优先：App 分享按钮或 phone_traffic 的「可分享链接」；"
    "拿不到链接必须 include_screenshot=true），禁止纯文字；同一内容只发一次、"
    "每轮不超过 3 条，不刷屏。分享目标先 phone_recent_chat 看最近聊天，"
    "再按对方喜好自己选，不要用固定目标。"
)


def share_cooldown_text(minutes: object) -> str:
    """把冷却分钟数翻译成提示词片段（0/非法=不限）。"""
    value = normalize_share_cooldown_minutes(minutes)
    if value <= 0:
        return "当前未设置目标会话分享冷却；仍要同一内容只发一次、每轮≤3 条。"
    return (
        f"同一个群/人两次主动分享至少间隔 {value} 分钟"
        "（配置 auto.share_cooldown_minutes）；冷却内工具会拒绝，不要反复尝试。"
    )


def share_rule_text(minutes: object = None) -> str:
    """组合「看/分享两重判断」与目标会话冷却规则。"""
    if minutes is None:
        return _SHARE_BASE
    return _SHARE_BASE + share_cooldown_text(minutes)


BREAK_RULE_FLOW = (
    "行动按你自己的心情、好奇心与真实意愿决定：即使暂时不感兴趣或已经看腻，"
    "也可以点开、继续看完；直接打开贴吧、小黑盒、抖音或快手，安静看直播、刷帖或滑动。"
    "按需用 phone_screen 查看页面、phone_watch_media 分段观看，phone_content 是可选的精读工具。"
    "可以直接续看，无需逐篇 begin/read/decide 或为休闲强行写学习记录；"
    "无需编造兴趣或对用户有用的理由。看和分享分别决定，想独自看完也可以。"
)


def break_rule_hint(enabled: bool) -> str:
    """运行时规则弹性的范围与执行边界。"""
    if not enabled:
        return ""
    return (
        "【break_rule 已开启】" + BREAK_RULE_FLOW
        + "本次任务 token 预算可自主选择延展最多 20%，到执行上限后收尾；"
        "原预算为 0 时沿用不限单次预算。兴趣与新鲜感阈值仅作参考，曝光和用量照常记录。"
        "滚动总额度、互动开关/限额、分享目标/时段/冷却、金融应用限制、"
        "用户优先、取消、暂停与执行期限继续生效。"
    )


def flow_rule_text(enabled: bool = False) -> str:
    """当前兴趣决策规则。"""
    return BREAK_RULE_FLOW if enabled else FLOW_RULE


CONTENT_FLOW = (
    "每篇内容独立任务：图文点开前 phone_content begin；视频打开并确认起点后begin，"
    "read先理解正文或试看。感兴趣再decide continue并read续看。看评论前独立decide "
    "read_comments；想回复具体已读评论再decide reply_comment并申请互动名额，"
    "看评论不等于回复。点赞收藏可选且遵守权限，结束finish后再换内容。"
)

DIRECT_BROWSE_FLOW = (
    "自由浏览：直接 phone_open_app 打开应用，phone_screen 看屏幕，phone_tap/phone_swipe 操作；"
    "需要声音或动态内容时 phone_watch_media 按游标分段观看，可直接继续调用。"
    "phone_content 精读流程与 phone_learn 按需使用，安静休闲也可以；"
    "只描述实际看到或听到的内容，互动仍先申请名额。"
)


def content_rule_text(enabled: bool = False) -> str:
    return DIRECT_BROWSE_FLOW if enabled else CONTENT_FLOW
