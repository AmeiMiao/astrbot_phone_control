"""完整点播视频：已观测原文件、下载阈值与整段识别。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
import shutil
import time
from urllib.parse import urljoin, urlsplit

from .errors import PhoneControlError
from .media_context import video_context
from .media_video import MAX_VIDEO_BYTES


class FullVideoTooLarge(PhoneControlError):
    """完整文件超过用户阈值，应切换有序分段观看。"""


def observed_video_urls(service) -> set[str]:
    traffic = getattr(service, "traffic", None)
    log = getattr(traffic, "log_path", None)
    if not log:
        return set()
    from .traffic import load_recent_flows

    return {r["url"] for r in load_recent_flows(log, limit=200, since_seconds=300)
            if str(r.get("method", "GET")).upper() == "GET" and 200 <= r.get("status", 0) < 400
            and ("video" in str(r.get("ctype", "")).lower() or urlsplit(r["url"]).path.lower().endswith((".mp4", ".webm", ".mov", ".mkv")))}


def restricted_address(value) -> bool:
    address = ipaddress.ip_address(value)
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified


def validate_source_url(url: str, observed: set[str]) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise PhoneControlError("原视频地址需为手机已访问的 HTTP(S) 视频地址")
    if url not in observed:
        raise PhoneControlError("原视频地址未在当前手机最近视频流量中观测到")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address and restricted_address(address):
        raise PhoneControlError("原视频地址不允许回环、链路本地或组播目标")
    if parts.path.lower().endswith((".m3u8", ".mpd")):
        raise PhoneControlError("分片播放清单按流媒体采集处理")
    return url


async def download_video(url: str, path: Path, *, maximum: int, observed: set[str]) -> Path:
    """只取实际手机流量已观测的完整文件，按响应头和实际字节数双重限制。"""
    import aiohttp

    class Resolver(aiohttp.abc.AbstractResolver):
        def __init__(self):
            self.resolver = aiohttp.resolver.DefaultResolver()

        async def resolve(self, host, port=0, family=0):
            entries = await self.resolver.resolve(host, port, family)
            for entry in entries:
                if restricted_address(entry["host"]):
                    raise PhoneControlError("原视频域名解析到了受限目标")
            return entries

        async def close(self):
            await self.resolver.close()

    resolver = Resolver()
    partial = path.with_suffix(".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(resolver=resolver), trust_env=True) as session:
            for _ in range(4):
                if url not in observed:
                    from .traffic import mask_url
                    matches = [candidate for candidate in observed if mask_url(candidate) == url]
                    if len(matches) == 1:
                        url = matches[0]
                validate_source_url(url, observed)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status != 200 or response.headers.get("Content-Range"):
                        raise PhoneControlError(f"原视频未返回完整文件（HTTP {response.status}）")
                    length = response.content_length
                    if length is not None and length > maximum:
                        raise FullVideoTooLarge("原视频大小超过完整观看阈值，使用切片观看")
                    total = 0
                    with partial.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(65536):
                            total += len(chunk)
                            if total > maximum:
                                raise FullVideoTooLarge("原视频实际大小超过完整观看阈值，使用切片观看")
                            handle.write(chunk)
                    if not total:
                        raise PhoneControlError("原视频文件为空")
                    partial.replace(path)
                    return path
            raise PhoneControlError("原视频重定向次数超过上限")
    except aiohttp.ClientError as exc:
        raise PhoneControlError("原视频下载失败", detail=type(exc).__name__) from exc
    finally:
        partial.unlink(missing_ok=True)
        await resolver.close()


async def run_media_process(args: list[str], timeout=30) -> bytes:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise PhoneControlError("处理完整视频失败", detail=stderr.decode("utf-8", "replace")[-300:])
        return stdout
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def video_info(path: Path) -> tuple[float, dict]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PhoneControlError("完整视频观看需要 FFmpeg 与 ffprobe")
    data = json.loads(await run_media_process([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format", "-show_streams", "-of", "json", str(path)], 15))
    import math
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (ValueError, TypeError) as exc:
        raise PhoneControlError("原视频没有可确认的完整时长") from exc
    if not math.isfinite(duration) or duration <= 0 or not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
        raise PhoneControlError("原文件没有有限的完整视频时长，按流媒体处理")
    return duration, data


class FullVideoWatch:
    def __init__(self, manager):
        self.manager = manager
        self.service = manager.service

    async def watch(self, event, **kwargs):
        owner = self.manager.owner(event)
        task = self.manager.active.get(owner)
        if task and task.get("_reader") is not None:
            raise PhoneControlError("当前视频正在处理，请等待本次调用完成")
        try:
            return await self._watch(event, **kwargs)
        except BaseException:
            task = self.manager.active.get(owner)
            if task and task.get("_reader") is asyncio.current_task():
                await self.manager.finish_owner(owner, abnormal=True)
            raise
        finally:
            task = self.manager.active.get(owner)
            if task and task.get("_reader") is asyncio.current_task():
                task.pop("_reader", None)

    async def _watch(self, event, *, seconds=0, question="", mode="auto", stream_kind="auto", video_url="", duration_seconds=0, play_x=None, play_y=None):
        manager = self.manager
        owner = manager.owner(event)
        task = manager.active.get(owner)
        if task and (await self.service.actions.foreground())[0] != task["package"]:
            await manager.finish_owner(owner, abnormal=False)
            task = None
        if task and video_url and task.get("_source_identity") and task["_source_identity"] != video_url:
            await manager.finish_owner(owner, abnormal=False)
            task = None
        if stream_kind == "live":
            if task and task.get("_full_request"):
                await manager.finish_owner(owner, abnormal=False)
            result = await self.service._watch_segment(event, seconds=seconds, question=question, mode=mode)
            result["watch_mode"] = "segments"
            result["segment_reason"] = "直播保持分段观看"
            return result
        if task and task.get("_segmented_reason"):
            result = await manager.read(event, seconds=seconds)
            result.update(watch_mode="segments", segment_reason=task["_segmented_reason"])
            return result
        if task and task.get("_full_target") and not task.get("_full_result"):
            task["_reader"] = asyncio.current_task()
            return await self.poll_recording(event, task, seconds)
        if task and task.get("_full_result"):
            return {**task["_full_result"], "note": "本条完整视频已识别；切换内容先结束当前观看。"}
        if stream_kind != "video":
            raise PhoneControlError("完整视频开关已开启，请确认 stream_kind=video（点播）或 live（直播）")
        if not self.service.config.model.video_provider_id:
            raise PhoneControlError("完整视频观看需先选择视频识别模型")
        if manager.providers.provider(self.service.config.model.video_provider_id) is None:
            raise PhoneControlError("配置的视频识别模型不可用")
        if task:
            await manager.finish_owner(owner, abnormal=False)
        package, activity = await self.service.actions.foreground()
        task = await manager.begin(event, kind="video", content_id=f"{package}/{activity}", question=question, mode=mode, capture=False)
        task["_reader"] = asyncio.current_task()
        task["_full_request"] = True
        task["_source_identity"] = video_url
        task["_full_max_bytes"] = min(MAX_VIDEO_BYTES, int(self.service.config.media.full_video_max_mb * 1024 * 1024))
        maximum = min(MAX_VIDEO_BYTES, int(self.service.config.media.full_video_max_mb * 1024 * 1024))
        path = manager.root / task["id"] / "original-video.mp4"
        reason = "未提供可获取的原视频地址"
        try:
            if video_url:
                try:
                    await download_video(video_url, path, maximum=maximum, observed=observed_video_urls(self.service))
                    duration, info = await video_info(path)
                    expected = float(duration_seconds or 0)
                    if expected > 0 and abs(duration - expected) > max(1, expected * .03):
                        raise PhoneControlError("原文件时长与已确认的视频总时长不符，改为从头录制")
                    if duration > self.service.config.media.full_video_max_seconds:
                        raise FullVideoTooLarge("视频时长超过完整观看上限，使用切片观看")
                    task["_full_audio_available"] = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
                except FullVideoTooLarge as exc:
                    return await self.segment_fallback(event, task, str(exc), seconds, play_x, play_y)
                except (PhoneControlError, OSError, asyncio.TimeoutError) as exc:
                    reason = "原视频获取不可用：" + (str(exc) if isinstance(exc, PhoneControlError) else type(exc).__name__)
                else:
                    return await self.analyse(event, task, path, duration, source="original_file")
            return await self.record_fallback(event, task, reason, seconds=seconds, duration_seconds=duration_seconds, play_x=play_x, play_y=play_y)
        except BaseException:
            await manager.finish_owner(owner, abnormal=True)
            raise
        finally:
            path.unlink(missing_ok=True)

    async def segment_fallback(self, event, task, reason, seconds, play_x=None, play_y=None):
        task["_segmented_reason"] = reason
        await self.manager.start_capture(task, play_x=play_x, play_y=play_y)
        task.pop("_reader", None)
        result = await self.manager.read(event, seconds=seconds)
        result.update(watch_mode="segments", segment_reason=reason)
        return result

    async def record_fallback(self, event, task, reason, *, seconds=0, duration_seconds=0, play_x=None, play_y=None):
        import math
        duration = float(duration_seconds or 0)
        if not math.isfinite(duration) or duration < 3 or play_x is None or play_y is None:
            raise PhoneControlError(reason + "；请先暂停在视频起点，确认总时长 duration_seconds，并提供播放按钮 play_x/play_y")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (play_x, play_y)):
            raise PhoneControlError("播放按钮坐标需为非负整数")
        if duration > min(300, self.service.config.media.full_video_max_seconds):
            return await self.segment_fallback(event, task, "完整录制超过时长上限，使用切片观看", seconds, play_x, play_y)
        task["_full_target"] = duration
        task["_original_unavailable"] = reason
        await self.manager.start_capture(task, play_x=play_x, play_y=play_y)
        return await self.poll_recording(event, task, seconds)

    async def poll_recording(self, event, task, seconds=0):
        # 每次工具最多等待 20 秒，长视频通过连续调用保持 Agent 与设备租约。
        deadline = time.monotonic() + 20
        while not task.get("_capture_finished") and time.monotonic() < deadline:
            self.service.guard_device("full_video_record")
            if task["status"] != "active":
                raise PhoneControlError(task.get("error") or "完整视频采集已结束")
            await asyncio.sleep(.1)
        if not task.get("_capture_finished"):
            return dict(task_id=task["id"], status="recording_full", watch_mode="full_recording",
                        remaining_seconds=round(max(0, task["_full_target"] - (time.monotonic() - task["_start"])), 1),
                        next="整条视频尚未录完，请保持页面播放并再次调用 phone_watch_media；不要结束本轮或切换内容。")
        path = self.manager.root / task["id"] / "recorded-full.mp4"
        audio = path.with_suffix(".wav")
        try:
            stream = task.get("_audio")
            if stream is not None:
                await self.export_audio(stream, task.get("_audio_offset", 0), task["_full_target"], audio)
            from .media_video import encode_segment, VideoTooLarge
            try:
                await encode_segment(list(task["frames"]), 0, task["_full_target"], audio if stream else None, path,
                                     maximum_seconds=300, maximum_bytes=task["_full_max_bytes"])
            except VideoTooLarge:
                task["_segmented_reason"] = "完整录制文件超过大小阈值，按已采集缓存切片观看"
                task.pop("_reader", None)
                result = await self.manager.read(event, seconds=seconds)
                result.update(watch_mode="segments", segment_reason=task["_segmented_reason"])
                return result
            task["_full_audio_available"] = stream is not None
            return await self.analyse(event, task, path, task["_full_target"], source="recording")
        except BaseException:
            await self.manager.finish_owner(task["owner"], abnormal=True)
            raise
        finally:
            path.unlink(missing_ok=True)
            audio.unlink(missing_ok=True)

    @staticmethod
    async def export_audio(stream, start, duration, path):
        from .media import BYTES_PER_SECOND, SAMPLE_RATE, CHANNELS
        import wave
        offset = int(start * BYTES_PER_SECOND) // 4 * 4
        length = int(duration * BYTES_PER_SECOND) // 4 * 4
        if stream.size < offset + length:
            raise PhoneControlError("完整视频音轨尚未采集完毕")
        def export():
            with stream.path.open("rb") as source, wave.open(str(path), "wb") as target:
                source.seek(offset)
                target.setnchannels(CHANNELS)
                target.setsampwidth(2)
                target.setframerate(SAMPLE_RATE)
                remaining = length
                while remaining:
                    data = source.read(min(65536, remaining))
                    if not data:
                        raise PhoneControlError("完整音轨文件不完整")
                    target.writeframesraw(data)
                    remaining -= len(data)
        await asyncio.to_thread(export)


    async def analyse(self, event, task, path, duration, *, source):
        self.service.guard_device("full_video_analyse")
        if (await self.service.actions.foreground())[0] != task["package"]:
            raise PhoneControlError("完整视频识别前前台 App 已切换")
        if not self.service.config.media.audio_enabled or task["mode"] == "frames":
            visual = path.with_name("full-visual-only.mp4")
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise PhoneControlError("完整视频仅画面模式需要 FFmpeg")
            try:
                await run_media_process([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:v:0", "-c:v", "copy", "-an", str(visual)])
                task["_full_audio_available"] = False
                return await self.analyse_file(event, task, visual, duration, source=source)
            finally:
                visual.unlink(missing_ok=True)
        return await self.analyse_file(event, task, path, duration, source=source)

    async def analyse_file(self, event, task, path, duration, *, source):
        config = self.service.config
        prompt = f"这是完整点播视频（0–{duration:.3f} 秒），请从头到尾理解画面、事件和声音，仅依据实际素材回答。\n" + task["question"]
        prompt += video_context(task)
        has_audio = task.get("_full_audio_available", True)
        if not has_audio:
            prompt += "\n原视频没有音轨，不得编造声音。"
        if has_audio and config.model.video_audio_separate:
            asr_id = str(config.model.asr_provider_id or config.media.asr_provider_id).strip()
            provider = self.manager.providers.provider(asr_id) if asr_id else None
            if provider is None:
                raise PhoneControlError("视频音轨单独识别需配置可用语音识别模型")
            audio = path.with_suffix(".wav")
            try:
                ffmpeg = shutil.which("ffmpeg")
                if not ffmpeg:
                    raise PhoneControlError("提取完整音轨需要 FFmpeg")
                await run_media_process([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-t", str(duration), str(audio)])
                prompt += "\n【音轨单独识别】\n" + await self.transcribe_chunks(event, audio, provider, asr_id)
                prompt += "\n声音含义以转写为准，不编造未转写的音乐或语气。"
            finally:
                audio.unlink(missing_ok=True)
        response = await asyncio.wait_for(self.manager.providers.generate(chat_provider_id=task["video_provider_id"], prompt=prompt, video_path=path), config.media.vision_timeout_seconds)
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise PhoneControlError("完整视频识别模型未返回正文")
        self.service.record_internal_usage(getattr(response, "usage", None), source="internal:full_video", event=event, prompt_text=prompt, completion_text=text, image_count=0)
        task["cursor"] = duration
        task["summaries"].append(text)
        task["summary_ranges"].append([0, duration])
        result = dict(task_id=task["id"], watch_mode="full", full_video=True, source=source, video_bytes=path.stat().st_size, understood_until=duration, summary=text, next="完整视频已理解，结束观看后再切换内容")
        task["_full_result"] = result
        self.manager.write(task)
        return result

    async def transcribe_chunks(self, event, path, provider, provider_id):
        import wave
        chunks = []
        with wave.open(str(path), "rb") as source:
            rate = source.getframerate()
            index = 0
            while True:
                data = source.readframes(rate * 30)
                if not data:
                    break
                target = path.with_name(f"asr-part-{index}.wav")
                try:
                    with wave.open(str(target), "wb") as dest:
                        dest.setparams(source.getparams())
                        dest.writeframes(data)
                    text = await self.manager.transcribe(event, target, provider, provider_id)
                    chunks.append(f"{index * 30} 秒起：{text}")
                finally:
                    target.unlink(missing_ok=True)
                index += 1
        return "\n".join(chunks)
