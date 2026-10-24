"""单篇内容的有序理解、素材所有权和独立兴趣决策。"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import shutil
import time
import uuid

from .errors import PhoneControlError


class ContentTasks:
    def __init__(self, service):
        self.service = service
        self.root = service.data_dir / "content_tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = {}
        self.lock = asyncio.Lock()
        from .media_provider import MediaProviders

        self.providers = MediaProviders(getattr(service, "app_context", None))
        # 新插件实例只恢复已完成任务；进行中记录的运行者已经失效。
        for path in self.root.glob("*/state.json"):
            try:
                state = json.loads(path.read_text())
                if not isinstance(state, dict):
                    raise ValueError("内容任务状态必须为对象")
            except (OSError, ValueError):
                if len(path.parent.name) == 32 and all(
                    c in "0123456789abcdef" for c in path.parent.name
                ):
                    shutil.rmtree(path.parent)
                continue
            if state.get("status") == "active":
                state["status"] = "abandoned"
                self.history(state)
                shutil.rmtree(path.parent)
        self.cleanup()

    @staticmethod
    def owner(event):
        return str(getattr(event, "unified_msg_origin", "") or "")

    def history(self, task):
        record = {
            k: task.get(k)
            for k in (
                "id",
                "kind",
                "content_id",
                "status",
                "cursor",
                "summaries",
                "decisions",
                "error",
            )
        }
        record["ended"] = time.time()
        with (self.root / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write(self, task):
        data = {k: v for k, v in task.items() if not k.startswith("_")}
        path = self.root / task["id"] / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False))
        temp.replace(path)

    def current(self, event, task_id=""):
        task = self.active.get(self.owner(event))
        if task is None or (task_id and task["id"] != task_id):
            raise PhoneControlError("本会话没有匹配的进行中内容任务")
        return task

    async def begin(self, event, *, kind, content_id, question="", mode="auto"):
        if kind not in {"post", "video"} or not str(content_id or "").strip():
            raise PhoneControlError("需指定 post/video 和当前内容唯一标识 content_id")
        if mode in {"audio", "both"} and not self.service.config.media.audio_enabled:
            raise PhoneControlError("声音采集已关闭，不能仅听音频")
        if kind == "video" and not self.service.config.media.enabled:
            raise PhoneControlError("流媒体内容识别已关闭")
        async with self.lock:
            for owner in list(self.active):
                if owner != self.owner(event):
                    raise PhoneControlError(
                        "其它会话有进行中的内容任务，请先结束或停止手机"
                    )
                await self.finish_owner(owner, abnormal=False)
            package, activity = await self.service.actions.foreground()
            if not package:
                raise PhoneControlError("无法识别内容所属前台 App")
            task = dict(
                id=uuid.uuid4().hex,
                owner=self.owner(event),
                kind=kind,
                content_id=str(content_id or "")[:500],
                package=package,
                activity=activity,
                question=str(question)[:300],
                mode=mode,
                created=time.time(),
                status="active",
                cursor=0.0,
                decisions=[],
                summaries=[],
                frames=[],
                heartbeat=time.time(),
                video_provider_id=str(self.service.config.model.video_provider_id or "") if kind == "video" and mode != "audio" else "",
            )
            self.active[task["owner"]] = task
            # 内容归属整次 Agent run；收尾由 finish、on_agent_done 和服务关闭执行。
            self.write(task)
            try:
                if kind == "video":
                    await self.service.backends.ensure()
                    if self.service.config.media.audio_enabled and mode != "frames":
                        if self.service.media is None:
                            raise PhoneControlError("设备音频采集客户端未初始化")
                        stream = self.service.media.stream(
                            self.root / task["id"] / "source.pcm",
                            source=self.service.config.media.audio_source,
                        )
                        task["_audio"] = stream
                        await stream.start()
                        task["_start"] = stream.started
                    else:
                        task["_start"] = time.monotonic()
                self.arm_capture(task)
                task["_sampler"] = asyncio.create_task(
                    self.sample(task), name=f"{PLUGIN_NAME}_content_sampler"
                )
            except BaseException:
                await self.finish_owner(task["owner"], abnormal=True)
                raise
            return task

    def arm_capture(self, task):
        traffic = getattr(self.service, "traffic", None)
        if traffic is not None:
            control = Path(traffic.root) / "content_capture.json"
            control.parent.mkdir(parents=True, exist_ok=True)
            control.write_text(
                json.dumps(
                    {
                        "task": task["id"],
                        "directory": str(self.root / task["id"]),
                        "expires": time.time() + 30,
                    }
                )
            )

    async def sample(self, task):
        try:
            while task["status"] == "active":
                self.service.guard_device("content_sample")
                package, _ = await self.service.actions.foreground()
                if package != task["package"]:
                    raise PhoneControlError("内容采集中前台 App 已切换")
                task["heartbeat"] = time.time()
                self.write(task)
                self.arm_capture(task)
                elapsed = time.monotonic() - task.get("_start", time.monotonic())
                if task["kind"] == "video" and task["mode"] != "audio":
                    video = bool(task.get("video_provider_id"))
                    options = dict(fast=True, quiet=True, record_metric=False, persist=False) if video else {}
                    data, path, meta = await self.service.capture(
                        expected_package=task["package"], **options
                    )
                    if video:
                        from .media_video import compact_frame, MAX_BUFFER_BYTES, MAX_CAPTURE_SECONDS
                        data = await asyncio.to_thread(compact_frame, data)
                        task["_buffer_bytes"] = task.get("_buffer_bytes", 0) + len(data)
                        stream = task.get("_audio")
                        if (task["_buffer_bytes"] + (stream.size if stream else 0) > MAX_BUFFER_BYTES
                                or time.monotonic() - task["_start"] > MAX_CAPTURE_SECONDS):
                            raise PhoneControlError("视频采集达到 15 分钟或 128 MiB 缓存上限，请结束后开始新内容")
                    elapsed = time.monotonic() - task["_start"]
                    dest = self.root / task["id"] / f"frame-{elapsed:.3f}.jpg"
                    dest.write_bytes(data)
                    task["frames"].append([elapsed, str(dest)])
                    stream = task.get("_audio")
                    if stream is not None and stream.task.done():
                        await stream.task
                        raise PhoneControlError("内容音频流结束")
                if task["kind"] == "video":
                    from .media_video import VIDEO_INTERVAL
                    interval = VIDEO_INTERVAL if task.get("video_provider_id") else self.service.config.media.frame_interval_seconds
                    elapsed = time.monotonic() - task["_start"]
                    next_at = task["_start"] + (int(elapsed / interval) + 1) * interval
                    await asyncio.sleep(max(0.01, next_at - time.monotonic()))
                else:
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task["error"] = str(exc)
            await self.finish_owner(task["owner"], abnormal=True)

    def observe(self, event, text):
        if text.startswith(("error:", "已中止")):
            return
        task = self.active.get(self.owner(event))
        if task and any(d["decision"] == "read_comments" for d in task["decisions"]):
            task["_comment_observation"] = text[:12000]

    async def decide(self, event, decision, reason, task_id="", target=""):
        task = self.current(event, task_id)
        allowed = {
            "continue",
            "read_comments",
            "reply_comment",
            "like",
            "favorite",
            "skip",
        }
        if decision not in allowed or not reason.strip():
            raise PhoneControlError("兴趣决策需有效 decision 和独立 reason")
        if decision != "skip" and not task["summaries"]:
            raise PhoneControlError("请先理解正文或试看内容，再判断续看或互动兴趣")
        if decision == "reply_comment":
            if (
                not target.strip()
                or target.strip() not in task.get("_comment_observation", "")
                or not any(x["decision"] == "read_comments" for x in task["decisions"])
            ):
                raise PhoneControlError(
                    "先独立决定看评论并阅读具体评论，再填写 target 判断是否回复"
                )
        task["decisions"].append(
            {
                "decision": decision,
                "reason": reason[:500],
                "target": target[:500],
                "at": time.time(),
            }
        )
        self.write(task)
        if decision == "skip":
            await self.finish_owner(task["owner"], abnormal=False)
        return {
            "decision": decision,
            "permission_granted": False,
            "note": "兴趣判断不执行点赞、收藏或发布；回复另申请互动名额"
            + ("；break_rule 已开启，可如实记录心情、好奇心或单纯想看作为续看原因"
               if self.service.config.auto.break_rule else ""),
        }

    async def read(self, event, task_id="", seconds=0):
        task = self.current(event, task_id)
        if task.get("_reader") is not None:
            raise PhoneControlError("当前内容正在分析，请等待本段完成")
        # 流程参数错误可由下一次调用修正，检查阶段不占用 reader 或清理素材。
        if task["kind"] == "video":
            if task["cursor"] and not self.service.config.auto.break_rule and not any(
                i > task.get("_continuation_index", -1) and d["decision"] == "continue"
                for i, d in enumerate(task["decisions"])
            ):
                raise PhoneControlError("续看前请独立决定 continue 并给出原因")
            config = self.service.config.media
            try:
                duration = float(seconds or (config.preview_seconds if not task["cursor"] else config.seconds))
            except (TypeError, ValueError):
                raise PhoneControlError("片段时长需为有效秒数") from None
            if not 3 <= duration <= config.max_seconds:
                raise PhoneControlError(f"片段时长需 3–{config.max_seconds} 秒")
        task["_reader"] = asyncio.current_task()
        try:
            return await self._read(event, task_id, seconds)
        except BaseException:
            await self.finish_owner(self.owner(event), abnormal=True)
            raise
        finally:
            task.pop("_reader", None)

    async def _read(self, event, task_id="", seconds=0):
        task = self.current(event, task_id)
        async with self.lock:
            if (await self.service.actions.foreground())[0] != task["package"]:
                await self.finish_owner(task["owner"], abnormal=True)
                raise PhoneControlError("内容前台已改变，任务素材已清理")
            if task["kind"] == "post":
                text, images = self.post_material(task)
                if not text:
                    from .ui import dump_ui

                    snapshot = await dump_ui(self.service.adb)
                    text = "页面文字（抓包正文未可靠关联，可能不完整）：\n" + "\n".join(
                        n.text for n in snapshot.nodes if n.text
                    )
                if not images:
                    data, path, meta = await self.service.capture(
                        expected_package=task["package"]
                    )
                    dest = self.root / task["id"] / "post-screen.png"
                    dest.write_bytes(data)
                    images = [dest]
                result = await self.understand(event, task, text, images, None)
                task["cursor"] += 1
            else:
                available = [
                    i
                    for i, d in enumerate(task["decisions"])
                    if d["decision"] == "continue"
                    and i > task.get("_continuation_index", -1)
                ]
                if task["cursor"] and not available and not self.service.config.auto.break_rule:
                    raise PhoneControlError("续看前请独立决定 continue 并给出原因")
                config = self.service.config.media
                duration = float(
                    seconds
                    or (
                        config.preview_seconds if not task["cursor"] else config.seconds
                    )
                )
                maximum = min(config.max_seconds, 60) if task.get("video_provider_id") else config.max_seconds
                duration = min(duration, maximum)
                if not 3 <= duration <= maximum:
                    raise PhoneControlError(f"片段时长需 3–{config.max_seconds} 秒")
                start = task["cursor"]
                end = start + duration
                deadline = time.monotonic() + duration + 45
                while time.monotonic() - task["_start"] < end or (
                    task["mode"] != "audio"
                    and not any(
                        t >= end - config.frame_interval_seconds
                        for t, p in task["frames"]
                    )
                ):
                    if task["status"] != "active":
                        raise PhoneControlError(task.get("error", "内容任务已结束"))
                    if time.monotonic() > deadline:
                        raise PhoneControlError("关键帧采集超时")
                    await asyncio.sleep(0.1)
                images = [Path(p) for t, p in task["frames"] if start <= t < end]
                audio = None
                if task.get("_audio") is not None:
                    audio = await task["_audio"].segment(
                        start,
                        end,
                        self.root / task["id"] / f"audio-{start:g}-{end:g}.wav",
                    )
                if task.get("video_provider_id"):
                    result = await self.understand_video(event, task, start, end, audio)
                else:
                    result = await self.understand(
                        event, task,
                        f"视频时间 {start:.1f}–{end:.1f} 秒；请只描述已提供内容。",
                        images, audio,
                    )
                task["cursor"] = end
                if available:
                    task["_continuation_index"] = available[-1]
            if task["status"] != "active":
                raise PhoneControlError(task.get("error", "内容任务已结束"))
            task["summaries"].append(result)
            self.write(task)
            return {
                "task_id": task["id"],
                "understood_until": task["cursor"],
                "summary": result,
                "available_images": len(self.post_material(task)[1]) if task["kind"] == "post" else 0,
                "auto_save_images": self.service.config.image.auto_save_images,
                "image_storage_location": "both" if self.service.config.image.save_images_both else self.service.config.image.image_storage_location,
                "captured_until": self.captured_until(task),
                "note": "分析期间持续采集；续看从 understood_until 继续，包含分析期间的素材。",
                "next": ("可直接续看或自由浏览；需要精读时使用内容决策，结束调用 finish" if self.service.config.auto.break_rule else "独立判断 continue/read_comments/like/favorite/skip，结束调用 finish；看评论不等于回复"),
            }

    async def understand(self, event, task, text, images, audio):
        config = self.service.config.media
        provider_id, note = await self.service._resolve_media_vision_provider_id(
            event, self.service.config
        )
        if not provider_id:
            raise PhoneControlError(note)
        prompt = (
            "理解当前帖子或视频，仅根据素材解释内容；不执行互动。\n"
            + task["question"]
            + "\n"
            + text
        )
        if audio is None:
            prompt += "\n本次未提供音频，不得推测或编造声音内容。"
        if audio is not None:
            prompt += "\n音频已做峰值归一化，不据此判断设备实际响度。"
        audio_note = ""
        configured_asr = str(self.service.config.model.asr_provider_id or config.asr_provider_id).strip()
        if audio is not None and configured_asr:
            selected_asr = self.providers.provider(configured_asr)
            if selected_asr is None:
                raise PhoneControlError(f"未找到语音识别模型「{configured_asr}」")
            transcript = await self.transcribe(event, audio, selected_asr, configured_asr)
            prompt += "\n【语音转写；音乐与语气信息可能不完整】" + transcript
            audio_note = f"【语音识别模型】{configured_asr}；已使用语音转写。"
            audio = None
        elif audio is not None:
            resolver = getattr(self.service, "_resolve_media_stt_provider", None)
            stt, stt_note = await resolver(event, self.service.config) if callable(resolver) else (None, "")
            if stt is not None:
                transcript = await asyncio.wait_for(stt.get_text(str(audio)), config.asr_timeout_seconds)
                prompt += "\n【语音转写；音乐与语气信息可能不完整】" + str(transcript)
                audio_note = "【音频输入】已使用语音转写。"
                audio = None
        result = []
        limit = max(1, config.max_frames)
        for offset in range(0, max(1, len(images)), limit):
            batch_prompt = prompt + (
                "\n之前批次的已理解内容："
                + "\n".join(result)
                + "\n本批次只有新增画面，不要编造新的音频内容。"
                if result
                else ""
            )
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": batch_prompt,
                "image_urls": [str(p) for p in images[offset : offset + limit]],
            }
            native = audio is not None and offset == 0
            provider_getter = getattr(
                self.service.app_context, "get_provider_by_id", None
            )
            selected = (
                provider_getter(provider_id) if callable(provider_getter) else None
            )
            from .media_provider import audio_transport

            native_supported = audio_transport(selected) != "transcript"
            if native and not native_supported:
                stt, stt_note = await self.service._resolve_media_stt_provider(
                    event, self.service.config
                )
                if stt is not None:
                    transcript = await asyncio.wait_for(
                        stt.get_text(str(audio)), config.asr_timeout_seconds
                    )
                    kwargs["prompt"] += "\n【音频转写降级，音乐/语气信息不完整】" + str(
                        transcript
                    )
                    audio_note = "【音频输入】原适配器不传音频，已使用语音转写。"
                else:
                    kwargs["prompt"] += (
                        "\n未提供声音信息，不得根据画面推测语音、音乐或语气。"
                    )
                    audio_note = f"【音频不可用，仅画面】模型适配器不传原音频，且转写不可用：{stt_note}"
                native = False
            if native:
                kwargs["audio_urls"] = [str(audio)]
            try:
                response = await asyncio.wait_for(
                    self.providers.generate(**kwargs),
                    config.vision_timeout_seconds,
                )
            except Exception as exc:
                msg = str(exc).lower()
                unsupported = "audio" in msg and any(
                    x in msg
                    for x in [
                        "unsupported",
                        "not support",
                        "not allowed",
                        "unexpected keyword",
                        "invalid type",
                    ]
                )
                if not native or not unsupported:
                    raise
                provider, note = await self.service._resolve_media_stt_provider(
                    event, self.service.config
                )
                if provider is None:
                    raise PhoneControlError(
                        f"原音频不受支持且转写不可用：{note}"
                    ) from exc
                transcript = await asyncio.wait_for(
                    provider.get_text(str(audio)), config.asr_timeout_seconds
                )
                kwargs.pop("audio_urls")
                kwargs["prompt"] += "\n【音频转写降级，音乐/语气信息不完整】" + str(
                    transcript
                )
                response = await asyncio.wait_for(
                    self.providers.generate(**kwargs),
                    config.vision_timeout_seconds,
                )
            value = str(getattr(response, "completion_text", "") or "").strip()
            if not value:
                raise PhoneControlError("内容理解模型未返回正文")
            self.service.record_internal_usage(
                getattr(response, "usage", None),
                source="internal:content",
                event=event,
                prompt_text=kwargs["prompt"],
                completion_text=value,
                image_count=len(kwargs["image_urls"]),
            )
            result.append((audio_note + "\n" if audio_note else "") + value)
        return "\n".join(result)

    async def understand_video(self, event, task, start, end, audio):
        from .media_video import encode_segment

        provider_id = task["video_provider_id"]
        if self.providers.provider(provider_id) is None:
            raise PhoneControlError(f"未找到视频识别模型「{provider_id}」")
        prompt = f"这是手机屏幕按 2 FPS 采样的视频片段，时间 {start:.1f}–{end:.1f} 秒。只描述本段实际画面、动作与文字。\n" + task["question"]
        if audio is None:
            prompt += "\n本段未提供原音频；MP4 的音轨为生成的静音占位，不得推测声音。"
        else:
            prompt += "\n片段包含设备原音轨，由你同时理解画面与声音；仅描述实际听到的内容。音频已做峰值归一化，不据此判断设备实际响度。"
        path = self.root / task["id"] / f"video-{start:g}-{end:g}.mp4"
        try:
            await encode_segment(list(task["frames"]), start, end, audio, path)
            response = await asyncio.wait_for(
                self.providers.generate(chat_provider_id=provider_id, prompt=prompt, video_path=path),
                self.service.config.media.vision_timeout_seconds,
            )
            value = str(getattr(response, "completion_text", "") or "").strip()
            if not value:
                raise PhoneControlError("视频识别模型未返回正文")
            self.service.record_internal_usage(
                getattr(response, "usage", None), source="internal:media_video", event=event,
                prompt_text=prompt, completion_text=value, image_count=0,
            )
            return f"【分段视频模型】{provider_id}；{start:g}–{end:g} 秒\n" + value
        finally:
            path.unlink(missing_ok=True)

    async def transcribe(self, event, audio, provider, provider_id):
        """显式语音模型优先处理音频，画面请求只携带转写文字。"""
        config = self.service.config.media
        get_text = getattr(provider, "get_text", None)
        if callable(get_text):
            value = await asyncio.wait_for(get_text(str(audio)), config.asr_timeout_seconds)
        else:
            prompt = "请将音频中的人声准确转写为原文，不补充画面、剧情或未听到的内容；无人声时说明无人声。"
            response = await asyncio.wait_for(
                self.providers.generate(chat_provider_id=provider_id, prompt=prompt, audio_urls=[str(audio)]),
                config.asr_timeout_seconds,
            )
            value = getattr(response, "completion_text", "")
            self.service.record_internal_usage(
                getattr(response, "usage", None), source="internal:media_asr", event=event,
                prompt_text=prompt, completion_text=str(value or ""), image_count=0,
            )
        if not str(value or "").strip():
            raise PhoneControlError(f"语音识别模型「{provider_id}」未返回转写文字")
        return str(value).strip()

    def post_material(self, task):
        from .post_material import load_post_material

        return load_post_material(self.root / task["id"], task["content_id"])

    async def finish_owner(self, owner, *, abnormal):
        task = self.active.pop(owner, None)
        if task is None:
            return
        task["status"] = "failed" if abnormal else "completed"
        reader = task.get("_reader")
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        sampler = task.get("_sampler")
        if sampler is not None and sampler is not asyncio.current_task():
            sampler.cancel()
            with suppress(asyncio.CancelledError):
                await sampler
        if task.get("_audio") is not None:
            await task["_audio"].stop()
        traffic = getattr(self.service, "traffic", None)
        if traffic is not None:
            control = Path(traffic.root) / "content_capture.json"
            if control.exists():
                state = json.loads(control.read_text())
                if state.get("task") == task["id"]:
                    control.unlink()
        self.write(task)
        self.history(task)
        if abnormal:
            shutil.rmtree(self.root / task["id"])
        else:
            self.cleanup()

    async def close(self, abnormal=True):
        for owner in list(self.active):
            await self.finish_owner(owner, abnormal=abnormal)
        await self.providers.close()

    def cleanup(self):
        config = self.service.config.perception
        if not config.screenshot_auto_clean:
            return
        cutoff = time.time() - config.screenshot_retention_minutes * 60
        active = {t["id"] for t in self.active.values()}
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.name in active:
                continue
            for path in directory.iterdir():
                if (
                    path.is_file()
                    and path.name != "state.json"
                    and path.stat().st_mtime < cutoff
                ):
                    path.unlink()

    @staticmethod
    def captured_until(task):
        audio = task.get("_audio")
        from .media import pcm_duration

        times = []
        if audio is not None:
            times.append(pcm_duration(audio.size))
        if task["mode"] != "audio" and task["kind"] == "video":
            times.append(task["frames"][-1][0] if task["frames"] else 0.0)
        return round(min(times), 3) if times else 0.0

    def status(self):
        return [
            dict(
                **{k: t[k] for k in ("id", "kind", "content_id", "cursor", "status")},
                captured_until=self.captured_until(t),
                frame_count=len(t["frames"]),
                analyzing=t.get("_reader") is not None,
            )
            for t in self.active.values()
        ]
