"""App 画像：包名、别名与信息流浏览行为参数。

画像数据来自真机刻画：抖音 / B 站 / 小黑盒已按实测结果回填抽取规则、启动
Activity、登录页跳过与 dump 适配参数。插件不局限于预置画像：通过类型识别的已安装 App 都可以
按包名或中文标签打开/浏览，没有画像时用通用文本抽取兜底（内置画像只是参考提示）。

``KNOWN_APP_LABELS`` 提供常见第三方 App 的包名→中文名映射，供 ``phone_list_apps``
展示可读名称、``phone_open_app`` 按中文名解析包名；``resolve_package`` 支持精确
包名、中文标签与内置画像别名三种输入。

卡片级抽取字段（browse 使用）：

* ``card_desc_re``：卡片 ViewGroup 的 content-desc 结构化正则，带命名组
  ``title`` / ``author`` / ``play`` / ``danmaku`` / ``duration`` / ``likes``；
* ``title_res_id`` / ``title_res_id_fallback`` / ``author_res_id``：resource-id
  抽取，多个候选用 ``|`` 分隔；``author_prefix`` 只接受带该前缀的作者节点并去掉前缀；
* ``max_cards_per_screen``：单屏最多提取卡片数；
* ``pause_before_dump``：dump 前先单击屏幕中心暂停（视频自动播放时界面永不 idle，
  抖音必配），dump 完再单击恢复，禁止双击（双击会点赞）；
* ``skip_login_activity``：打开后前台 Activity 命中该子串视为登录/引导页，单击
  ``skip_login_tap_ratio`` 对应的跳过按钮进入游客信息流（小黑盒）；
* ``dump_retries``：该 App 的额外 dump 重试次数。

本模块只依赖标准库，便于本地单测。
"""

from __future__ import annotations

import random

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class AppProfile:
    """一款 App 的信息流画像。"""

    key: str
    label: str
    package: str
    aliases: tuple[str, ...]
    activity: str | None = None
    """显式启动组件（可选，形如 ``com.x/.MainActivity``）。"""
    feed_scroll_direction: str = "up"
    """信息流翻页方向：up（上滑看下一条）/down/left/right。"""
    settle_seconds: float = 2.5
    """滑动后等待界面稳定的秒数（全局配置保持默认值时使用画像校准值）。"""
    content_area: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    """内容区域（屏占比 l,t,r,b），区域外的节点视为导航/按钮噪声。"""
    exclude_text_re: tuple[str, ...] = ()
    """命中即丢弃的文本正则（导航、按钮等噪声）。"""
    item_signature_keys: tuple[str, ...] = ("text",)
    """生成条目指纹的字段（text/desc）。"""
    card_desc_re: str = ""
    """卡片级 content-desc 解析正则（可带 title/author/play/danmaku/duration 命名组）。"""
    title_res_id: str = ""
    """卡片标题节点 resource-id；多个候选用 ``|`` 分隔。"""
    title_res_id_fallback: str = ""
    """``title_res_id`` 无结果时再尝试的标题 resource-id（双列卡/纯文字帖等场景）。"""
    author_res_id: str = ""
    """卡片作者节点 resource-id；多个候选用 ``|`` 分隔。"""
    author_prefix: str = ""
    """只接受带该前缀的作者节点，并在入库时去掉前缀（如抖音 ``@``）。"""
    max_cards_per_screen: int = 0
    """单屏最多提取卡片数；0 表示按解析结果数量（内部上限 20）。"""
    swipe_duration_ms: int = 0
    """翻页滑动时长（毫秒）；0 表示使用全局配置 browse.swipe_duration_ms。"""
    pause_before_dump: bool = False
    """dump 前先单击屏幕中心暂停自动播放，dump 后单击恢复（禁止双击）。"""
    pause_tap_ratio: tuple[float, float] = (0.5, 0.5)
    """暂停/恢复点击位置（屏占比）。"""
    skip_login_activity: str = ""
    """前台 Activity 包含该子串时视为登录/引导页，需要单击按钮跳过。"""
    skip_login_tap_ratio: tuple[float, float] = (0.091, 0.094)
    """跳过登录/引导页的点击位置（屏占比，按 900x1600 折算）。"""
    dump_retries: int = 0
    """该 App 额外的 dump 重试次数（失败后等待约 1.8s 再试）。"""
    like_gesture: str = "none"
    """点赞手势钩子：none/double_tap；本期仅保留配置钩子。"""


# 通用信息流噪声：底部导航与顶部按钮。未刻画 App 使用；内置三款用实测清单。
_COMMON_NOISE = (
    r"^首页$",
    r"^推荐$",
    r"^关注$",
    r"^朋友$",
    r"^消息$",
    r"^我$",
    r"^我的$",
    r"^搜索$",
    r"^发布$",
    r"^拍摄$",
    r"^直播$",
    r"^同城$",
    r"^精选$",
    r"^频道$",
    r"^返回$",
    r"^更多$",
    r"^评论$",
    r"^分享$",
    r"^收藏$",
    r"^点赞$",
)

# B 站信息流卡片 content-desc 结构（真机实测）：
#   视频,<标题>,<播放>观看,<弹幕>弹幕,时长X分钟Y秒,UP主<作者>[,<n>点赞][,竖屏][,]
# 大卡可能缺 UP主 段、弹幕数可能是 "-"、竖版视频前缀为「竖版视频」。
_BILIBILI_CARD_DESC_RE = (
    r"^(?:竖版)?视频,(?P<title>.+?),(?P<play>[^,]+)观看,"
    r"(?P<danmaku>[^,]*?)弹幕,时长(?P<duration>[^,]+)"
    r"(?:,UP主(?P<author>[^,]+))?"
    r"(?:,(?P<likes>[^,]*?)点赞)?"
)

# B 站实测噪声：底部导航 / 顶部频道 tab / 计数与时长碎片 / 卡片按钮文案。
_BILIBILI_NOISE = (
    r"^(直播|推荐|热门|动画|影视|新征程|首页|关注|会员购|我的|登录|消息|游戏中心|发布)$",
    r"^\d+之\d+$",
    r"^\d+(\.\d+)?万?$",
    r"^-$",
    r"^\d{1,2}:\d{2}$",
    r"^(UP|UP主|视频|弹幕|点赞|更多|时长|广告|已关注)$",
)

# 小黑盒实测噪声：底部导航 / 等级 / 图片计数 / 热度等碎片文本。
_XIAOHEIHE_NOISE = (
    r"^(关注|推荐|首页|热点|游戏库|我)$",
    r"^Lv\.\d+$",
    r"^共\d+张$",
    r"^\d+/\d+$",
    r"^\d+$",
    r"^热点第\d+位$",
    r"^已推荐.*新内容$",
)

# 抖音实测噪声：底部导航 / 顶部频道 / 计数 / 卡片按钮文案。
_DOUYIN_NOISE = (
    r"^(首页|朋友|消息|我|拍摄)$",
    r"^(热点|直播|团购|同城|关注|商城|推荐|精选|经验)$",
    r"^\d+(\.\d+)?万?$",
    r"^(相关搜索|全屏观看|拍同款|展开|收起|弹|合集|搜索)$",
)


APP_PROFILES: dict[str, AppProfile] = {
    "douyin": AppProfile(
        key="douyin",
        label="抖音",
        package="com.ss.android.ugc.aweme",
        aliases=("douyin", "抖音", "aweme", "dy"),
        activity="com.ss.android.ugc.aweme/.splash.SplashActivity",
        feed_scroll_direction="up",
        settle_seconds=1.5,
        content_area=(0.0, 0.10, 1.0, 0.93),
        exclude_text_re=_DOUYIN_NOISE,
        item_signature_keys=("text",),
        title_res_id="com.ss.android.ugc.aweme:id/desc",
        author_res_id="com.ss.android.ugc.aweme:id/title",
        author_prefix="@",
        max_cards_per_screen=1,
        swipe_duration_ms=300,
        pause_before_dump=True,
        pause_tap_ratio=(0.5, 0.5),
        dump_retries=2,
        like_gesture="none",
    ),
    "bilibili": AppProfile(
        key="bilibili",
        label="哔哩哔哩",
        package="tv.danmaku.bili",
        aliases=("bilibili", "bili", "B站", "哔哩哔哩", "哔哩", "b站"),
        activity="tv.danmaku.bili/.MainActivityV2",
        feed_scroll_direction="up",
        settle_seconds=2.0,
        content_area=(0.0, 0.10, 1.0, 0.94),
        exclude_text_re=_BILIBILI_NOISE,
        item_signature_keys=("text",),
        card_desc_re=_BILIBILI_CARD_DESC_RE,
        title_res_id="tv.danmaku.bili:id/ugc_video_description",
        title_res_id_fallback="tv.danmaku.bili:id/title",
        author_res_id="tv.danmaku.bili:id/real_desc",
        max_cards_per_screen=8,
        swipe_duration_ms=400,
        dump_retries=2,
        like_gesture="none",
    ),
    "xiaoheihe": AppProfile(
        key="xiaoheihe",
        label="小黑盒",
        package="com.max.xiaoheihe",
        aliases=("xiaoheihe", "小黑盒", "xhh", "黑盒"),
        activity="com.max.xiaoheihe/.SplashActivity",
        feed_scroll_direction="up",
        settle_seconds=2.0,
        content_area=(0.0, 0.10, 1.0, 0.93),
        exclude_text_re=_XIAOHEIHE_NOISE,
        item_signature_keys=("text",),
        title_res_id="com.max.xiaoheihe:id/tv_title",
        title_res_id_fallback="com.max.xiaoheihe:id/tv_content",
        author_res_id="com.max.xiaoheihe:id/bbs_name",
        max_cards_per_screen=3,
        swipe_duration_ms=300,
        skip_login_activity="RegisterOrLoginActivityV2",
        skip_login_tap_ratio=(0.091, 0.094),
        dump_retries=2,
        like_gesture="none",
    ),
    "generic": AppProfile(
        key="generic",
        label="通用",
        package="",
        aliases=("generic", "通用", "任意", "当前应用"),
        feed_scroll_direction="up",
        settle_seconds=2.0,
        content_area=(0.0, 0.0, 1.0, 1.0),
        exclude_text_re=(),
        item_signature_keys=("text",),
        like_gesture="none",
    ),
}

# 常见第三方 App 包名 → 中文名。用于 phone_list_apps 展示与按中文名解析包名；
# 查不到的包名照常显示原始包名即可（不编造标签）。
KNOWN_APP_LABELS: dict[str, str] = {
    # 社交 / 通讯
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "com.tencent.tim": "TIM",
    "com.tencent.wework": "企业微信",
    "com.sina.weibo": "微博",
    "com.immomo.momo": "陌陌",
    "com.tencent.karaoke": "全民K歌",
    # 电商 / 生活
    "com.taobao.taobao": "淘宝",
    "com.tmall.wireless": "天猫",
    "com.jingdong.app.mall": "京东",
    "com.xunmeng.pinduoduo": "拼多多",
    "com.taobao.idlefish": "闲鱼",
    "com.eg.android.AlipayGphone": "支付宝",
    "com.unionpay": "云闪付",
    "com.alibaba.android.rimet": "钉钉",
    "com.sankuai.meituan": "美团",
    "com.dianping.v1": "大众点评",
    "me.ele": "饿了么",
    "com.cainiao.wireless": "菜鸟",
    "com.achievo.vipshop": "唯品会",
    "com.taobao.trip": "飞猪",
    # 内容 / 社区
    "tv.danmaku.bili": "哔哩哔哩",
    "com.bilibili.comic": "哔哩哔哩漫画",
    "com.ss.android.ugc.aweme": "抖音",
    "com.ss.android.ugc.aweme.lite": "抖音极速版",
    "com.smile.gifmaker": "快手",
    "com.kuaishou.nebula": "快手极速版",
    "com.xingin.xhs": "小红书",
    "com.zhihu.android": "知乎",
    "com.douban.frodo": "豆瓣",
    "com.baidu.tieba": "百度贴吧",
    "com.max.xiaoheihe": "小黑盒",
    "com.netease.cloudmusic": "网易云音乐",
    "com.tencent.qqmusic": "QQ音乐",
    "com.kugou.android": "酷狗音乐",
    "com.tencent.qqlive": "腾讯视频",
    "com.qiyi.video": "爱奇艺",
    "com.youku.phone": "优酷",
    "com.hunantv.imgo.activity": "芒果TV",
    "com.sohu.sohuvideo": "搜狐视频",
    "air.tv.douyu.android": "斗鱼",
    "com.duowan.kiwi": "虎牙直播",
    "com.tencent.weishi": "微视",
    # 工具 / 出行 / 游戏
    "com.autonavi.minimap": "高德地图",
    "com.baidu.BaiduMap": "百度地图",
    "com.tencent.mtt": "QQ浏览器",
    "com.tencent.qqmail": "QQ邮箱",
    "com.valvesoftware.android.steam.community": "Steam",
    "com.miHoYo.GenshinImpact": "原神",
    "com.miHoYo.hkrpg": "崩坏：星穹铁道",
    "com.tencent.tmgp.sgame": "王者荣耀",
    "com.tencent.tmgp.pubgmhd": "和平精英",
    "com.netease.sky": "光·遇",
}

# 未知 App 的错误提示（命令/工具共用；引导 AI 自己去发现已安装 App）。
UNKNOWN_APP_HINT_TEMPLATE = (
    "未知 App：{name}。可先调用 phone_list_apps 查看已安装 App 的包名/中文名，"
    "或先 phone_key home 回桌面再用 phone_screen 看图标"
)

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._]+$")

# 内置画像的稳定顺序（帮助文案与 /phone help 使用）。
_BUILTIN_ORDER = ("douyin", "bilibili", "xiaoheihe", "generic")


def _is_package_name(name: str) -> bool:
    """判断是否像 Android 包名：至少两段、仅含字母数字下划线与点。"""
    if "." not in name or " " in name:
        return False
    parts = name.split(".")
    return len(parts) >= 2 and all(part and part.replace("_", "").isalnum() for part in parts)


def resolve_app(name: str) -> AppProfile | None:
    """按 key / 别名 / 包名解析 App 画像。

    Args:
        name: 用户输入的 App 名称，大小写不敏感。

    Returns:
        匹配的画像；传入的是合法包名但不在内置画像里时，返回绑定该包名的
        generic 画像；无法识别时返回 None。
    """
    text = str(name or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in APP_PROFILES:
        return APP_PROFILES[lowered]

    for profile in APP_PROFILES.values():
        if lowered == profile.package.lower() or lowered == profile.label.lower():
            return profile
        if any(lowered == alias.lower() for alias in profile.aliases):
            return profile

    if _is_package_name(text):
        generic = APP_PROFILES["generic"]
        return replace(generic, key=lowered, label=text, package=text)
    return None


def all_app_keys() -> list[str]:
    """返回内置画像的 key 列表（按展示顺序）。"""
    return [key for key in _BUILTIN_ORDER if key in APP_PROFILES]


def app_label(name: str) -> str:
    """把 App 名称解析成人类可读标签，未知时原样返回。"""
    profile = resolve_app(name)
    return profile.label if profile else str(name or "").strip()


def _looks_like_package(text: str) -> bool:
    """判断是否像 Android 包名（至少含一个点，仅字母数字下划线与点）。"""
    return "." in text and bool(_PACKAGE_NAME_RE.fullmatch(text))


def app_label_for_package(package: str) -> str:
    """查询包名的可读中文名；未收录时返回空串。"""
    return KNOWN_APP_LABELS.get(str(package or "").strip(), "")


def pick_random_app(
    packages: list[str] | tuple[str, ...] | None,
    *,
    rng: random.Random | None = None,
) -> str:
    """从已安装包名中随机选一个；空列表返回空串。"""
    items = [str(item or "").strip() for item in (packages or ())]
    items = [item for item in items if item]
    if not items:
        return ""
    generator = rng or random
    return generator.choice(items)


def parse_package_list(output: str) -> list[str]:
    """解析 ``pm list packages -3`` 输出为排序后的包名列表。

    Args:
        output: adb shell 命令 stdout，每行形如 ``package:com.example.app``。

    Returns:
        去重排序的包名列表；无有效行时返回空列表。
    """
    packages: set[str] = set()
    for line in str(output or "").splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        package = line[len("package:") :].strip()
        if _looks_like_package(package):
            packages.add(package)
    return sorted(packages)


def resolve_package(
    name: str,
    installed: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    """把 App 名称解析成包名（内置画像别名 → 中文标签 → 直接包名）。

    Args:
        name: 用户/模型给出的 App 名称，如 ``微信``、``bilibili`` 或
            ``com.tencent.mm``。
        installed: 可选的已安装包名列表；提供时直接包名必须在列表内才返回。

    Returns:
        匹配到的包名；无法识别返回 ``None``。
    """
    text = str(name or "").strip()
    if not text:
        return None
    lowered = text.lower()

    # 1) 内置画像：key / 中文标签 / 别名 / 包名
    for profile in APP_PROFILES.values():
        if not profile.package:
            continue
        if lowered == profile.package.lower() or lowered == profile.label.lower():
            return profile.package
        if any(lowered == alias.lower() for alias in profile.aliases):
            return profile.package

    # 2) 常见 App 中文名 / 精确包名
    for package, label in KNOWN_APP_LABELS.items():
        if lowered == package.lower() or text == label:
            return package

    # 3) 直接包名（提供 installed 时要求确实已安装）
    if _looks_like_package(text):
        if installed is None:
            return text
        installed_set = {str(item).strip() for item in installed}
        return text if text in installed_set else None
    return None


# ----------------------------------------------------------------------
# 持续刷手机：偏好 App 中文名解析
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PreferredAppMatch:
    """持续刷手机偏好 App 的解析结果。"""

    raw: str
    """原始输入（已去首尾空白）。"""
    label: str
    """可读名：内置画像标签 / 中文名表优先，未知时用原始输入。"""
    package: str
    """解析出的包名；未识别时为空串。"""
    matched: bool
    """是否识别为已知 App（内置画像、中文名表或合法包名）。"""
    installed: bool | None = None
    """是否已安装：``None`` 表示未提供已安装列表；提供列表时按包名判断。"""

    @property
    def display(self) -> str:
        """供提示词与持续刷状态展示的文本。

        已识别且已安装：``哔哩哔哩（tv.danmaku.bili，已安装）``；
        已识别但未安装：``抖音（未安装，跳过）``；
        未识别：原样返回输入。
        """
        if not self.matched or not self.package:
            return self.label or self.raw
        label = self.label or self.raw or self.package
        # 直接填包名且无中文映射时可读名与包名相同，避免重复展示
        same_name = label == self.package
        name = self.package if same_name else label
        if self.installed is False:
            return f"{name}（未安装，跳过）"
        if self.installed is True:
            return f"{name}（已安装）" if same_name else f"{name}（{self.package}，已安装）"
        return name if same_name else f"{name}（{self.package}）"


def _resolve_preferred_package(text: str) -> str:
    """把偏好 App 输入解析为包名，未识别时返回空串。

    先复用 :func:`resolve_package`；未命中时再按 ``KNOWN_APP_LABELS`` 的中文名
    做大小写不敏感兜底（如 ``steam`` → ``Steam``）。
    """
    package = resolve_package(text)
    if package:
        return package
    lowered = text.lower()
    for known_package, label in KNOWN_APP_LABELS.items():
        if label.lower() == lowered:
            return known_package
    return ""


def match_preferred_app(
    name: str,
    installed: list[str] | tuple[str, ...] | None = None,
) -> PreferredAppMatch:
    """解析持续刷手机偏好 App：中文名 / 别名 / 画像 key / 合法包名。

    大小写不敏感；支持内置画像 key、中文标签与别名（B站、哔哩哔哩、bilibili）、
    ``KNOWN_APP_LABELS`` 中的中文名以及合法 Android 包名。``generic`` 是通用
    占位画像而非具体 App，按未识别处理。

    Args:
        name: 配置里填写的 App 名称，如 ``B站``、``哔哩哔哩``、``bilibili``、
            ``微信`` 或 ``tv.danmaku.bili``。
        installed: 可选的已安装包名列表（``parse_package_list`` 结果）；
            提供时按包名判断是否已安装。

    Returns:
        解析结果。未识别时 ``matched=False``、``package=""``、``installed=None``；
        已识别时 ``matched=True``，提供 ``installed`` 时按包名给出 ``installed``。
    """
    text = str(name or "").strip()
    if not text:
        return PreferredAppMatch(raw="", label="", package="", matched=False, installed=None)

    package = _resolve_preferred_package(text)
    if not package:
        # 未识别：保留原文，不臆测包名与安装状态
        return PreferredAppMatch(raw=text, label=text, package="", matched=False, installed=None)

    # 可读名优先内置画像标签，其次中文名表，最后退回原文
    profile = resolve_app(text)
    if profile is not None and profile.package and profile.key in APP_PROFILES:
        label = profile.label
    else:
        label = app_label_for_package(package) or text

    installed_flag: bool | None = None
    if installed is not None:
        installed_set = {str(item).strip().lower() for item in installed}
        installed_flag = package.lower() in installed_set

    return PreferredAppMatch(
        raw=text,
        label=label,
        package=package,
        matched=True,
        installed=installed_flag,
    )


def format_preferred_apps(
    matches: Iterable[PreferredAppMatch],
    *,
    limit: int = 10,
) -> str:
    """把偏好 App 解析结果拼成一行展示文本。

    Args:
        matches: :func:`match_preferred_app` 的结果序列。
        limit: 最多展示的条目数；超出时截断并在末尾标注总数。

    Returns:
        以 ``、`` 连接的展示文本，如
        ``哔哩哔哩（tv.danmaku.bili，已安装）、抖音（未安装，跳过）``；
        超过 ``limit`` 时为 ``…、…等 12 个``；没有可展示条目时返回空串。
    """
    displays: list[str] = []
    for match in matches:
        text = match.display
        if text:
            displays.append(text)
    total = len(displays)
    if total == 0:
        return ""
    if limit <= 0:
        return f"等 {total} 个"
    if total <= limit:
        return "、".join(displays)
    return f"{'、'.join(displays[:limit])}等 {total} 个"
