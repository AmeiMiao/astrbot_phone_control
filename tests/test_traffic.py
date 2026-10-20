"""core.traffic 单元测试：抓包 addon 生成、JSONL 解析、打码与页面推断/渲染。"""

from __future__ import annotations

import json
import time

from core.traffic import (
    DEFAULT_BODY_CHARS,
    extract_share_links,
    DEFAULT_KEEP_SECONDS,
    DEFAULT_TRAFFIC_PORT,
    build_addon_script,
    infer_flow_hints,
    load_recent_flows,
    mask_url,
    normalize_body_chars,
    normalize_keep_seconds,
    parse_flow_line,
    prune_log,
    render_traffic,
)


def _flow(url: str, *, body: str = "", status: int = 200, ctype: str = "application/json", ts=None):
    return {
        "ts": time.time() if ts is None else ts,
        "method": "GET",
        "url": url,
        "status": status,
        "ctype": ctype,
        "reqbody": "",
        "body": body,
    }


def test_defaults():
    assert DEFAULT_TRAFFIC_PORT == 18080
    assert DEFAULT_BODY_CHARS == 1200
    assert DEFAULT_KEEP_SECONDS == 900


def test_normalize_body_chars_and_keep_seconds():
    assert normalize_body_chars(1200) == 1200
    assert normalize_body_chars(0) == 200
    assert normalize_body_chars(99999) == 8000
    assert normalize_body_chars("bad") == DEFAULT_BODY_CHARS
    assert normalize_keep_seconds(900) == 900
    assert normalize_keep_seconds(0) == 60
    assert normalize_keep_seconds(999999) == 86400
    assert normalize_keep_seconds(None) == DEFAULT_KEEP_SECONDS


def test_mask_url_hides_sensitive_query():
    url = "https://api.xiaoheihe.cn/bbs/app/feed?hkey=ABC&nonce=NNN&page=2&token=SECRET"
    masked = mask_url(url)
    assert "hkey=***" in masked
    assert "nonce=***" in masked
    assert "token=***" in masked
    assert "page=2" in masked
    assert "ABC" not in masked and "SECRET" not in masked
    # 无 query / 非法 URL 原样返回
    assert mask_url("https://api.bilibili.com/x/feed/index") == "https://api.bilibili.com/x/feed/index"
    assert mask_url("") == ""


def test_build_addon_script_contains_log_and_limits():
    source = build_addon_script("/tmp/flows.jsonl", 500)
    assert 'LOG_PATH = "/tmp/flows.jsonl"' in source
    assert "BODY_CHARS = 500" in source
    assert "class PhoneTrafficAddon" in source
    assert "addons = [PhoneTrafficAddon()]" in source
    # body 长度归一化
    assert "BODY_CHARS = 200" in build_addon_script("/tmp/x", 1)


def test_parse_flow_line_and_invalid():
    record = parse_flow_line(
        json.dumps(
            {
                "ts": 123.0,
                "method": "post",
                "url": "https://api.xiaoheihe.cn/bbs/app/feed?hkey=x",
                "status": "200",
                "ctype": "application/json",
                "body": "{}",
            }
        )
    )
    assert record is not None
    assert record["method"] == "POST"
    assert record["status"] == 200
    assert parse_flow_line("") is None
    assert parse_flow_line("not-json") is None
    assert parse_flow_line(json.dumps({"status": 200})) is None


def test_load_recent_flows_filters_since_and_limit(tmp_path):
    log = tmp_path / "flows.jsonl"
    now = time.time()
    lines = [
        json.dumps({"ts": now - 500, "method": "GET", "url": "https://a.com/old", "status": 200}),
        json.dumps({"ts": now - 30, "method": "GET", "url": "https://a.com/new", "status": 200}),
        "bad-line",
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    flows = load_recent_flows(log, limit=10, since_seconds=60, now=now)
    assert [flow["url"] for flow in flows] == ["https://a.com/new"]
    flows = load_recent_flows(log, limit=10, since_seconds=0, now=now)
    assert len(flows) == 2
    assert load_recent_flows(tmp_path / "missing.jsonl") == []


def test_prune_log_keeps_recent_lines(tmp_path):
    log = tmp_path / "flows.jsonl"
    lines = [json.dumps({"ts": i, "method": "GET", "url": f"https://a.com/{i}", "status": 200}) for i in range(50)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    removed = prune_log(log, max_bytes=1, keep_lines=10)
    assert removed == 40
    remaining = log.read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 10
    assert "https://a.com/49" in remaining[-1]
    # 未超过阈值不裁剪
    assert prune_log(log, max_bytes=10_000_000, keep_lines=10) == 0


def test_infer_flow_hints_known_apps():
    def hint(url: str):
        return infer_flow_hints({"url": url})

    assert hint("https://api.xiaoheihe.cn/bbs/app/feed/list?page=1") == "小黑盒·社区信息流"
    assert hint("https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=1") == "小黑盒·帖子详情"
    assert hint("https://api.bilibili.com/x/feed/index?build=1") == "B站·首页推荐"
    assert hint("https://api.bilibili.com/x/web-interface/view?bvid=BV1") == "B站·视频详情"
    assert hint("https://api.xiaoheihe.cn/unknown/path") == "小黑盒·接口"
    assert hint("https://api.bilibili.com/unknown") == "B站·接口"
    assert hint("https://example.com/api") == "example.com 接口"
    assert hint("") == ""


def test_render_traffic_includes_hint_body_and_masking():
    flows = [
        _flow(
            "https://api.xiaoheihe.cn/bbs/app/feed/list?page=1&hkey=SECRET",
            body='{"result":{"links":[{"title":"测试帖子标题"}]}}',
        ),
        _flow(
            "https://api.bilibili.com/x/web-interface/view?bvid=BV1",
            body="x" * 5000,
        ),
    ]
    text = render_traffic(flows, body_chars=300)
    assert "最近抓包（2 条" in text
    assert "hkey=***" in text and "SECRET" not in text
    assert "小黑盒·社区信息流" in text
    assert "测试帖子标题" in text
    assert "B站·视频详情" in text
    assert "截断，原 5000 字符" in text
    assert "多模态截图" in text


def test_render_traffic_empty():
    text = render_traffic([])
    assert "没有抓到可识别的接口流量" in text
    assert "phone_screen" in text


def test_extract_share_links_from_share_api_response():
    flow = _flow(
        "https://api.xiaoheihe.cn/bbs/app/link/share/info",
        body=(
            '{"result":{"share_url":"https://www.xiaoheihe.cn/app/bbs/link/123",'
            '"jump_url":"https://xiaoheihe.cn/share/abc",'
            '"image":"https://img.example.com/a.jpg",'
            '"nested":{"shareLink":"https://example.com/share/xyz"}}}'
        ),
    )
    links = extract_share_links(flow)
    assert "https://www.xiaoheihe.cn/app/bbs/link/123" in links
    assert "https://xiaoheihe.cn/share/abc" in links
    assert "https://example.com/share/xyz" in links
    assert not any("a.jpg" in item for item in links)


def test_extract_share_links_filters_static_and_non_json():
    assert extract_share_links(_flow("https://a.com/x", body="not-json")) == []
    assert extract_share_links(_flow("https://a.com/x", body='{"share_url":"https://a.com/x.zip"}')) == []
    assert extract_share_links(_flow("https://a.com/x", body='{"share_url":"ftp://a.com/x"}')) == []
    # 普通字段 url 也接受；重复去重
    flow = _flow(
        "https://api.bilibili.com/x/share",
        body='{"data":{"url":"https://www.bilibili.com/video/BV1xx","url":"https://www.bilibili.com/video/BV1xx"}}',
    )
    assert extract_share_links(flow) == ["https://www.bilibili.com/video/BV1xx"]


def test_render_traffic_includes_share_links_and_hint():
    flow = _flow(
        "https://api.xiaoheihe.cn/bbs/app/link/share/info",
        body='{"result":{"share_url":"https://www.xiaoheihe.cn/app/bbs/link/123"}}',
    )
    text = render_traffic([flow], body_chars=300)
    assert "可分享链接：https://www.xiaoheihe.cn/app/bbs/link/123" in text
    assert "优先用响应里的可分享链接" in text


# ---- TrafficCapture 生命周期：锁串行化、幂等、失败 reason 与退避 --------------

import asyncio  # noqa: E402 - 生命周期用例在文件末尾

import pytest  # noqa: E402

from core import traffic as traffic_module  # noqa: E402
from core.traffic_capture import TrafficCapture  # noqa: E402


class _FakeProc:
    """最小 asyncio subprocess 替身：terminate/kill 后 wait 立即返回。"""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> int:
        return int(self.returncode or 0)


class _FakeAdb:
    def __init__(self) -> None:
        self.run_calls: list[list[str]] = []
        self.shell_calls: list[str] = []

    async def run(self, args, **_kwargs):
        self.run_calls.append(list(args))
        return ""

    async def shell(self, command, **_kwargs):
        self.shell_calls.append(command)
        return "1000"


class _CaptureHarness:
    """构造可观测的 TrafficCapture 与假 mitmdump 进程工厂。"""

    def __init__(self, tmp_path, monkeypatch: pytest.MonkeyPatch, *, ca_exists=True):
        self.logs: list[str] = []
        self.procs: list[_FakeProc] = []
        self.adb = _FakeAdb()
        self.capture = TrafficCapture(
            adb=self.adb,
            data_dir=tmp_path,
            logger=self.logs.append,
        )
        if ca_exists:
            self.capture.confdir.mkdir(parents=True, exist_ok=True)
            (self.capture.confdir / traffic_module.CA_FILE_NAME).write_text(
                "-----BEGIN CERTIFICATE-----\n", encoding="utf-8"
            )

        async def _spawn(*_args, **_kwargs):
            proc = _FakeProc()
            self.procs.append(proc)
            return proc

        monkeypatch.setattr(
            "core.traffic_capture.asyncio.create_subprocess_exec", _spawn
        )
        monkeypatch.setattr(
            self.capture, "_find_mitmdump", lambda: "/usr/local/bin/mitmdump"
        )

    def patch_setup(self, monkeypatch: pytest.MonkeyPatch, behavior) -> None:
        async def _setup() -> None:
            await behavior()

        monkeypatch.setattr(self.capture, "_setup_device_locked", _setup)


def _scenario(coro):
    """把异步场景放进单个事件循环执行（asyncio.Lock 不能跨 loop 复用）。"""

    return asyncio.run(coro)


def test_start_is_idempotent_and_logs_success_after_device_setup(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup() -> None:
        harness.logs.append("setup-done")
        harness.capture._device_configured = True

    harness.patch_setup(monkeypatch, _setup)
    harness.logs.clear()

    async def _run() -> None:
        first = await harness.capture.start(port=18080)
        assert first["ok"] is True and first["running"] is True
        assert len(harness.procs) == 1
        # 成功日志必须在设备配置完成之后才输出（避免「先成功后失败」）。
        setup_index = harness.logs.index("setup-done")
        started_index = next(
            index
            for index, line in enumerate(harness.logs)
            if "抓包已启动" in line
        )
        assert started_index > setup_index
        # 运行中重复 start：幂等，不重复 spawn/清理。
        second = await harness.capture.start(port=18080)
        assert second["ok"] is True and second["running"] is True
        assert len(harness.procs) == 1
        assert harness.procs[0].terminate_calls == 0

    _scenario(_run())


def test_concurrent_start_is_serialized(tmp_path, monkeypatch) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)
    active = 0
    max_active = 0

    async def _setup() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        harness.capture._device_configured = True
        active -= 1

    harness.patch_setup(monkeypatch, _setup)

    async def _run() -> None:
        results = await asyncio.gather(
            harness.capture.start(port=18080),
            harness.capture.start(port=18080),
            harness.capture.start(port=18080),
        )
        assert all(result["ok"] is True for result in results)
        assert len(harness.procs) == 1
        assert max_active == 1
        assert harness.capture.running

    _scenario(_run())


def test_start_failure_reason_is_explicit_and_retries_with_backoff(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup_fail() -> None:
        raise RuntimeError(
            "ADB 命令失败（退出码 1）（mount: No such file or directory）"
        )

    harness.patch_setup(monkeypatch, _setup_fail)

    async def _run() -> None:
        result = await harness.capture.start(port=18080)
        assert result["ok"] is False
        # reason 必须是非空字符串，且带异常类型与原文（不能是 None）。
        assert isinstance(result["reason"], str) and result["reason"]
        assert "RuntimeError" in result["reason"]
        assert "No such file or directory" in result["reason"]
        assert result["unavailable"] is False
        assert result["retry_in"] > 0
        assert harness.capture.retry_ready is False
        assert harness.capture.running is False
        # 失败回滚：进程被清理、设备恢复到未配置状态。
        assert harness.procs[0].terminate_calls == 1
        assert harness.capture._device_configured is False
        # 退避到期后允许再次尝试。
        harness.capture._next_retry_at = 0.0

        async def _setup_ok() -> None:
            harness.capture._device_configured = True

        harness.patch_setup(monkeypatch, _setup_ok)
        retried = await harness.capture.start(port=18080)
        assert retried["ok"] is True
        assert len(harness.procs) == 2

    _scenario(_run())


def test_start_marks_deterministic_failure_and_stops_retrying(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup_no_root() -> None:
        raise RuntimeError("设备无 root，已跳过抓包（adb root 失败）")

    harness.patch_setup(monkeypatch, _setup_no_root)

    async def _run() -> None:
        first = await harness.capture.start(port=18080)
        assert first["ok"] is False
        assert first["unavailable"] is True
        assert first["reason"] and "root" in first["reason"]
        assert harness.capture.unavailable is True
        assert harness.capture.retry_ready is False
        # 确定性不可用：后续 start 直接返回原因，不再 spawn/操作设备。
        procs_before = len(harness.procs)
        adb_shells_before = len(harness.adb.shell_calls)
        second = await harness.capture.start(port=18080)
        assert second["ok"] is False
        assert second["unavailable"] is True
        assert second["reason"]
        assert len(harness.procs) == procs_before
        assert len(harness.adb.shell_calls) == adb_shells_before
        # 配置变化/用户重新开启后可清除标记再次尝试。
        harness.capture.reset_unavailable()
        assert harness.capture.unavailable is False
        assert harness.capture.retry_ready is True

    _scenario(_run())


def test_start_marks_missing_mitmdump_unavailable(tmp_path, monkeypatch) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)
    monkeypatch.setattr(harness.capture, "_find_mitmdump", lambda: "")

    async def _run() -> None:
        result = await harness.capture.start(port=18080)
        assert result["ok"] is False
        assert result["unavailable"] is True
        assert "mitmdump" in result["reason"]
        # 未启动进程；设备只做启动前残留状态清理（兜底恢复代理/CA），不进入配置流程。
        assert harness.procs == []
        assert harness.capture._device_configured is False
        assert any("http_proxy :0" in command for command in harness.adb.shell_calls)

    _scenario(_run())


def test_start_detects_mitmdump_death_during_device_setup(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup_then_die() -> None:
        harness.capture._device_configured = True
        harness.procs[0].returncode = 1  # 模拟端口被其它进程占用后退出

    harness.patch_setup(monkeypatch, _setup_then_die)
    harness.logs.clear()

    async def _run() -> None:
        result = await harness.capture.start(port=18080)
        assert result["ok"] is False
        assert result["reason"] and "退出" in result["reason"]
        # 不能出现「已启动」的成功日志。
        assert not any("抓包已启动" in line for line in harness.logs)
        assert harness.capture._device_configured is False

    _scenario(_run())


def test_stop_is_idempotent_and_restores_device_once(tmp_path, monkeypatch) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup() -> None:
        harness.capture._device_configured = True
        harness.capture._prev_proxy = ""

    harness.patch_setup(monkeypatch, _setup)

    async def _run() -> None:
        assert (await harness.capture.start(port=18080))["ok"] is True
        harness.logs.clear()
        await harness.capture.stop()
        assert harness.capture.running is False
        assert len([line for line in harness.logs if "已停止" in line]) == 1
        assert harness.procs[0].terminate_calls == 1
        commands_before = len(harness.adb.shell_calls) + len(harness.adb.run_calls)
        await harness.capture.stop()
        assert len(harness.adb.shell_calls) + len(harness.adb.run_calls) == commands_before
        assert len([line for line in harness.logs if "已停止" in line]) == 1

    _scenario(_run())


def test_cancelled_start_rolls_back_own_resources(tmp_path, monkeypatch) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)
    entered = asyncio.Event()

    async def _setup_slow() -> None:
        harness.capture._device_configured = True
        entered.set()
        await asyncio.sleep(30)

    harness.patch_setup(monkeypatch, _setup_slow)

    async def _run() -> None:
        task = asyncio.create_task(harness.capture.start(port=18080))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.capture.running is False
        assert harness.procs and harness.procs[0].terminate_calls == 1
        assert harness.capture._device_configured is False

    _scenario(_run())


def test_repeated_transient_failures_converge_to_unavailable(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup_fail() -> None:
        raise RuntimeError("ADB 命令失败（退出码 1）：transport 抖动")

    harness.patch_setup(monkeypatch, _setup_fail)

    async def _run() -> None:
        result = None
        for _ in range(4):
            harness.capture._next_retry_at = 0.0
            result = await harness.capture.start(port=18080)
            assert result["ok"] is False
            assert result["reason"]
        # 连续失败达到上限：收敛为不可用，不再无限重试。
        assert result is not None and result["unavailable"] is True
        assert harness.capture.unavailable is True
        assert "连续" in harness.capture.unavailable_reason

    _scenario(_run())


def test_ensure_device_failure_stops_own_capture_with_reason(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)

    async def _setup_ok() -> None:
        harness.capture._device_configured = True
        harness.capture._prev_proxy = ""

    harness.patch_setup(monkeypatch, _setup_ok)

    async def _run() -> None:
        assert (await harness.capture.start(port=18080))["ok"] is True

        async def _setup_fail() -> None:
            raise RuntimeError("adb reverse 建立失败（端口映射不可用）")

        harness.patch_setup(monkeypatch, _setup_fail)
        await harness.capture.ensure_device()
        # 保活重配置失败：停掉本实例抓包并记录可重试原因，不留下坏代理状态。
        assert harness.capture.running is False
        assert harness.capture._device_configured is False
        assert "adb reverse" in harness.capture.status()["error"]
        assert harness.capture.unavailable is False
        assert harness.capture.retry_ready is False

    _scenario(_run())


def test_start_reports_mitmdump_immediate_exit_without_configuring_device(
    tmp_path, monkeypatch
) -> None:
    harness = _CaptureHarness(tmp_path, monkeypatch)
    dead = _FakeProc()
    dead.returncode = 1  # 端口被占时 mitmdump 启动即退出

    async def _spawn(*_args, **_kwargs):
        harness.procs.append(dead)
        return dead

    monkeypatch.setattr(
        "core.traffic_capture.asyncio.create_subprocess_exec", _spawn
    )
    configured: list[bool] = []

    async def _setup() -> None:
        configured.append(True)

    monkeypatch.setattr(harness.capture, "_setup_device_locked", _setup)

    async def _run() -> None:
        result = await harness.capture.start(port=18080)
        assert result["ok"] is False
        assert "立即退出" in result["reason"]
        # 进程已退出时不再做设备配置，直接按临时失败退避。
        assert configured == []
        assert harness.capture._device_configured is False
        assert result["retry_in"] > 0

    _scenario(_run())
