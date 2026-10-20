"""core.novelty 单元测试：疲劳累加/阈值、相似度匹配、半衰期衰减、禁用、
持久化重载、损坏容错、噪声过滤、容量淘汰与文本渲染。"""

from __future__ import annotations

import json

import pytest

from core.novelty import (
    DEFAULT_MAX_ENTRIES,
    MAX_FATIGUE,
    NOVELTY_FILENAME,
    NoveltyEntry,
    NoveltyMatch,
    NoveltySettings,
    NoveltyStore,
    tokenize,
)

ALL_NOISE_TITLE = (
    "首页 推荐 关注 点赞 评论 分享 收藏 播放 视频 直播 搜索 消息 我的 官方 "
    "合集 高清 完整版 下载 安装 更多 详情 刷新 加载 用户 内容 作品 发布"
)
TOPIC_A = "CS2 赛事 冠军 预测"
TOPIC_A_SIMILAR = "CS2 赛事 冠军 赔率"
TOPIC_B = "原神 新角色 抽卡 攻略"
TOPIC_C = "猫咪 日常 搞笑 剪辑"


class _Clock:
    """可推进的假时钟（秒级墙钟）。"""

    def __init__(self, now: float = 1_760_000_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _settings(**overrides) -> NoveltySettings:
    base = {"threshold": 60.0, "half_life_hours": 24.0, "gain": 20.0}
    base.update(overrides)
    return NoveltySettings(**base)


def _payload(tmp_path) -> dict:
    return json.loads((tmp_path / NOVELTY_FILENAME).read_text(encoding="utf-8"))


def test_settings_enabled_requires_positive_values():
    assert _settings().enabled is True
    assert NoveltySettings(threshold=0).enabled is False
    assert NoveltySettings(half_life_hours=0).enabled is False
    assert NoveltySettings(gain=0).enabled is False
    assert NoveltySettings(threshold=-1, gain=-1).enabled is False


def test_tokenize_extracts_ngrams_and_filters_noise():
    tokens = tokenize(TOPIC_A)
    assert {"赛事", "冠军", "预测", "cs2"} <= tokens
    # 单字 CJK 段与长度 >= 2 的拉丁/数字词元
    assert "站" in tokenize("B站")
    assert "cs2" in tokenize("CS2")
    # 纯 UI/噪声标题、纯符号、空文本都不产生词元
    for text in (ALL_NOISE_TITLE, "首页", "推荐 关注", "!!! ???", "😀😀", "", "   "):
        assert tokenize(text) == frozenset()


def test_record_accumulates_fatigue_and_hits_threshold(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(threshold=60.0, gain=20.0)

    first = store.record("哔哩哔哩", TOPIC_A, settings)
    assert isinstance(first, NoveltyMatch)
    assert first.is_new is True
    assert first.entry is not None
    assert first.entry.label == TOPIC_A
    assert first.entry.app == "哔哩哔哩"
    assert first.entry.hits == 1
    assert first.similarity == 0.0
    assert first.fatigue == pytest.approx(20.0)
    assert first.bored is False

    second = store.record("哔哩哔哩", TOPIC_A, settings)
    assert second.is_new is False
    assert second.entry is not None and second.entry.hits == 2
    assert second.similarity == pytest.approx(1.0)
    assert second.fatigue == pytest.approx(40.0)
    assert second.bored is False

    third = store.record("哔哩哔哩", TOPIC_A, settings)
    assert third.entry is not None and third.entry.hits == 3
    assert third.fatigue == pytest.approx(60.0)
    assert third.bored is True

    # evaluate 不写盘，但能看到衰减后的疲劳度与看腻状态
    evaluated = store.evaluate("哔哩哔哩", TOPIC_A, settings)
    assert evaluated.is_new is False
    assert evaluated.bored is True
    assert evaluated.fatigue == pytest.approx(60.0)
    assert evaluated.entry is not None and evaluated.entry.hits == 3

    entries = store.entries(settings)
    assert len(entries) == 1
    assert entries[0] == evaluated.entry


def test_similar_titles_merge_but_similarity_threshold_is_respected(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    store.record("哔哩哔哩", TOPIC_A, _settings())

    similar = store.evaluate("哔哩哔哩", TOPIC_A_SIMILAR, _settings())
    assert similar.is_new is False
    assert similar.similarity == pytest.approx(3 / 5)
    recorded = store.record("哔哩哔哩", TOPIC_A_SIMILAR, _settings())
    assert recorded.is_new is False
    assert len(store.entries(_settings())) == 1

    # 相似度 0.6 的重合不算同类主题（用独立档案验证）
    strict = _settings(similarity=0.9)
    strict_store = NoveltyStore(tmp_path / "strict", clock=clock)
    strict_store.record("哔哩哔哩", TOPIC_A, strict)
    strict_match = strict_store.evaluate("哔哩哔哩", TOPIC_A_SIMILAR, strict)
    assert strict_match.is_new is True and strict_match.entry is None
    strict_store.record("哔哩哔哩", TOPIC_A_SIMILAR, strict)
    assert len(strict_store.entries(strict)) == 2


def test_different_titles_do_not_match(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()
    store.record("哔哩哔哩", TOPIC_A, settings)

    match = store.evaluate("哔哩哔哩", TOPIC_B, settings)
    assert match.entry is None
    assert match.is_new is True
    assert match.similarity == 0.0
    assert match.fatigue == 0.0
    assert match.bored is False

    recorded = store.record("哔哩哔哩", TOPIC_B, settings)
    assert recorded.is_new is True
    entries = store.entries(settings)
    assert len(entries) == 2
    assert len({entry.key for entry in entries}) == 2
    assert {entry.label for entry in entries} == {TOPIC_A, TOPIC_B}


def test_half_life_decay_after_24h(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(gain=20.0, half_life_hours=24.0)

    store.record("哔哩哔哩", TOPIC_A, settings)
    assert store.entries(settings)[0].fatigue == pytest.approx(20.0)

    clock.advance(24 * 3600)
    entries = store.entries(settings)
    assert entries[0].fatigue == pytest.approx(10.0)
    match = store.evaluate("哔哩哔哩", TOPIC_A, settings)
    assert match.is_new is False
    assert match.fatigue == pytest.approx(10.0)
    assert match.bored is False

    clock.advance(24 * 3600)
    assert store.entries(settings)[0].fatigue == pytest.approx(5.0)


def test_record_after_decay_continues_from_decayed_value(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(gain=20.0, half_life_hours=24.0)
    store.record("哔哩哔哩", TOPIC_A, settings)
    clock.advance(24 * 3600)
    again = store.record("哔哩哔哩", TOPIC_A, settings)
    assert again.is_new is False
    assert again.fatigue == pytest.approx(30.0)
    assert again.entry is not None and again.entry.hits == 2


def test_threshold_zero_disabled_writes_nothing(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    disabled = NoveltySettings(threshold=0)

    match = store.record("哔哩哔哩", TOPIC_A, disabled)
    assert match.entry is None and match.is_new is False
    assert match.bored is False and match.fatigue == 0.0
    assert not (tmp_path / NOVELTY_FILENAME).exists()
    assert store.entries(disabled) == []
    assert store.render_for_prompt(disabled) == ""
    assert "未启用" in store.status_text(disabled)
    assert store.evaluate("哔哩哔哩", TOPIC_A, disabled).is_new is False

    # 半衰期为 0 同样视为禁用，不写盘
    assert not (tmp_path / NOVELTY_FILENAME).exists()
    store.record("哔哩哔哩", TOPIC_A, NoveltySettings(half_life_hours=0))
    assert not (tmp_path / NOVELTY_FILENAME).exists()


def test_persistence_and_reload(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()
    store.record("哔哩哔哩", TOPIC_A, settings)
    store.record("哔哩哔哩", TOPIC_A, settings)
    assert store.entries(settings)[0].fatigue == pytest.approx(40.0)

    payload = _payload(tmp_path)
    assert payload["version"] == 1
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["hits"] == 2

    reloaded = NoveltyStore(tmp_path, clock=clock)
    entries = reloaded.entries(settings)
    assert len(entries) == 1
    assert entries[0].key == payload["entries"][0]["key"]
    assert entries[0].fatigue == pytest.approx(40.0)
    assert entries[0].hits == 2
    assert entries[0].app == "哔哩哔哩"
    again = reloaded.record("哔哩哔哩", TOPIC_A, settings)
    assert again.entry is not None and again.entry.hits == 3
    assert again.fatigue == pytest.approx(60.0)


def test_corrupt_json_is_tolerated(tmp_path):
    clock = _Clock()
    settings = _settings()
    path = tmp_path / NOVELTY_FILENAME
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    store = NoveltyStore(tmp_path, clock=clock)
    assert store.entries(settings) == []
    assert store.stats(settings)["entries"] == 0
    assert store.reset() == 0
    assert _payload(tmp_path)["entries"] == []

    # 结构合法但条目类型错乱时逐条跳过
    path.write_text(
        json.dumps({"version": 1, "entries": [{"bad": 1}, "x", 3, None]}),
        encoding="utf-8",
    )
    broken = NoveltyStore(tmp_path, clock=clock)
    assert broken.entries(settings) == []
    record = broken.record("哔哩哔哩", TOPIC_A, settings)
    assert record.is_new is True
    assert len(_payload(tmp_path)["entries"]) == 1


def test_noise_and_symbol_titles_are_ignored(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()

    for text in (ALL_NOISE_TITLE, "首页", "!!! ???", "😀😀", "", "   "):
        match = store.evaluate("抖音", text, settings)
        assert match.entry is None
        assert match.is_new is False
        recorded = store.record("抖音", text, settings)
        assert recorded.entry is None
        assert recorded.is_new is False
    assert not (tmp_path / NOVELTY_FILENAME).exists()
    assert store.render_for_prompt(settings) == ""


def test_capacity_eviction_prefers_lowest_fatigue_then_oldest(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(max_entries=2)
    store.record("哔哩哔哩", TOPIC_A, settings)
    clock.advance(1)
    store.record("哔哩哔哩", TOPIC_A, settings)  # A ≈ 40，命中 2 次
    clock.advance(1)
    store.record("哔哩哔哩", TOPIC_B, settings)  # B = 20，last_seen 较早
    clock.advance(1)
    store.record("哔哩哔哩", TOPIC_C, settings)  # C = 20，触发淘汰

    entries = store.entries(settings)
    assert len(entries) == 2
    labels = {entry.label for entry in entries}
    assert labels == {TOPIC_A, TOPIC_C}
    assert TOPIC_B not in labels
    assert entries[0].fatigue >= entries[1].fatigue
    assert len(_payload(tmp_path)["entries"]) == 2


def test_render_for_prompt_bored_and_near_threshold(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(threshold=60.0, gain=20.0)
    for _ in range(3):
        store.record("哔哩哔哩", TOPIC_A, settings)  # 60 -> 已看腻
    for _ in range(2):
        store.record("哔哩哔哩", TOPIC_B, settings)  # 40 -> 接近阈值

    render = store.render_for_prompt(settings)
    assert render.startswith("【内容新鲜感】阈值 60/100")
    assert f"「{TOPIC_A}」疲劳 60/100" in render
    assert "已看腻" in render
    assert f"「{TOPIC_B}」疲劳 40/100" in render
    assert "接近阈值" in render
    assert "自然恢复新鲜感" in render
    assert len(render) <= 600

    assert store.render_for_prompt(_settings(inject_limit=1)).count("- 「") == 1
    assert store.render_for_prompt(_settings(inject_limit=0)) == ""

    # 阈值提高到 100 后 floor=60：60 分的 A 仍以「接近阈值」出现，40 分的 B 被过滤
    higher = store.render_for_prompt(_settings(threshold=100.0))
    assert f"「{TOPIC_A}」疲劳 60/100" in higher
    assert TOPIC_B not in higher


def test_render_empty_when_fatigue_below_floor(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(threshold=60.0, gain=20.0)
    store.record("哔哩哔哩", TOPIC_A, settings)  # 20 < 60*0.6
    assert store.render_for_prompt(settings) == ""


def test_status_text_and_stats(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings(threshold=60.0, gain=20.0)
    for _ in range(3):
        store.record("哔哩哔哩", TOPIC_A, settings)

    status = store.status_text(settings)
    for fragment in (
        "内容新鲜感：已启用",
        "阈值：60",
        "半衰期：24",
        "增益：每次记录 +20",
        "相似度阈值 0.15",
        "条目：1 条",
        "已看腻 1 条",
        "Top 5",
        TOPIC_A,
    ):
        assert fragment in status

    stats = store.stats(settings)
    assert stats["enabled"] is True
    assert stats["entries"] == 1
    assert stats["bored"] == 1
    assert stats["max_entries"] == DEFAULT_MAX_ENTRIES
    assert stats["inject_limit"] == 5
    assert stats["max_fatigue"] == MAX_FATIGUE
    assert stats["avg_fatigue"] == pytest.approx(60.0)
    assert str(stats["path"]).endswith(NOVELTY_FILENAME)

    disabled_stats = store.stats(NoveltySettings(threshold=0))
    assert disabled_stats["enabled"] is False


def test_reset_clears_entries_and_file(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()
    store.record("哔哩哔哩", TOPIC_A, settings)
    store.record("哔哩哔哩", TOPIC_B, settings)
    assert len(store.entries(settings)) == 2

    assert store.reset() == 2
    assert store.entries(settings) == []
    assert store.render_for_prompt(settings) == ""
    assert _payload(tmp_path)["entries"] == []
    assert store.reset() == 0


def test_label_and_title_are_truncated(tmp_path):
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()
    long_text = "长" * 300
    match = store.record("哔哩哔哩", long_text, settings)
    assert isinstance(match.entry, NoveltyEntry)
    assert len(match.entry.label) == 40
    assert len(match.entry.title) == 200
    assert match.entry.first_seen == match.entry.last_seen


def test_long_titles_with_shared_topic_tokens_merge(tmp_path):
    """长标题 Jaccard 被拉低时，只要共享 >=2 个主题词元仍算同类内容。"""
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()
    first = store.record("哔哩哔哩", "CS2 赛事 精彩集锦", settings)
    assert first.is_new is True
    second = store.evaluate("哔哩哔哩", "CS2 赛事 决赛回放", settings)
    assert second.is_new is False
    assert second.entry is not None and second.entry.hits == 1
    assert second.similarity >= settings.similarity


def test_single_shared_generic_token_does_not_merge(tmp_path):
    """只共享一个通用词元（教学/日常）时不应把无关主题合并。"""
    clock = _Clock()
    store = NoveltyStore(tmp_path, clock=clock)
    settings = _settings()
    store.record("哔哩哔哩", "数学教学 函数精讲", settings)
    match = store.evaluate("哔哩哔哩", "英语教学 语法精讲", settings)
    assert match.is_new is True
    assert match.entry is None
    assert len(store.entries(settings)) == 1

