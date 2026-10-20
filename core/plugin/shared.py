"""插件共享常量与提示词（供入口 main.py 与运行逻辑 mixin 共用）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

from ..plugin_shared import (
    _INBOX_EXCLUDE_WORDS,
    _INBOX_KEYWORDS,
    _format_time,
    _llm_response_text,
    ensure_executable,
)
from ..share_policy import share_window_text


def plugin_root_dir() -> Path:
    """插件根目录（core/plugin/shared.py 向上两级）。"""
    return Path(__file__).resolve().parents[2]



PLUGIN_NAME = "astrbot_phone_control"


BUNDLED_ADB = Path("bin") / "linux-x86_64" / "adb"


PHONE_KEYWORDS = (
    "手机",
    "截图",
    "点击",
    "点一下",
    "滑动",
    "打开",
    "评论",
    "发帖",
    "弹幕",
    "b站",
    "抖音",
    "小黑盒",
    "后台任务",
    "玩手机",
    "验证码",
    "quota",
    "视频",
    "直播",
    "音频",
    "声音",
)


_PHONE_CONTROL_TOOLS = frozenset(
    {
        "phone_tap",
        "phone_long_press",
        "phone_swipe",
        "phone_input_text",
        "phone_key",
        "phone_open_app",
        "phone_browse",
        "phone_watch_media",
    }
)


_GUIDANCE = (
    "【手机操控能力】30 个 phone_* 工具；先 phone_skill 读手册。\n"
    "回复：默认 1–2 句、≤80 字；任务结果只给 1–3 个要点，不复述过程/工具输出"
    "（用户要求详情除外）。\n"
    "静默：仅主人可用；后台任务静默，完成后一次性汇报。\n"
    "额度/频率默认不限制（在配置 quota/互动限额里显式填正数才生效）。\n"
    "App 记忆：操作 App 前先 phone_app_memory 查经验并按经验操作；界面与经验不符说明"
    " App 已改版，先重新探索，再用 phone_app_remember 更新该页面经验。\n"
    "任务优先：用户手机任务优先于 AI 自刷/随机/后台任务；自刷被暂停时立即收尾让路。\n"
    "看与分享：看和分享是两次独立判断——先决定是否点开/继续看（感兴趣只说明值得看，"
    "不触发分享），看完后再单独判断是否值得分享；只有确实值得对方看才 phone_share_to_user，"
    "且必须带内容链接或截图（链接优先：App 分享按钮或 phone_traffic 的「可分享链接」；"
    "拿不到链接必须 include_screenshot=true），禁止只发文字；分享目标先 phone_recent_chat "
    "看最近聊天再按对方喜好选择。\n"
    "新鲜感：同类内容刷多了会看腻（novelty.* 可配，阈值 0=关闭）；提示词会列出已看腻/"
    "接近阈值的主题，刷到同类内容时降低打开/停留概率，优先换主题或换 App；一段时间不看"
    "会自然恢复。\n"
    "页面内容识别：优先 phone_screen（抓包可用时自动附带当前前台 App 的接口内容，"
    "已按最近手机操作与时效过滤）；图文、视频画面、布局等视觉信息看截图/OCR；"
    "抓包不可用、不属于当前 App 或已过期时自动回退屏幕识别；需要原始流量明细或"
    "可分享链接再用 phone_traffic。\n"
    "流媒体内容识别：视频/直播/音乐只有标题和评论不算看懂；刷到值得理解的内容（不是每条）"
    "用一次 phone_watch_media 录一小段（默认 15–30 秒，上限按配置）——它会转写声音并描述关键画面。"
    "识别后把听到/看到写进汇报或 phone_learn；同一段内容不要反复识别；直播间按需限时；"
    "无音频时如实说明只用画面。\n"
    "验证：solver/bypass/普通写操作共享自动尝试次数与总时限（配置 verification.*）；"
    "达到上限会暂停手机并通知用户协助，不要继续刷验证页；收到暂停提示立即收尾。\n"
    "评论/回复：评论区与被动回复一开始必须用 UI 操作（先确认回复对象/楼层/用户，"
    "用 phone_tap/input_text 完成）；不要刚上手就用抓包记录直接调接口发回复。"
    "等熟悉该 App 的回复流程、并积累足够抓包记录后，才可参考之前抓包的请求格式逐步尝试用 API 发送。\n"
    "互动：主动评论=刷帖/视频时自己有感触才发（kind=comment）；被动回复=进入 App 先 "
    "phone_check_inbox 看「回复我的/互动消息」，读内容后按心情与内容决定是否回"
    "（kind=passive_reply），不想回就不回。两者都要先 gate 再 record。\n"
    "多步任务先 phone_start_background_task；控制优先 scrcpy（仅控制通道，低开销），"
    "失败回退 adb；实时/验证码用 phone_live_screen / phone_read_screen 按需取帧。\n"
    "人机验证/做题：优先 phone_solve_verification（一次调用内部两次视觉模型：第一次识图+"
    "判断是否人机验证/做题，第二次重新截图给操作顺序+最终确认点并自动执行）；需要手动精细"
    "控制再用 phone_bypass_verify（capture→drag/tap_points）；答题/测试页不要反复 phone_wait/"
    "phone_screen 干等。\n"
    "学习：持续刷手机每轮结束用 phone_learn 记录 1–2 条真正学到的内容。\n"
    "收尾：玩完/任务结束插件会自动回桌面并清后台，无需手动调 phone_clean_background；"
    "用户 /phone stop 可停止后台/持续刷任务。"
)


def _companion_plugin_available() -> bool:
    """检测「我会永远陪着你」陪伴插件是否可用（用于心情来源降级）。"""
    try:
        return (Path(get_astrbot_plugin_path()) / "astrbot_plugin_private_companion").is_dir()
    except Exception:  # noqa: BLE001 - 检测失败按不可用降级
        return False


def _build_guidance(config: PhoneConfig | None = None) -> str:
    """生成手机能力指南（心情/互动提示按运行时条件追加，保持简短）。"""
    if _companion_plugin_available():
        mood = "心情：按陪伴插件注入的状态表达，不暴露数值。"
    else:
        mood = "心情：按人格与最近记忆推断，不编造数值。"
    parts = [_GUIDANCE, mood]
    if config is not None:
        auto = config.auto
        window = share_window_text(
            start_hour=auto.share_start_hour,
            end_hour=auto.share_end_hour,
            tz_name=auto.share_timezone,
        )
        quiet = bool(auto.share_quiet_hours_enabled)
        parts.append(
            "分享：看和分享分开判断——先决定要不要点开/继续看，看完后再单独判断是否值得分享，"
            "不要因为感兴趣就同时看+分享；分享必须带链接或截图（链接优先：App 分享按钮，或从 "
            "phone_traffic 的「可分享链接」取 link；拿不到链接用 include_screenshot=true 截图），"
            "禁止纯文字；同一内容只发一次、短时间别刷屏。目标由你根据 phone_recent_chat 的最近"
            "聊天内容自己判断（群聊按聊天内容，私聊只分享给最近"
            f"有交流的用户，{auto.share_user_recent_hours} 小时内聊过），不要用固定目标；"
            + (
                f"分享时段 {window}，其余时间为免打扰，自刷任务不主动分享。"
                if quiet
                else "已关闭分享免打扰。"
            )
        )
    security = config.security if config is not None else None
    if security is not None and (security.allow_comments or security.allow_posts):
        parts.append(
            "互动：主人开启评论/发帖后可 phone_interaction_gate 申请 1 次，"
            "自己完成后 record，每轮最多 1 条。"
        )
    return "\n".join(parts)


_HELP_TEXT = """手机操控插件指令：
/phone help           查看本帮助
/phone status         查看设备连接、前台 App、电量与分辨率
/phone connect        重新连接 ADB 设备
/phone screenshot     截图并发送到当前会话
/phone open <app>     打开任意已安装 App（中文名/别名/包名）
/phone browse [app] [条数]  浏览任意已安装 App 的信息流并记入记忆
/phone diary [条数|关键词]  查看/检索浏览日记
/phone auto on <app[,app]> [cron]  创建定时自主浏览任务（cron 默认取配置）
/phone auto off [job_id]           取消自主浏览任务
/phone auto list                   列出自主浏览任务
/phone quota [audit [小时]]       查看 token 额度与对账明细（audit=对照 AstrBot 实际 usage）
/phone play [tokens]              立即静默玩手机一轮（可指定本轮 token 预算）
/phone interactions [n]           查看今日主动评论/被动回复/发帖次数、上限与最近记录
/phone continuous on|off|status [间隔秒] [token预算]  持续刷手机（一轮接一轮，可学习内容）
/phone stop [job_id|all]          停止本插件后台/持续刷任务，回桌面并清理后台
/phone learn [关键词|条数]         查看 AI 在手机上学到的内容档案
/phone traffic [status|tail [n]|on|off]  查看抓包状态/最近流量，或开关手机流量抓包
/phone verify [status|resume <id>|cancel <id>]  查看/恢复/取消验证等待（人工协助）
/phone watch [秒数] [问题]        识别当前正在播放的视频/直播/音乐（声音转写+关键画面）
/phone novelty [status|reset]     查看内容新鲜感/厌倦感状态（阈值、疲劳主题）或清空状态
/phone task <instruction>         把多步任务转入后台静默执行（完成后主动汇报）
/phone tasks                      列出本插件创建的后台任务
adb.host / adb.port 为必填项：未填写时插件无法启动，请先在插件配置「ADB 连接」填写后重载。
可在对话中直接让我使用 phone_* 工具操作手机。"""


def _read_plugin_version() -> str:
    """从 metadata.yaml 读取版本号（失败时回退空串）。"""
    try:
        text = (
            plugin_root_dir() / "metadata.yaml"
        ).read_text(encoding="utf-8")
        match = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
        return match.group(1) if match else ""
    except OSError:
        return ""


class _AutonomousDeviceEvent:
    """降级调度回调使用的合成事件：按 cron 自主任务参与设备门禁。"""

    def __init__(self, umo: str = "") -> None:
        self.unified_msg_origin = str(umo or "")
        self._extras: dict[str, Any] = {
            "cron_payload": {"origin": "phone_background_task"}
        }

    def get_platform_name(self) -> str:
        return "cron"

    def get_sender_id(self) -> str:
        return ""

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value
