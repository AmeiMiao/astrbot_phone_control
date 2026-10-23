"""内容新鲜感 / 厌倦感状态存储：让 AI 少刷已经看腻的同类内容。

手机信息流里同一个主题（同一款游戏、同一桩赛事、同一类梗）反复出现时，单纯
「最近看过什么」无法表达「已经看烦了」。本模块按主题累计疲劳度，随时间自然
衰减，并把「已看腻 / 接近阈值」的条目渲染进提示词，供主 Agent 降低同类内容的
打开与停留概率、优先换主题或换 App。

存储结构：``root/novelty.json``（UTF-8 JSON）：:

    {
      "version": 1,
      "entries": [
        {
          "key": "topic:3f0c9a1d2b4e5f60",
          "label": "CS2 上海 Major 决赛前瞻",
          "app": "哔哩哔哩",
          "title": "CS2 上海 Major 决赛前瞻：双方图池与关键选手",
          "fatigue": 60.0,
          "hits": 3,
          "first_seen": 1760000000.0,
          "last_seen": 1760086400.0
        }
      ]
    }

算法口径：

* 文本归一化：NFKC + 小写；抽取 CJK 连续段的 2-gram（单字段回退为该字本身）
  以及长度 >= 2 的拉丁/数字词元；先剔除 UI/噪声词与模板词再切词，切词后再剔除
  通用模板 2-gram（「年最/最值/值得/得买/买的」等），避免「2024 年最值得买的
  10 款手机」和「10 部电影」这类同模板标题被误判为同类内容。
* 相似度：两个主题词元集合的 Jaccard = |A∩B| / |A∪B|；取相似度最高、不低于
  ``settings.similarity`` **且至少共享 2 个主题词元**的已有条目为同类主题
  （词元完全相同的近乎重复标题除外），否则视为新主题。要求共享词元数可避免
  只因一个通用词（如「教学」「日常」）就把无关主题错误合并。
* 疲劳度：新主题 = ``min(100, gain*weight)``；命中同类 = ``min(100, 衰减后疲劳 +
  gain*weight)``，命中次数 +1，``last_seen`` 刷新。``threshold=0`` 或半衰期/增益
  非正时整体禁用：``record`` 不写盘、``render_for_prompt`` 返回空串。
* 衰减：``stored_fatigue * 0.5 ** (elapsed / (half_life_hours*3600))``，从
  ``last_seen`` 起算，读取时实时计算、不要求每次扫描回写。
* 容量：超过 ``settings.max_entries`` 时优先淘汰疲劳度最低者，其次 ``last_seen``
  最旧者。
* 持久化：tmp 文件 + ``os.replace`` 原子替换；文件不存在按空档案处理，JSON 损坏
  时容错为空并继续工作（不抛异常）。短时写入/读取异常均吞掉，不影响手机主流程。
* 所有时间经 :meth:`NoveltyStore._now` 读取，构造时可注入 ``clock`` 便于单测。

本模块只依赖标准库，不导入 astrbot。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

NOVELTY_FILENAME = "novelty.json"
DEFAULT_THRESHOLD = 60.0
DEFAULT_HALF_LIFE_HOURS = 24.0
DEFAULT_GAIN = 20.0
# 长标题共享少量主题词时 Jaccard 会被拉低，默认取较低阈值并辅以「至少共享
# 2 个词元」的约束（见 _best_match）。
DEFAULT_SIMILARITY = 0.15
DEFAULT_MAX_ENTRIES = 300
DEFAULT_INJECT_LIMIT = 5
MAX_FATIGUE = 100.0
PERSIST_VERSION = 1
LABEL_CHARS = 40
TITLE_CHARS = 200
RENDER_CHARS = 600

# CJK 连续段（中文/日文假名/韩文等），段内不含空白与 ASCII。
_CJK_RUN_RE = re.compile(
    r"[\u2e80-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff]+"
)
_WORD_RE = re.compile(r"[a-z0-9]+")

# UI/噪声词：抽词元前先从 CJK 段里剔除，纯界面标题因而不产生主题词元。
_NOISE_WORDS: tuple[str, ...] = tuple(
    sorted(
        {
            "首页",
            "推荐",
            "关注",
            "点赞",
            "评论",
            "分享",
            "收藏",
            "播放",
            "视频",
            "直播",
            "搜索",
            "消息",
            "我的",
            "官方",
            "合集",
            "高清",
            "完整版",
            "下载",
            "安装",
            "更多",
            "详情",
            "刷新",
            "加载",
            "用户",
            "内容",
            "作品",
            "发布",
            "热门",
            "精选",
            "福利",
            "广告",
            "专题",
            "榜单",
            "排行",
            "最新",
            "更新",
            "正在",
            "观看",
            "进入",
            "返回",
            "设置",
            "通知",
            "私信",
            "弹幕",
            "预约",
            "置顶",
            "转发",
            "展开",
            "收起",
            "查看",
            "全部",
            "相关",
            "滑动",
            "点击",
            "推荐阅读",
            # 通用 UI 动作词：按 text/desc 点击按钮时不应被当成内容主题。
            "发送",
            "确认",
            "确定",
            "取消",
            "关闭",
            "打开",
            "继续",
            "跳过",
            "同意",
            "允许",
            "完成",
            "提交",
            "保存",
            "删除",
            "编辑",
            "复制",
            "粘贴",
            "下一步",
            "上一步",
            "登录",
            "注册",
            "关注",
            "已关注",
            "展开全文",
            # 通用内容模板词：与具体主题无关，参与相似度只会制造误合并。
            "教学",
            "日常",
            "搞笑",
            "今天",
            "昨天",
            "明天",
            "值得",
            "最值得",
            "解读",
            "攻略",
            "指南",
            "教程",
            "讲解",
            "精讲",
            "测评",
            "评测",
            "实测",
            "开箱",
            "十大",
            "什么",
            "怎么",
            "为何",
            "如何",
            "哪个",
            "哪些",
        },
        key=len,
        reverse=True,
    )
)
# 切词后再过滤的通用模板 2-gram（长模板短语剔词后仍会残留的拼接片段）。
_NOISE_BIGRAMS = frozenset(
    {
        "年最",
        "最值",
        "值得",
        "得买",
        "买的",
        "款手",
        "部电",
        "十大",
        "第一",
        "第二",
        "第三",
        "最新",
        "新款",
        "合集",
        "盘点",
        "排行",
        "榜单",
        "测评",
        "评测",
        "实测",
        "开箱",
        "教程",
        "攻略",
        "指南",
        "讲解",
        "精讲",
        "教学",
        "日常",
        "搞笑",
        "今天",
        "昨天",
        "明天",
    }
)
# 拉丁/数字噪声词：长度 >= 2 才可能成为词元，这里只剔除纯界面噪声。
_NOISE_WORDS_LATIN = frozenset(
    {
        "the",
        "and",
        "for",
        "you",
        "are",
        "this",
        "that",
        "with",
        "from",
        "your",
        "www",
        "com",
        "cn",
        "net",
        "html",
        "htm",
        "http",
        "https",
    }
)


@dataclass(frozen=True)
class NoveltySettings:
    """新鲜感/厌倦感算法的可调参数（由调用方按配置构造）。"""

    threshold: float = DEFAULT_THRESHOLD
    """疲劳度阈值（0-100）；``0`` 表示不启用，达到阈值视为「已看腻」。"""
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS
    """疲劳度自然衰减的半衰期小时数；非正数表示不启用衰减。"""
    gain: float = DEFAULT_GAIN
    """每次记录（严重程度 weight=1）累加的疲劳度。"""
    similarity: float = DEFAULT_SIMILARITY
    """判定同类主题的 Jaccard 相似度下限（0-1，默认 0.15）。"""
    max_entries: int = DEFAULT_MAX_ENTRIES
    """档案最多保留的主题条目数。"""
    inject_limit: int = DEFAULT_INJECT_LIMIT
    """提示词最多注入的条目数。"""

    @property
    def enabled(self) -> bool:
        """是否启用：阈值、半衰期、增益都必须为正数。"""
        return self.threshold > 0 and self.half_life_hours > 0 and self.gain > 0


@dataclass(frozen=True)
class NoveltyEntry:
    """一个主题的新鲜感档案（不可变，命中时整体替换）。"""

    key: str
    """主题标识（由词元集合哈希得到，跨 App 保持一致）。"""
    label: str
    """可读主题名（最近一次内容的压缩文本前 40 字）。"""
    app: str
    """首次出现该主题的 App 名。"""
    title: str
    """最近一次内容的压缩文本（最多 200 字，用于相似度匹配与展示）。"""
    fatigue: float
    """落盘时的疲劳度（尚未衰减；读取时按半衰期实时计算）。"""
    hits: int
    """累计命中次数。"""
    first_seen: float
    """首次出现时间戳（秒）。"""
    last_seen: float
    """最近一次出现时间戳（秒），衰减从此起算。"""


@dataclass(frozen=True)
class NoveltyMatch:
    """一次评估/记录的结果。"""

    entry: NoveltyEntry | None
    """命中的主题（已含衰减后的 fatigue）；新主题或无法识别时为 ``None``。"""
    similarity: float
    """与最相近主题的相似度（0-1）；新主题为 0。"""
    fatigue: float
    """衰减后的当前疲劳度（0-100）。"""
    bored: bool
    """是否已达看腻阈值（``threshold<=0`` 时恒为 False）。"""
    is_new: bool
    """是否为新主题；文本不可识别/功能禁用时为 False。"""


def _clean_text(value: Any, limit: int = 0) -> str:
    """压缩空白；``limit>0`` 时截断并加省略号。"""
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """把任意值转成有限 float；非法值回退默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def _coerce_int(value: Any, default: int) -> int:
    """把任意值转成 int；非法值回退默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clamp_fatigue(value: Any) -> float:
    """把疲劳度收敛到 ``[0, MAX_FATIGUE]``。"""
    return min(MAX_FATIGUE, max(0.0, _coerce_float(value, 0.0)))


def _normalize_text(text: Any) -> str:
    """NFKC 归一化 + 小写化，供抽词元使用。"""
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def tokenize(text: Any) -> frozenset[str]:
    """抽取用于相似度比较的主题词元集合。

    CJK 连续段先剔除 UI/噪声词与通用模板词，再取 2-gram（过滤模板 2-gram）；
    单字段回退为该字本身。拉丁/数字词元取长度 >= 2 的连续串（小写），纯数字
    不参与比较（避免「2024」「10」这类年份/序号把无关标题拉到一起），并剔除
    常见界面噪声词。

    Args:
        text: 待抽取的标题/正文。

    Returns:
        去重后的词元集合；纯符号、空白或纯 UI 文本返回空集合。
    """
    normalized = _normalize_text(text)
    if not normalized:
        return frozenset()
    tokens: set[str] = set()
    for match in _CJK_RUN_RE.finditer(normalized):
        segment = match.group(0)
        for word in _NOISE_WORDS:
            if word in segment:
                segment = segment.replace(word, " ")
        for part in segment.split():
            length = len(part)
            if length <= 0:
                continue
            if length == 1:
                tokens.add(part)
                continue
            for index in range(length - 1):
                bigram = part[index : index + 2]
                if bigram not in _NOISE_BIGRAMS:
                    tokens.add(bigram)
    for match in _WORD_RE.finditer(normalized):
        word = match.group(0)
        if (
            len(word) >= 2
            and word not in _NOISE_WORDS_LATIN
            and not word.isdigit()
        ):
            tokens.add(word)
    return frozenset(token for token in tokens if token.strip())


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """两个词元集合的 Jaccard 相似度；任一为空返回 0。"""
    first = set(left)
    second = set(right)
    if not first or not second:
        return 0.0
    union = first | second
    if not union:
        return 0.0
    return len(first & second) / len(union)


def _entry_key(tokens: Iterable[str]) -> str:
    """由主题词元集合生成稳定标识。"""
    digest = hashlib.sha1("|".join(sorted(tokens)).encode("utf-8")).hexdigest()
    return f"topic:{digest[:16]}"


def _decayed_fatigue(
    fatigue: Any,
    last_seen: Any,
    half_life_hours: Any,
    now: float,
) -> float:
    """按半衰期计算当前疲劳度；半衰期非正或时间异常时原样返回。"""
    base = _clamp_fatigue(fatigue)
    half_life = _coerce_float(half_life_hours, 0.0)
    if half_life <= 0:
        return base
    last = _coerce_float(last_seen, 0.0)
    elapsed = now - last
    if elapsed <= 0:
        return base
    return _clamp_fatigue(base * (0.5 ** (elapsed / (half_life * 3600.0))))


def _max_entries(settings: NoveltySettings) -> int:
    """归一化档案容量（至少 1 条）。"""
    value = _coerce_int(getattr(settings, "max_entries", DEFAULT_MAX_ENTRIES), DEFAULT_MAX_ENTRIES)
    return max(1, value)


def _inject_limit(settings: NoveltySettings) -> int:
    """归一化提示词注入条数（小于 0 按 0 处理）。"""
    value = _coerce_int(getattr(settings, "inject_limit", DEFAULT_INJECT_LIMIT), DEFAULT_INJECT_LIMIT)
    return max(0, value)


def _entry_to_dict(entry: NoveltyEntry) -> dict[str, Any]:
    """把主题条目转成可 JSON 序列化的字典。"""
    return {
        "key": entry.key,
        "label": entry.label,
        "app": entry.app,
        "title": entry.title,
        "fatigue": round(_clamp_fatigue(entry.fatigue), 4),
        "hits": max(0, _coerce_int(entry.hits, 0)),
        "first_seen": _coerce_float(entry.first_seen, 0.0),
        "last_seen": _coerce_float(entry.last_seen, 0.0),
    }


def _entry_from_dict(raw: Any) -> NoveltyEntry | None:
    """把 JSON 里的一条主题还原为 :class:`NoveltyEntry`；不可用返回 None。"""
    if not isinstance(raw, dict):
        return None
    title = _clean_text(raw.get("title") or raw.get("label"), TITLE_CHARS)
    label = _clean_text(raw.get("label") or title, LABEL_CHARS)
    if not title and not label:
        return None
    key = str(raw.get("key") or "").strip()
    if not key:
        tokens = tokenize(title or label)
        if not tokens:
            return None
        key = _entry_key(tokens)
    fatigue = _clamp_fatigue(raw.get("fatigue"))
    first_seen = _coerce_float(raw.get("first_seen"), 0.0)
    last_seen = _coerce_float(raw.get("last_seen"), first_seen)
    return NoveltyEntry(
        key=key,
        label=label,
        app=_clean_text(raw.get("app"), 80),
        title=title,
        fatigue=fatigue,
        hits=max(0, _coerce_int(raw.get("hits"), 0)),
        first_seen=first_seen,
        last_seen=last_seen,
    )


class NoveltyStore:
    """基于 JSON 文件的新鲜感/厌倦感档案（线程安全、原子写、异常吞掉）。"""

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化档案。

        Args:
            root: 数据目录，档案写入 ``root/novelty.json``。
            clock: 时间源（返回秒级墙钟），缺省用 :func:`time.time`；便于单测注入。
        """
        self.root = Path(root)
        self.path = self.root / NOVELTY_FILENAME
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: list[NoveltyEntry] = []
        self._load()

    # ---- 时间/持久化 --------------------------------------------------

    def _now(self) -> float:
        """读取当前时间；时钟异常时回退系统时间。"""
        clock = self._clock
        if not callable(clock):
            return time.time()
        try:
            return _coerce_float(clock(), time.time())
        except Exception:  # noqa: BLE001 - 注入时钟异常不应影响主流程
            return time.time()

    def _load(self) -> None:
        """从磁盘加载档案；文件缺失/损坏/不可读时保持空档案。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return
        try:
            payload = json.loads(text)
        except ValueError:
            return
        raw_entries = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(raw_entries, list):
            return
        loaded: dict[str, NoveltyEntry] = {}
        for raw in raw_entries:
            entry = _entry_from_dict(raw)
            if entry is None:
                continue
            previous = loaded.get(entry.key)
            # 同一主题重复落盘时只保留最近一条
            if previous is None or entry.last_seen >= previous.last_seen:
                loaded[entry.key] = entry
        self._entries = sorted(loaded.values(), key=lambda item: item.last_seen)

    def _save_locked(self) -> None:
        """原子写入整份档案（调用方须持锁）；失败不影响内存状态。"""
        payload = {
            "version": PERSIST_VERSION,
            "entries": [_entry_to_dict(entry) for entry in self._entries],
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            return

    # ---- 内部计算 -----------------------------------------------------

    def _decayed_entry(self, entry: NoveltyEntry, settings: NoveltySettings, now: float) -> NoveltyEntry:
        """返回附上当前衰减疲劳度的条目副本。"""
        return replace(
            entry,
            fatigue=_decayed_fatigue(
                entry.fatigue,
                entry.last_seen,
                settings.half_life_hours,
                now,
            ),
        )

    def _best_match(
        self,
        tokens: frozenset[str],
        settings: NoveltySettings,
        now: float,
    ) -> tuple[NoveltyEntry, float] | None:
        """查找与给定词元最相近的已有主题（须持锁）。"""
        if not tokens:
            return None
        threshold = _coerce_float(settings.similarity, DEFAULT_SIMILARITY)
        best: NoveltyEntry | None = None
        best_similarity = 0.0
        for entry in self._entries:
            entry_tokens = tokenize(entry.title or entry.label)
            similarity = _jaccard(tokens, entry_tokens)
            if similarity <= 0 or similarity < threshold:
                continue
            # 只共享 1 个通用词元容易把无关主题合并；近乎完全相同的词元集合例外。
            shared = len(tokens & entry_tokens)
            if shared < 2 and similarity < 0.99:
                continue
            if similarity > best_similarity:
                best_similarity = similarity
                best = entry
        if best is None or best_similarity <= 0:
            return None
        return self._decayed_entry(best, settings, now), best_similarity

    def _trim_locked(self, settings: NoveltySettings, now: float) -> None:
        """按容量上限淘汰：先疲劳度最低，其次 last_seen 最旧（调用方须持锁）。"""
        limit = _max_entries(settings)
        while len(self._entries) > limit:
            index = min(
                range(len(self._entries)),
                key=lambda position: (
                    _decayed_fatigue(
                        self._entries[position].fatigue,
                        self._entries[position].last_seen,
                        settings.half_life_hours,
                        now,
                    ),
                    self._entries[position].last_seen,
                ),
            )
            del self._entries[index]

    @staticmethod
    def _bored(settings: NoveltySettings, fatigue: float) -> bool:
        """是否达到看腻阈值（阈值非正时恒为 False）。"""
        threshold = _coerce_float(settings.threshold, 0.0)
        return threshold > 0 and fatigue >= threshold

    @staticmethod
    def _disabled_match() -> NoveltyMatch:
        """禁用/文本不可识别时的统一结果。"""
        return NoveltyMatch(
            entry=None,
            similarity=0.0,
            fatigue=0.0,
            bored=False,
            is_new=False,
        )

    # ---- 对外接口 -----------------------------------------------------

    def evaluate(self, app: str, text: str, settings: NoveltySettings) -> NoveltyMatch:
        """评估一条内容的新鲜度，不写盘。

        Args:
            app: 来源 App 名（仅记录用，匹配跨 App 进行）。
            text: 标题/正文文本。
            settings: 算法参数。

        Returns:
            :class:`NoveltyMatch`；功能禁用或文本无有效词元时 ``entry=None``、
            ``is_new=False``；有有效词元但未命中同类主题时 ``is_new=True``。
        """
        tokens = tokenize(text)
        if not settings.enabled or not tokens:
            return self._disabled_match()
        now = self._now()
        with self._lock:
            matched = self._best_match(tokens, settings, now)
        if matched is None:
            return NoveltyMatch(
                entry=None,
                similarity=0.0,
                fatigue=0.0,
                bored=False,
                is_new=True,
            )
        entry, similarity = matched
        return NoveltyMatch(
            entry=entry,
            similarity=similarity,
            fatigue=entry.fatigue,
            bored=self._bored(settings, entry.fatigue),
            is_new=False,
        )

    def record(
        self,
        app: str,
        text: str,
        settings: NoveltySettings,
        *,
        weight: float = 1.0,
    ) -> NoveltyMatch:
        """记录一次内容曝光，累加同类主题疲劳度并落盘。

        Args:
            app: 来源 App 名（首次出现时记录）。
            text: 标题/正文文本；无有效词元或功能禁用时不写盘。
            settings: 算法参数。
            weight: 本次曝光的严重程度（>=0；1 表示一次正常曝光）。

        Returns:
            :class:`NoveltyMatch`：新主题 ``is_new=True``（``entry`` 为新条目），
            命中同类主题 ``is_new=False`` 且疲劳度已累加；禁用/文本不可识别时
            ``entry=None``、``is_new=False``。
        """
        tokens = tokenize(text)
        if not settings.enabled or not tokens:
            return self._disabled_match()
        now = self._now()
        amount = _coerce_float(settings.gain, 0.0) * max(0.0, _coerce_float(weight, 0.0))
        app_text = _clean_text(app, 80) or "未知"
        cleaned = _clean_text(text)
        label = cleaned[:LABEL_CHARS]
        title = cleaned[:TITLE_CHARS]
        with self._lock:
            matched = self._best_match(tokens, settings, now)
            if matched is not None:
                previous, similarity = matched
                entry = replace(
                    previous,
                    label=label or previous.label,
                    title=title or previous.title,
                    fatigue=min(MAX_FATIGUE, previous.fatigue + amount),
                    hits=max(0, previous.hits) + 1,
                    last_seen=now,
                )
                is_new = False
                for index, current in enumerate(self._entries):
                    if current.key == previous.key:
                        self._entries[index] = entry
                        break
                else:  # pragma: no cover - 理论上命中即存在
                    self._entries.append(entry)
            else:
                similarity = 0.0
                entry = NoveltyEntry(
                    key=_entry_key(tokens),
                    label=label,
                    app=app_text,
                    title=title,
                    fatigue=_clamp_fatigue(amount),
                    hits=1,
                    first_seen=now,
                    last_seen=now,
                )
                self._entries.append(entry)
                is_new = True
            self._trim_locked(settings, now)
            self._save_locked()
            current_fatigue = _decayed_fatigue(
                entry.fatigue,
                entry.last_seen,
                settings.half_life_hours,
                now,
            )
        return NoveltyMatch(
            entry=replace(entry, fatigue=current_fatigue),
            similarity=similarity,
            fatigue=current_fatigue,
            bored=self._bored(settings, current_fatigue),
            is_new=is_new,
        )

    def entries(self, settings: NoveltySettings) -> list[NoveltyEntry]:
        """返回全部主题条目（疲劳度已衰减，按疲劳度降序）。"""
        now = self._now()
        with self._lock:
            items = [self._decayed_entry(entry, settings, now) for entry in self._entries]
        items.sort(key=lambda entry: (entry.fatigue, entry.last_seen), reverse=True)
        return items

    def render_for_prompt(self, settings: NoveltySettings, *, break_rule: bool = False) -> str:
        """渲染可注入提示词的新鲜感摘要（未启用/无符合条目返回空串）。

        只列出疲劳度 >= ``threshold*0.6`` 的条目（最多 ``inject_limit`` 条），
        整段文本不超过 600 字。
        """
        if not settings.enabled:
            return ""
        threshold = _coerce_float(settings.threshold, 0.0)
        limit = _inject_limit(settings)
        if limit <= 0:
            return ""
        floor = threshold * 0.6
        items = [
            entry
            for entry in self.entries(settings)
            if entry.fatigue > 0 and entry.fatigue >= floor
        ]
        if not items:
            return ""
        lines = [f"【内容新鲜感】阈值 {threshold:g}/100："]
        for entry in items[:limit]:
            label = entry.label or entry.app or "未命名主题"
            if self._bored(settings, entry.fatigue):
                lines.append(
                    f"- 「{label}」疲劳 {entry.fatigue:.0f}/100"
                    + ("（已看腻：阈值仅作参考，可按真实意愿继续看）" if break_rule
                     else "（已看腻：下次降低同类内容打开/停留概率，优先换主题或换 App）")
                )
            else:
                lines.append(
                    f"- 「{label}」疲劳 {entry.fatigue:.0f}/100"
                    + ("（接近阈值，供参考）" if break_rule else "（接近阈值，适度减少）")
                )
        lines.append("一段时间不看会自然恢复新鲜感。")
        text = "\n".join(lines)
        if len(text) > RENDER_CHARS:
            text = text[: max(0, RENDER_CHARS - 1)] + "…"
        return text

    def status_text(self, settings: NoveltySettings) -> str:
        """渲染 ``/phone novelty status`` 用的中文状态摘要。"""
        threshold = _coerce_float(settings.threshold, 0.0)
        half_life = _coerce_float(settings.half_life_hours, 0.0)
        gain = _coerce_float(settings.gain, 0.0)
        similarity = _coerce_float(settings.similarity, DEFAULT_SIMILARITY)
        items = self.entries(settings)
        bored_count = (
            sum(1 for entry in items if self._bored(settings, entry.fatigue))
            if threshold > 0
            else 0
        )
        if not settings.enabled:
            lines = [
                "内容新鲜感：未启用（threshold=0 或半衰期/增益非正，不统计也不注入）",
                f"- 条目：{len(items)} 条（上限 {_max_entries(settings)}）",
            ]
        else:
            lines = [
                "内容新鲜感：已启用",
                f"- 阈值：{threshold:g}/100（达到即视为已看腻）",
                f"- 半衰期：{half_life:g} 小时（安静一段时间后疲劳度自然减半）",
                f"- 增益：每次记录 +{gain:g}；相似度阈值 {similarity:.2f}",
                f"- 条目：{len(items)} 条，已看腻 {bored_count} 条"
                f"（上限 {_max_entries(settings)}，注入最多 {_inject_limit(settings)} 条）",
            ]
        if items:
            lines.append("Top 5：")
            for entry in items[:5]:
                label = entry.label or entry.app or "未命名主题"
                mark = "（已看腻）" if self._bored(settings, entry.fatigue) else ""
                lines.append(
                    f"  - 「{label}」疲劳 {entry.fatigue:.0f}/100"
                    f"（{entry.app or '未知'}，命中 {entry.hits} 次）{mark}"
                )
        return "\n".join(lines)

    def stats(self, settings: NoveltySettings) -> dict[str, Any]:
        """返回档案统计信息（供监控/Dashboard 或日志使用）。"""
        threshold = _coerce_float(settings.threshold, 0.0)
        items = self.entries(settings)
        bored_count = (
            sum(1 for entry in items if self._bored(settings, entry.fatigue))
            if threshold > 0
            else 0
        )
        average = (
            round(sum(entry.fatigue for entry in items) / len(items), 2) if items else 0.0
        )
        return {
            "enabled": bool(settings.enabled),
            "threshold": threshold,
            "half_life_hours": _coerce_float(settings.half_life_hours, 0.0),
            "gain": _coerce_float(settings.gain, 0.0),
            "similarity": _coerce_float(settings.similarity, DEFAULT_SIMILARITY),
            "entries": len(items),
            "bored": bored_count,
            "max_entries": _max_entries(settings),
            "inject_limit": _inject_limit(settings),
            "max_fatigue": MAX_FATIGUE,
            "avg_fatigue": average,
            "path": str(self.path),
        }

    def reset(self) -> int:
        """清空档案并返回清除前的条目数（同时写入空档案）。"""
        with self._lock:
            count = len(self._entries)
            self._entries = []
            self._save_locked()
            return count
