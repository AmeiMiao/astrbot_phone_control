"""报错汇报的实际发送、合并、限频与失败边界。"""

import asyncio
from types import SimpleNamespace

import pytest

from core.tool_reports import (
    ReportSettings,
    ToolErrorReporter,
    owner_targets,
    result_failed,
    safe_summary,
)


@pytest.mark.parametrize(
    "result",
    [
        None,
        "error: upstream 500",
        {"isError": True, "content": []},
        SimpleNamespace(isError=True, content=[]),
        {"ok": False},
    ],
)
def test_errors_are_detected(result):
    assert result_failed(result)


@pytest.mark.parametrize(
    "result",
    [
        "ok",
        "已中止：用户取消",
        {"content": [{"text": "normal"}]},
        SimpleNamespace(content=[SimpleNamespace(text="screen")]),
    ],
)
def test_success_and_cancel_are_not_errors(result):
    assert not result_failed(result)


def test_summary_keeps_useful_error_and_removes_credentials():
    text = safe_summary(
        "error: HTTP 500 api_key=private123 Authorization: Bearer abcdef password=pass123 https://user:pass@host/path?token=abc sk-1234567890"
    )
    assert "HTTP 500" in text
    for secret in (
        "private123",
        "abcdef",
        "pass123",
        "user:pass",
        "token=abc",
        "sk-1234567890",
    ):
        assert secret not in text


def test_share_cooldown_is_silent_but_other_share_errors_are_delivered():
    async def run():
        r, sent, _ = reporter()
        try:
            r.submit(None, "phone_share_to_user", "error: 距上次向该会话分享不足冷却时长（还需约 3 分钟）")
            assert r.task is None and not r.pending
            r.submit(None, "phone_share_to_user", "error: 消息发送失败")
            await r.task
            assert len(sent) == 1 and "消息发送失败" in sent[0][1]
        finally:
            await r.close()
    asyncio.run(run())


def test_tap_failure_notification_excludes_internal_experience_hint():
    assert safe_summary("error: 点击失败\n\n【App 经验更新】经验已失效") == "error: 点击失败"


def test_cron_uses_last_real_platform_for_private_owner_notification():
    assert owner_targets(
        target="",
        owners=("owner1", "owner2"),
        origin="cron:OtherMessage:job",
        last_session="onebot:GroupMessage:group",
    ) == ["onebot:FriendMessage:owner1", "onebot:FriendMessage:owner2"]
    assert owner_targets(
        target="test:FriendMessage:owner", owners=(), origin="", last_session=""
    ) == ["test:FriendMessage:owner"]
    assert (
        owner_targets(
            target="",
            owners=("owner",),
            origin="cron:OtherMessage:job",
            last_session="",
        )
        == []
    )


async def spin_until(condition):
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("通知任务没有到达预期状态")


def reporter(*, clock=None, send=None, settings=None, targets=None):
    delivered, logs = [], []

    async def deliver(target, text):
        delivered.append((target, text))
        return True

    r = ToolErrorReporter(
        settings or (lambda: ReportSettings()),
        targets or (lambda _: ["bot:FriendMessage:owner"]),
        send or deliver,
        logs.append,
        batch_delay=0,
        **({"clock": clock} if clock else {}),
    )
    return r, delivered, logs


def test_first_error_sent_and_later_errors_merged_until_cooldown():
    async def run():
        now = [0]
        r, sent, _ = reporter(clock=lambda: now[0])
        try:
            r.submit(None, "phone_watch_media", "error: HTTP 500")
            await r.task
            assert len(sent) == 1 and "phone_watch_media" in sent[0][1]
            for _ in range(3):
                r.submit(None, "phone_watch_media", "error: HTTP 500")
            r.submit(None, "phone_content", "error: task failed")
            await spin_until(lambda: r.status()["pending"] == 4)
            assert len(sent) == 1
            now[0] = 300
            r.wake.set()
            await r.task
            assert len(sent) == 2
            assert (
                "4 次" in sent[1][1]
                and "phone_watch_media ×3" in sent[1][1]
                and "phone_content ×1" in sent[1][1]
            )
            assert r.status()["pending"] == 0
        finally:
            await r.close()

    asyncio.run(run())


def test_sending_failure_retries_three_times_and_remains_visible():
    async def run():
        now, calls = [0], []

        async def fail(target, text):
            calls.append(text)
            return False

        r, _, logs = reporter(clock=lambda: now[0], send=fail)
        try:
            r.submit(None, "phone_watch_media", "error: upstream 500")
            await spin_until(lambda: r.failed == 1)
            for time_value, count in [(5, 2), (35, 3)]:
                now[0] = time_value
                r.wake.set()
                await spin_until(lambda: r.failed == count)
            await r.task
            assert len(calls) == 3 and r.status()["failed"] == 3
            assert any("重试已结束" in line for line in logs)
        finally:
            await r.close()

    asyncio.run(run())


def test_disabled_success_foreign_tool_and_missing_target_do_not_send():
    async def run():
        r, sent, _ = reporter(settings=lambda: ReportSettings(enabled=False))
        r.submit(None, "phone_screen", "error: failure")
        assert r.task is None
        r.settings = lambda: ReportSettings()
        r.submit(None, "phone_screen", "ok")
        r.submit(None, "other_plugin_tool", "error: failure")
        assert r.task is None
        r.targets = lambda _: []
        r.submit(None, "phone_screen", "error: failure")
        assert r.task is None and sent == []
        await r.close()

    asyncio.run(run())


def test_disable_and_close_cancel_pending_reports():
    async def run():
        r, sent, _ = reporter()
        r.submit(None, "phone_screen", "error: failure")
        r.settings = lambda: ReportSettings(enabled=False)
        await r.task
        assert not sent
        r.settings = lambda: ReportSettings()
        r.last_sent["bot:FriendMessage:owner"] = r.clock()
        r.submit(None, "phone_screen", "error: failure")
        await asyncio.sleep(0)
        await r.close()
        assert not sent and r.status()["pending"] == 0

    asyncio.run(run())
