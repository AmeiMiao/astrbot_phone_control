"""信息流浏览会话：打开 App → 稳定 → 逐屏采集卡片/条目 → 滑动翻页。

采集优先走 App 画像的**卡片级抽取**：B 站信息流卡片的 content-desc 是结构化文本
（``视频,<标题>,<播放>观看,<弹幕>弹幕,时长…,UP主<作者>[,点赞][,竖屏]``），一屏可
直接解析出多条视频；content-desc 缺失时用 resource-id 兜底（大卡标题 / 双列卡
标题+作者）；两者都没有则回退到"一屏一条文本摘要"的通用逻辑。

跨屏按条目指纹去重（B 站单次上滑约 600px，翻页重叠 4~6 条），达到目标条数即停；
连续多屏没有新内容视为滑动失效，提前结束并在 ``errors`` 中说明。单步失败只记入
``errors``，不中断整体流程；日记写入由调用方（``PhoneService``）决定。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .apps import AppProfile, resolve_app
from .config import DEFAULT_SETTLE_SECONDS, BrowseSettings
from .errors import (
    AppNotForegroundError,
    AppNotInstalledError,
    BrowseError,
    PhoneControlError,
)
from .models import BrowseRequest, BrowseResult, FeedItem, ScreenSnapshot, UiNode

_module_logger = logging.getLogger("astrbot_phone_control.browse")

_AUTHOR_PATTERNS = (
    re.compile(r"^@(.{1,32})$"),
    re.compile(r"^(?:作者|UP主|up主|发布者|来稿)[：:]\s*(.{1,32})$"),
)
_MAX_ERRORS = 10
_MAX_CONSECUTIVE_ERRORS = 3
# 连续多屏没有新内容（或解析不出条目）时提前结束，避免空转。
_NO_NEW_BREAK_STREAK = 3
_MAX_CARDS_HARD_LIMIT = 20
# B 站 dump 实测 3.8~13s，等待稳定的总超时不能低于 20s。
_DUMP_TIMEOUT_FLOOR = 20.0
# dump 失败后的重试间隔（抖音/小黑盒实测需等待 1.5~2s 再试）。
_DUMP_RETRY_DELAY = 1.8
# dump 前单击暂停自动播放后的稳定等待。
_PAUSE_SETTLE_SECONDS = 0.3
# 跳过登录/引导页：点击前等待、点击后复查、总超时与轮询间隔。
_SKIP_LOGIN_TAP_DELAY = 1.0
_SKIP_LOGIN_RECHECK_DELAY = 2.0
_SKIP_LOGIN_TIMEOUT = 10.0
_SKIP_LOGIN_POLL_INTERVAL = 0.5
# 启动页 Activity 关键字：等待登录页出现期间允许停留。
_LAUNCH_ACTIVITY_HINTS = ("splash", "launch")


def item_signature(texts: list[str]) -> str:
    """按文本集合生成稳定的条目指纹。"""
    normalized = [text.strip().lower() for text in texts if text and text.strip()]
    payload = "\x00".join(normalized)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _guess_author(texts: list[str]) -> str:
    """从条目文本里猜测作者名；猜不出时返回空串。"""
    for text in texts:
        for pattern in _AUTHOR_PATTERNS:
            match = pattern.match(text)
            if match:
                return match.group(1).strip()
    return ""


def _split_res_ids(spec: str) -> tuple[str, ...]:
    """把 ``a|b`` 形式的 resource-id 候选项拆成元组。"""
    return tuple(part.strip() for part in str(spec or "").split("|") if part.strip())


def _match_res_id(resource_id: str, candidates: tuple[str, ...]) -> bool:
    """resource-id 精确匹配或以候选项结尾（允许带包名前缀的简写）。"""
    if not resource_id or not candidates:
        return False
    return any(
        resource_id == candidate or resource_id.endswith(candidate)
        for candidate in candidates
    )


class BrowseSession:
    """一次浏览请求的执行器。"""

    def __init__(
        self,
        actions: Any,
        *,
        settings: BrowseSettings | None = None,
        logger: Callable[[str], None] | None = None,
        screenshot_dir: str | Path | None = None,
    ) -> None:
        """初始化浏览会话。

        Args:
            actions: ``core.actions.DeviceActions`` 实例（测试可传鸭子类型）。
            settings: 浏览参数快照。
            logger: 日志输出回调（默认写标准库 logger）。
            screenshot_dir: 截图落盘目录；默认放系统临时目录下。
        """
        self.actions = actions
        self.settings = settings or BrowseSettings()
        self._log = logger or (lambda message: _module_logger.info(message))
        self.screenshot_dir = (
            Path(screenshot_dir)
            if screenshot_dir is not None
            else Path(tempfile.gettempdir()) / "astrbot_phone_control" / "screenshots"
        )

    # ---- 对外入口 -----------------------------------------------------

    async def run(self, request: BrowseRequest) -> BrowseResult:
        """执行一次浏览并返回结果。

        Args:
            request: 浏览请求（App、条数、是否截图/记忆）。

        Returns:
            采集结果；单步失败记录在 ``errors`` 中。

        Raises:
            BrowseError: App 无法识别、未指定当前应用或打开失败。
            AppNotInstalledError: 目标 App 未安装。
            AppNotForegroundError: 打开后目标 App 不在前台。
        """
        profile = resolve_app(request.app)
        if profile is None:
            raise BrowseError(
                f"未知 App：{request.app}，可用 douyin/bilibili/xiaoheihe 或包名"
            )
        started_at = time.time()
        started = time.monotonic()
        result = BrowseResult(app=profile.label or profile.key, started_at=started_at)

        profile = await self._prepare(profile)
        target = self._resolve_item_count(request)
        collect_screenshots = bool(
            request.collect_screenshots and self.settings.collect_screenshots
        )
        seen: set[str] = set()
        rounds = 0
        max_rounds = max(target + 3, 6)
        no_new_streak = 0
        consecutive_errors = 0
        while len(result.items) < target and rounds < max_rounds:
            rounds += 1
            result.steps = rounds
            paused = False
            try:
                if profile.pause_before_dump:
                    # 视频自动播放时 uiautomator 永远等不到 idle，先单击暂停。
                    await self._tap_ratio(profile.pause_tap_ratio)
                    paused = True
                    await asyncio.sleep(_PAUSE_SETTLE_SECONDS)
                snapshot = await self._snapshot_with_retry(profile)
                added = await self._ingest(
                    profile, snapshot, result, seen, target, collect_screenshots
                )
                consecutive_errors = 0
                if paused:
                    await self._resume_playback(profile)
                    paused = False
                if len(result.items) >= target:
                    break
                if added == 0:
                    no_new_streak += 1
                    if no_new_streak >= _NO_NEW_BREAK_STREAK:
                        result.errors.append("连续多屏没有新内容，提前结束本次浏览")
                        break
                else:
                    no_new_streak = 0
                await self._advance(profile)
                await asyncio.sleep(self._settle(profile))
            except PhoneControlError as exc:
                consecutive_errors += 1
                self._record_error(result, rounds, exc)
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    result.errors.append("连续失败，提前结束本次浏览")
                    break
                await asyncio.sleep(self._settle(profile))
            except asyncio.TimeoutError:
                consecutive_errors += 1
                self._record_error(result, rounds, BrowseError("等待界面稳定超时"))
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    result.errors.append("连续失败，提前结束本次浏览")
                    break
                await asyncio.sleep(self._settle(profile))
            finally:
                if paused:
                    await self._resume_playback(profile)

        result.elapsed = round(time.monotonic() - started, 2)
        self._log(
            f"浏览 {profile.label} 完成：目标 {target} 条，实际 {len(result.items)} 条，"
            f"耗时 {result.elapsed:.1f}s，异常 {len(result.errors)} 条"
        )
        return result

    # ---- 各阶段 -------------------------------------------------------

    async def _prepare(self, profile: AppProfile) -> AppProfile:
        """校验安装并打开目标 App，返回绑定实际包名的画像。"""
        package = profile.package
        if not package:
            foreground_package, _ = await self.actions.foreground()
            if not foreground_package:
                raise BrowseError("未指定 App 且无法识别当前前台应用")
            package = str(foreground_package)
            profile = replace(profile, package=package)
        if not await self.actions.is_installed(package):
            raise AppNotInstalledError(f"{profile.label}（{package}）未安装")
        await self.actions.open_app(package)
        await self._wait_foreground(profile, package)
        await self._skip_login_if_needed(profile)
        return profile

    async def _wait_foreground(
        self, profile: AppProfile, package: str, *, timeout: float = 8.0
    ) -> None:
        """等待目标 App 进入前台；被弹窗/系统界面挡住时抛错。"""
        deadline = time.monotonic() + timeout
        foreground_package = ""
        while time.monotonic() < deadline:
            foreground_package, _ = await self.actions.foreground()
            if foreground_package == package:
                return
            await asyncio.sleep(0.5)
        raise AppNotForegroundError(
            f"打开 {profile.label} 后前台是 {foreground_package or '未知'}，可能被系统弹窗拦截"
        )

    async def _skip_login_if_needed(self, profile: AppProfile) -> None:
        """打开后若停在登录/引导页，单击跳过按钮进入游客信息流。

        小黑盒冷启动必经 SplashActivity → RegisterOrLoginActivityV2，需要单击
        左上角 X；不能按 BACK（会退出 App）。先等登录页出现（启动页可能还在），
        命中后点击并复查 Activity；超过 _SKIP_LOGIN_TIMEOUT 仍未离开则报错。
        """
        needle = str(profile.skip_login_activity or "").strip().lower()
        if not needle:
            return
        deadline = time.monotonic() + _SKIP_LOGIN_TIMEOUT
        activity = ""
        saw_login = False
        while time.monotonic() < deadline:
            _package, activity = await self.actions.foreground()
            current = str(activity or "").lower()
            if needle in current:
                saw_login = True
                self._log(f"{profile.label} 命中登录页 {activity}，点击跳过")
                await asyncio.sleep(_SKIP_LOGIN_TAP_DELAY)
                await self._tap_ratio(profile.skip_login_tap_ratio)
                await asyncio.sleep(_SKIP_LOGIN_RECHECK_DELAY)
                continue
            if saw_login:
                self._log(f"{profile.label} 已跳过登录页，当前 {activity or '未知'}")
                return
            if current and not any(
                token in current for token in _LAUNCH_ACTIVITY_HINTS
            ):
                return
            await asyncio.sleep(_SKIP_LOGIN_POLL_INTERVAL)
        if saw_login:
            raise BrowseError(
                f"{profile.label} 仍停留在登录/引导页（{activity or '未知'}），"
                "请先在模拟器上完成登录或手动关闭登录页"
            )

    async def _tap_ratio(self, ratio: tuple[float, float]) -> None:
        """按屏占比点击（暂停/恢复、跳过登录等固定位置点击）。"""
        width, height = await self.actions.screen_size()
        x = max(0, min(width - 1, int(width * float(ratio[0]))))
        y = max(0, min(height - 1, int(height * float(ratio[1]))))
        await self.actions.tap(x, y)

    async def _resume_playback(self, profile: AppProfile) -> None:
        """dump 完成后单击恢复自动播放（失败只记录，不影响结果）。"""
        try:
            await self._tap_ratio(profile.pause_tap_ratio)
        except PhoneControlError as exc:
            self._log(f"恢复播放失败：{exc}")

    async def _snapshot_with_retry(self, profile: AppProfile) -> ScreenSnapshot:
        """带重试的稳定快照：dump 失败（idle/Killed 等）等待后重试。"""
        attempts = max(1, int(profile.dump_retries) + 1)
        last_error: PhoneControlError | None = None
        for attempt in range(attempts):
            try:
                return await self._snapshot(profile)
            except PhoneControlError as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                self._log(
                    f"dump 失败，{_DUMP_RETRY_DELAY:.1f}s 后重试"
                    f"（{attempt + 1}/{attempts - 1}）：{exc}"
                )
                await asyncio.sleep(_DUMP_RETRY_DELAY)
        if last_error is not None:
            raise last_error
        raise UiDumpError("dump 失败且没有可用快照")

    def _resolve_item_count(self, request: BrowseRequest) -> int:
        settings = self.settings
        try:
            count = int(request.items or 0)
        except (TypeError, ValueError):
            count = settings.default_items
        if count <= 0:
            count = settings.default_items
        return max(1, min(count, settings.max_items))

    async def _snapshot(self, profile: AppProfile) -> ScreenSnapshot:
        """取一张稳定快照；dump 慢的 App 给足超时（B 站实测 3.8~13s）。"""
        timeout = max(_DUMP_TIMEOUT_FLOOR, self._settle(profile) * 8)
        return await self.actions.wait_until_stable(timeout=timeout)

    async def _advance(self, profile: AppProfile) -> None:
        duration_ms = profile.swipe_duration_ms or self.settings.swipe_duration_ms
        await self.actions.swipe_direction(
            profile.feed_scroll_direction,
            duration_ms=duration_ms,
        )

    def _settle(self, profile: AppProfile) -> float:
        """滑动后的等待节奏。

        全局配置保持默认值时使用 App 画像的校准值（真机实测回填），用户改动
        全局配置后以配置为准，便于现场按网络/性能调快或调慢。
        """
        configured = self.settings.settle_seconds
        if configured > 0 and abs(configured - DEFAULT_SETTLE_SECONDS) > 1e-9:
            return max(0.2, configured)
        return max(0.2, profile.settle_seconds or configured)

    async def _attach_screenshot(self, item: FeedItem, profile: AppProfile) -> None:
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}_{profile.key}_{item.signature[:8]}.png"
            path = await self.actions.screenshot(self.screenshot_dir / name)
            item.screenshot_path = str(path)
        except PhoneControlError as exc:
            self._log(f"截图失败，已跳过：{exc}")

    async def _ingest(
        self,
        profile: AppProfile,
        snapshot: ScreenSnapshot,
        result: BrowseResult,
        seen: set[str],
        target: int,
        collect_screenshots: bool,
    ) -> int:
        """把一屏解析出的条目加入结果，返回新增条数。"""
        added = 0
        for item in self._extract_items(profile, snapshot):
            if len(result.items) >= target:
                break
            if not item.signature or item.signature in seen:
                continue
            if collect_screenshots:
                await self._attach_screenshot(item, profile)
            seen.add(item.signature)
            result.items.append(item)
            added += 1
        return added

    @staticmethod
    def _record_error(result: BrowseResult, index: int, exc: Exception) -> None:
        if len(result.errors) < _MAX_ERRORS:
            result.errors.append(f"第 {index} 屏失败：{exc}")

    # ---- 条目提取（卡片优先，文本兜底） -------------------------------

    def _extract_items(
        self, profile: AppProfile, snapshot: ScreenSnapshot
    ) -> list[FeedItem]:
        """一屏可产出多条：结构化 desc → resource-id 兜底 → 文本摘要。"""
        cards: list[FeedItem] = []
        if profile.card_desc_re:
            cards = self._extract_desc_cards(profile, snapshot)
        if not cards and (profile.title_res_id or profile.title_res_id_fallback):
            cards = self._extract_resid_cards(profile, snapshot)
        if cards:
            limit = profile.max_cards_per_screen
            if limit <= 0:
                limit = _MAX_CARDS_HARD_LIMIT
            return cards[: max(1, min(limit, _MAX_CARDS_HARD_LIMIT))]
        fallback = self._extract_summary_item(profile, snapshot)
        return [fallback] if fallback is not None else []

    def _extract_desc_cards(
        self, profile: AppProfile, snapshot: ScreenSnapshot
    ) -> list[FeedItem]:
        """按 content-desc 的结构化规则解析卡片（B 站主路径）。"""
        try:
            pattern = re.compile(profile.card_desc_re)
        except re.error as exc:
            self._log(f"card_desc_re 非法，跳过卡片抽取：{exc}")
            return []
        bounds = self._content_bounds(profile, snapshot)
        cards: list[FeedItem] = []
        signatures: set[str] = set()
        for node in sorted(
            snapshot.nodes, key=lambda node: (node.bounds.top, node.bounds.left)
        ):
            desc = (node.desc or "").strip()
            if not desc or not self._in_bounds(node, bounds):
                continue
            match = pattern.match(desc)
            if match is None:
                continue
            groups = {
                key: (value or "").strip()
                for key, value in match.groupdict().items()
            }
            title = groups.get("title", "")
            if not title:
                continue
            author = groups.get("author", "")
            meta: dict[str, Any] = {
                key: value
                for key, value in groups.items()
                if key not in {"title", "author"} and value
            }
            meta["raw_desc"] = desc
            meta["package"] = snapshot.package
            meta["screen_id"] = snapshot.screen_id
            signature = item_signature([title, author, meta.get("duration", "")])
            if signature in signatures:
                continue
            signatures.add(signature)
            cards.append(
                FeedItem(
                    app=profile.key,
                    signature=signature,
                    texts=[part for part in (title, author) if part],
                    title=title,
                    author=author,
                    meta=meta,
                    captured_at=snapshot.captured_at,
                )
            )
            if len(cards) >= _MAX_CARDS_HARD_LIMIT:
                break
        return cards

    def _extract_resid_cards(
        self, profile: AppProfile, snapshot: ScreenSnapshot
    ) -> list[FeedItem]:
        """resource-id 抽取：主标题 + 兜底标题（纯文字帖首行），并匹配作者。

        抖音/小黑盒等没有结构化 content-desc 的信息流走这里：主标题资源 id
        （``title_res_id``）优先，兜底资源 id（``title_res_id_fallback``）只在卡片
        附近没有主标题时使用（如小黑盒纯文字短帖没有 tv_title，取 tv_content 首行）。
        作者节点按 ``author_prefix`` 过滤并去掉前缀（抖音作者以 ``@`` 开头，同一 id
        下的『相关搜索』等噪声被前缀过滤掉）。
        """
        title_ids = _split_res_ids(profile.title_res_id)
        fallback_ids = _split_res_ids(profile.title_res_id_fallback)
        if not title_ids and not fallback_ids:
            return []
        author_ids = _split_res_ids(profile.author_res_id)
        author_prefix = str(profile.author_prefix or "")
        bounds = self._content_bounds(profile, snapshot)
        excludes = self._compile_excludes(profile)
        width = snapshot.width or 1080
        height = snapshot.height or 2400
        slack = max(60.0, width * 0.08)
        card_dy = max(60.0, height * 0.10)

        primary_titles: list[UiNode] = []
        fallback_titles: list[UiNode] = []
        author_nodes: list[UiNode] = []
        for node in snapshot.nodes:
            if not self._in_bounds(node, bounds):
                continue
            text = (node.text or "").strip()
            if not text:
                continue
            if title_ids and _match_res_id(node.resource_id, title_ids):
                if not self._is_noise(profile, node, [text], excludes):
                    primary_titles.append(node)
            elif fallback_ids and _match_res_id(node.resource_id, fallback_ids):
                if not self._is_noise(profile, node, [text], excludes):
                    fallback_titles.append(node)
            if author_ids and _match_res_id(node.resource_id, author_ids):
                author_text = text
                if author_prefix:
                    if not author_text.startswith(author_prefix):
                        continue
                    author_text = author_text[len(author_prefix) :].strip()
                    if not author_text:
                        continue
                author_nodes.append(replace(node, text=author_text))
        if not primary_titles and not fallback_titles:
            return []

        def _near_primary_title(node: UiNode) -> bool:
            """兜底标题是否与某张已有主标题的卡片过近（同一张卡片）。"""
            node_y = node.bounds.center_y
            for primary in primary_titles:
                if abs(primary.bounds.center_y - node_y) > card_dy:
                    continue
                if node.bounds.right < primary.bounds.left - slack:
                    continue
                if node.bounds.left > primary.bounds.right + slack:
                    continue
                return True
            return False

        candidates: list[tuple[UiNode, str, str]] = []
        for node in primary_titles:
            candidates.append((node, (node.text or "").strip(), "res_id"))
        for node in fallback_titles:
            if _near_primary_title(node):
                continue
            text = (node.text or "").strip()
            first_line = next(
                (line.strip() for line in text.splitlines() if line.strip()), ""
            )
            if first_line:
                candidates.append((node, first_line, "res_id_fallback"))
        if not candidates:
            return []
        candidates.sort(key=lambda item: (item[0].bounds.top, item[0].bounds.left))

        used_authors: set[int] = set()
        cards: list[FeedItem] = []
        signatures: set[str] = set()
        for node, title, source in candidates:
            author = ""
            author_index = self._pick_author(node, author_nodes, used_authors, snapshot)
            if author_index is not None:
                used_authors.add(author_index)
                author = (author_nodes[author_index].text or "").strip()
            signature = item_signature([title, author])
            if signature in signatures:
                continue
            signatures.add(signature)
            cards.append(
                FeedItem(
                    app=profile.key,
                    signature=signature,
                    texts=[part for part in (title, author) if part],
                    title=title,
                    author=author,
                    meta={
                        "source": source,
                        "title_res_id": node.resource_id,
                        "package": snapshot.package,
                        "screen_id": snapshot.screen_id,
                    },
                    captured_at=snapshot.captured_at,
                )
            )
            if len(cards) >= _MAX_CARDS_HARD_LIMIT:
                break
        return cards

    @staticmethod
    def _pick_author(
        title_node: UiNode,
        author_nodes: list[UiNode],
        used: set[int],
        snapshot: ScreenSnapshot,
    ) -> int | None:
        """为标题节点匹配最近的作者节点：同列、垂直距离受限（上下均可）。"""
        if not author_nodes:
            return None
        width = snapshot.width or 1080
        height = snapshot.height or 2400
        title_x, title_y = title_node.bounds.center
        max_dy = max(80.0, height * 0.18)
        # 作者与标题横向需有重叠（大卡标题可横跨全屏，双列卡各占半屏；允许少量溢出）。
        # 抖音/小黑盒的作者在标题上方，B 站大卡在下方，因此上下都接受，按距离择优。
        slack = max(60.0, width * 0.08)
        best_index: int | None = None
        best_score = float("inf")
        for index, node in enumerate(author_nodes):
            if index in used:
                continue
            author_x, author_y = node.bounds.center
            dy = abs(author_y - title_y)
            if dy > max_dy:
                continue
            if node.bounds.right < title_node.bounds.left - slack:
                continue
            if node.bounds.left > title_node.bounds.right + slack:
                continue
            score = abs(author_x - title_x) * 2 + dy
            if score < best_score:
                best_score = score
                best_index = index
        return best_index

    def _extract_summary_item(
        self, profile: AppProfile, snapshot: ScreenSnapshot
    ) -> FeedItem | None:
        """通用兜底：把一屏正文文本聚合成一条摘要条目。"""
        bounds = self._content_bounds(profile, snapshot)
        excludes = self._compile_excludes(profile)
        include_desc = any(key.lower() == "desc" for key in profile.item_signature_keys)
        texts: list[str] = []
        for node in sorted(
            snapshot.nodes, key=lambda node: (node.bounds.top, node.bounds.left)
        ):
            if not self._in_bounds(node, bounds):
                continue
            labels = self._node_labels(node, include_desc=include_desc)
            if not labels:
                continue
            if self._is_noise(profile, node, labels, excludes):
                continue
            for label in labels:
                if label not in texts:
                    texts.append(label)
        if not texts:
            return None

        title = max(texts, key=len).strip()
        author = _guess_author(texts)
        return FeedItem(
            app=profile.key,
            signature=item_signature(texts),
            texts=texts,
            title=title,
            author=author,
            meta={
                "source": "summary",
                "package": snapshot.package,
                "activity": snapshot.activity,
                "screen_id": snapshot.screen_id,
                "node_count": len(snapshot.nodes),
            },
            captured_at=snapshot.captured_at,
        )

    # ---- 几何/文本辅助 -------------------------------------------------

    @staticmethod
    def _content_bounds(
        profile: AppProfile, snapshot: ScreenSnapshot
    ) -> tuple[float, float, float, float]:
        """内容区域像素边界（屏幕尺寸缺失时用节点最大边界兜底）。"""
        width = snapshot.width
        height = snapshot.height
        if width <= 0 or height <= 0:
            if not snapshot.nodes:
                return (0.0, 0.0, 0.0, 0.0)
            width = max(width, max(node.bounds.right for node in snapshot.nodes))
            height = max(height, max(node.bounds.bottom for node in snapshot.nodes))
        left_area, top_area, right_area, bottom_area = profile.content_area
        return (
            left_area * width,
            top_area * height,
            right_area * width,
            bottom_area * height,
        )

    @staticmethod
    def _in_bounds(node: UiNode, bounds: tuple[float, float, float, float]) -> bool:
        left, top, right, bottom = bounds
        center_x, center_y = node.bounds.center
        return left <= center_x <= right and top <= center_y <= bottom

    @staticmethod
    def _node_labels(node: UiNode, *, include_desc: bool) -> list[str]:
        labels: list[str] = []
        text = (node.text or "").strip()
        if text:
            labels.append(text)
        if include_desc:
            desc = (node.desc or "").strip()
            if desc and desc != text:
                labels.append(desc)
        return labels

    @staticmethod
    def _compile_excludes(profile: AppProfile) -> tuple[re.Pattern[str], ...]:
        compiled: list[re.Pattern[str]] = []
        for pattern in profile.exclude_text_re:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                continue
        return tuple(compiled)

    @staticmethod
    def _is_noise(
        profile: AppProfile,
        node: UiNode,
        labels: list[str],
        excludes: tuple[re.Pattern[str], ...],
    ) -> bool:
        haystacks = [*labels, (node.desc or "").strip()]
        for compiled in excludes:
            for haystack in haystacks:
                if haystack and compiled.search(haystack):
                    return True
        return False
