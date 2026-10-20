"""手机流量识别（抓包）：数据模型、日志解析、页面推断与渲染（纯标准库）。

手机上通过 mitmproxy 解密 HTTP(S) 流量并写成 JSONL（见 ``traffic_capture`` 模块），
本模块负责：

* 抓包日志插件脚本（mitmdump addon）源码生成；
* 单条流量记录解析/清洗（URL 敏感参数打码、正文截断）；
* 根据域名 + 接口路径推断 App 与页面类型（如小黑盒信息流、B站视频详情）；
* 渲染成 LLM 可读的「最近抓包」文本，作为页面内容识别的第一优先来源。

图文/视频画面、布局等视觉信息仍由多模态截图识别兜底。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_TRAFFIC_PORT = 18080
DEFAULT_BODY_CHARS = 1200
MAX_BODY_CHARS = 8000
DEFAULT_KEEP_SECONDS = 900
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
FLOW_LOG_NAME = "flows.jsonl"
ADDON_NAME = "traffic_addon.py"
CA_FILE_NAME = "mitmproxy-ca-cert.pem"
FLOW_LOG_MAX_BYTES = 2_000_000
FLOW_LOG_KEEP_LINES = 300

# URL 查询参数里常见的敏感字段：写入 AI 提示词/日志前打码
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "access_key",
        "appkey",
        "key",
        "hkey",
        "nonce",
        "sign",
        "signature",
        "sessionid",
        "session_id",
        "cookie",
        "password",
        "passwd",
        "secret",
        "device_id",
        "android_id",
        "imei",
        "openid",
        "autorization",
        "authorization",
    }
)
_MASK = "***"

# (host 关键词/正则, 路径关键词, App, 页面说明)
_FLOW_HINTS: tuple[tuple[str, str, str, str], ...] = (
    ("xiaoheihe", "/bbs/app/feed", "小黑盒", "社区信息流"),
    ("xiaoheihe", "/bbs/app/feeds", "小黑盒", "社区信息流"),
    ("xiaoheihe", "/bbs/app/link/tree", "小黑盒", "帖子详情"),
    ("xiaoheihe", "/bbs/app/link/detail", "小黑盒", "帖子详情"),
    ("xiaoheihe", "/bbs/app/comment", "小黑盒", "评论区"),
    ("xiaoheihe", "/game/", "小黑盒", "游戏/榜单页"),
    ("xiaoheihe", "/bbs/app/user", "小黑盒", "个人主页"),
    ("xiaoheihe", "", "小黑盒", "接口"),
    ("bilibili", "/x/feed/index", "B站", "首页推荐"),
    ("bilibili", "/x/web-interface/view", "B站", "视频详情"),
    ("bilibili", "/x/v2/view", "B站", "视频详情"),
    ("bilibili", "/x/v2/reply", "B站", "评论区"),
    ("bilibili", "/x/web-interface/search", "B站", "搜索"),
    ("bilibili", "/x/web-interface/nav", "B站", "个人/登录状态"),
    ("bilibili", "", "B站", "接口"),
    ("douyin", "/aweme/v1/feed", "抖音", "推荐信息流"),
    ("douyin", "/aweme/v1/comment", "抖音", "评论区"),
    ("douyin", "", "抖音", "接口"),
    ("weibo", "", "微博", "接口"),
    ("zhihu", "", "知乎", "接口"),
    ("xiaohongshu", "", "小红书", "接口"),
    ("kuaishou", "", "快手", "接口"),
)


def normalize_body_chars(raw: Any, default: int = DEFAULT_BODY_CHARS) -> int:
    """归一化正文截断长度：默认 1200，范围 200–8000。"""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    return max(200, min(MAX_BODY_CHARS, value))


def normalize_keep_seconds(raw: Any, default: int = DEFAULT_KEEP_SECONDS) -> int:
    """归一化抓包记录保留秒数：默认 900，最小 60，最大 86400。"""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    return max(60, min(86400, value))


def mask_url(url: Any) -> str:
    """对 URL 查询参数中的敏感字段打码，返回可展示 URL。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if not parts.query:
            return text
        masked = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in _SENSITIVE_QUERY_KEYS:
                masked.append((key, _MASK))
            else:
                masked.append((key, value))
        query = urlencode(masked, doseq=True, safe="*")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except Exception:  # noqa: BLE001 - 解析失败按原文返回
        return text


def build_addon_script(log_path: str | Path, body_chars: int = DEFAULT_BODY_CHARS) -> str:
    """生成 mitmdump addon 源码：把解密后的 HTTP 流写入 JSONL。

    只记录文本类响应（json/text/xml/javascript 等），二进制（图片/视频/zip）不落盘；
    正文按 ``body_chars`` 截断，避免日志与提示词膨胀。
    """
    chars = normalize_body_chars(body_chars)
    log_literal = json.dumps(str(log_path), ensure_ascii=False)
    return f'''"""手机流量抓包 addon：由 astrbot_phone_control 自动生成。"""

import json
import time

LOG_PATH = {log_literal}
BODY_CHARS = {chars}
TEXT_MARKERS = ("json", "text", "xml", "javascript", "x-www-form-urlencoded", "graphql")


def _text_body(message):
    try:
        if not message or not message.content:
            return ""
        ctype = str(message.headers.get("content-type", "")).lower()
        if ctype and not any(marker in ctype for marker in TEXT_MARKERS):
            return ""
        text = message.get_text(strict=False) or ""
        return text[:BODY_CHARS]
    except Exception:
        return ""


class PhoneTrafficAddon:
    def response(self, flow):
        try:
            response = flow.response
            record = {{
                "ts": time.time(),
                "method": str(flow.request.method or ""),
                "url": str(flow.request.pretty_url or ""),
                "status": int(getattr(response, "status_code", 0) or 0),
                "ctype": str(response.headers.get("content-type", "") or ""),
                "reqbody": _text_body(flow.request)[:800],
                "body": _text_body(response),
                "duration_ms": 0,
            }}
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\\n")
        except Exception:
            pass


addons = [PhoneTrafficAddon()]
'''


def parse_flow_line(line: str) -> dict[str, Any] | None:
    """解析一行抓包 JSONL；非法行返回 None。"""
    text = str(line or "").strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    url = str(record.get("url") or "").strip()
    if not url:
        return None
    try:
        status = int(record.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    try:
        ts = float(record.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return {
        "ts": ts,
        "method": str(record.get("method") or "").upper(),
        "url": url,
        "status": status,
        "ctype": str(record.get("ctype") or ""),
        "reqbody": str(record.get("reqbody") or ""),
        "body": str(record.get("body") or ""),
    }


def load_recent_flows(
    log_path: str | Path,
    *,
    limit: int = DEFAULT_LIMIT,
    since_seconds: int = 0,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """读取最近抓包记录（按时间正序返回最后 ``limit`` 条）。"""
    count = max(1, min(MAX_LIMIT, int(limit)))
    current = float(now) if now is not None else time.time()
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    flows: list[dict[str, Any]] = []
    for line in lines:
        record = parse_flow_line(line)
        if record is None:
            continue
        if since_seconds and current - record["ts"] > float(since_seconds):
            continue
        flows.append(record)
    return flows[-count:]


def prune_log(
    log_path: str | Path,
    *,
    max_bytes: int = FLOW_LOG_MAX_BYTES,
    keep_lines: int = FLOW_LOG_KEEP_LINES,
) -> int:
    """日志过大时裁剪为最近 ``keep_lines`` 行，返回删除行数。"""
    path = Path(log_path)
    try:
        if path.stat().st_size <= max_bytes:
            return 0
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    kept = lines[-max(1, int(keep_lines)) :]
    removed = len(lines) - len(kept)
    try:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        return 0
    return max(0, removed)


_SHARE_LINK_KEYS = frozenset(
    {
        "share_url",
        "shareurl",
        "share_link",
        "sharelink",
        "short_url",
        "shorturl",
        "jump_url",
        "jumpurl",
        "link_url",
        "linkurl",
        "url",
        "link",
        "web_url",
        "weburl",
    }
)
_SHARE_LINK_SKIP_SUFFIX = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".zip",
    ".mp4",
    ".m3u8",
    ".js",
    ".css",
    ".json",
)


def _looks_share_link(value: Any) -> bool:
    """判断字符串是否像可分享链接（http/https，且不是静态资源）。"""
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")):
        return False
    lowered = text.lower().split("?", 1)[0]
    if any(lowered.endswith(suffix) for suffix in _SHARE_LINK_SKIP_SUFFIX):
        return False
    return True


def extract_share_links(
    flow: dict[str, Any], *, limit: int = 5
) -> list[str]:
    """从接口响应 JSON 中提取可分享链接（分享接口通常返回 share_url/jump_url 等）。

    支持嵌套 JSON；除常见字段名外，也接受 URL 路径含 ``share`` 的链接和 App 网页链接；
    过滤图片/压缩包/脚本等静态资源，去重后最多返回 ``limit`` 条。
    """
    body = str(flow.get("body") or "").strip()
    if not body or not body.startswith(("{", "[")):
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) >= max(1, int(limit)):
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_lower = str(key).lower()
                if isinstance(value, str) and _looks_share_link(value):
                    if key_lower in _SHARE_LINK_KEYS or "share" in key_lower:
                        if value not in found:
                            found.append(value)
                            if len(found) >= max(1, int(limit)):
                                return
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data)
    return found


def infer_flow_hints(flow: dict[str, Any]) -> str:
    """根据域名 + 路径推断 App 与页面类型，返回简短说明。"""
    url = str(flow.get("url") or "")
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return ""
    host = (parts.netloc or "").lower()
    path = (parts.path or "").lower()
    for host_key, path_key, app, page in _FLOW_HINTS:
        if host_key and host_key not in host:
            continue
        if path_key and path_key not in path:
            continue
        return f"{app}·{page}"
    if host:
        return f"{host} 接口"
    return ""


def render_traffic(
    flows: Iterable[dict[str, Any]],
    *,
    window_seconds: int = 0,
    body_chars: int = DEFAULT_BODY_CHARS,
) -> str:
    """把抓包记录渲染成 LLM 可读文本（含页面推断、敏感打码与截断说明）。"""
    records = list(flows)
    chars = normalize_body_chars(body_chars)
    if not records:
        return (
            "最近没有抓到可识别的接口流量（可能：抓包未就绪、App 尚未发起请求、"
            "该 App 使用证书固定/加密导致解密失败）。可回退 phone_screen(mode=ui) "
            "文本或截图识别页面。"
        )
    lines = [f"最近抓包（{len(records)} 条" + (f"，近 {window_seconds} 秒" if window_seconds else "") + "）："]
    for flow in records:
        when = time.strftime("%H:%M:%S", time.localtime(float(flow.get("ts") or 0)))
        url = mask_url(flow.get("url"))
        ctype = str(flow.get("ctype") or "").split(";")[0].strip()
        hint = infer_flow_hints(flow)
        head = f"[{when}] {flow.get('method') or 'GET'} {url} → {flow.get('status')}"
        if ctype:
            head += f" {ctype}"
        if hint:
            head += f" | {hint}"
        lines.append(head)
        body = str(flow.get("body") or "").strip()
        if body:
            body = body.replace("\n", " ")
            if len(body) > chars:
                body = body[:chars] + f"…（截断，原 {len(body)} 字符）"
            lines.append(f"  响应：{body}")
        links = extract_share_links(flow)
        if links:
            lines.append("  可分享链接：" + " | ".join(links))
        req_body = str(flow.get("reqbody") or "").strip()
        if req_body:
            req_body = req_body.replace("\n", " ")
            if len(req_body) > chars // 2:
                req_body = req_body[: chars // 2] + f"…（截断，原 {len(req_body)} 字符）"
            lines.append(f"  请求体：{req_body}")
    lines.append(
        "说明：URL 敏感参数已打码、正文按长度截断；抓包用于识别页面/接口内容，"
        "图文、视频画面、布局等视觉信息仍用多模态截图（抓不到包的 App 用证书固定/加密时也回退截图）。"
        "发现有趣内容分享时优先用响应里的可分享链接（或找分享按钮复制链接），"
        "拿不到链接再截图兜底。"
    )
    return "\n".join(lines)
