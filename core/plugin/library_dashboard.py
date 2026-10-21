"""Dashboard 图片库与学习档案管理；访问权限由 AstrBot 面板统一认证。"""

import base64

from ..errors import PhoneControlError
from ..saved_images import image_library
from .compat import error_response, json_response, plugin_main_module


class LibraryDashboardMixin:
    def _library_dashboard_routes(self, base):
        return (
            (
                f"{base}/learning/delete",
                self.dashboard_learning_delete,
                ["POST"],
                "删除手机学习记录",
            ),
            (f"{base}/images", self.dashboard_images, ["GET"], "保存的图片"),
            (
                f"{base}/images/preview",
                self.dashboard_image_preview,
                ["GET"],
                "图片预览",
            ),
            (
                f"{base}/images/update",
                self.dashboard_image_update,
                ["POST"],
                "编辑图片语义",
            ),
            (
                f"{base}/images/delete",
                self.dashboard_image_delete,
                ["POST"],
                "删除图片",
            ),
        )

    async def _library_request(self):
        data = await plugin_main_module().request.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not data["id"]
        ):
            raise PhoneControlError("请提供有效的记录编号")
        return data

    async def dashboard_learning_delete(self):
        try:
            data = await self._library_request()
            self.service.learning.delete(data["id"])
            return json_response({"deleted": data["id"]})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("删除学习记录失败，请检查存储", status_code=500)

    async def dashboard_images(self):
        try:
            query = str(
                plugin_main_module().request.query.get("query", "") or ""
            ).strip()[:100]
            library = image_library(self.service)
            records = list(library.load().values())
            needle = query.lower()
            matched = [
                r
                for r in records
                if not needle
                or needle
                in str(
                    [
                        r.get(k)
                        for k in ("id", "description", "reason", "tags", "sources")
                    ]
                ).lower()
            ]
            matched.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
            raw_offset = plugin_main_module().request.query.get("offset", 0, type=int)
            offset = max(0, int(raw_offset or 0))
            return json_response(
                {
                    "entries": matched[offset : offset + 30],
                    "total": len(matched),
                    "offset": offset,
                    "query": query,
                }
            )
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("读取图片库失败，请检查存储", status_code=500)

    async def dashboard_image_preview(self):
        try:
            image_id = str(plugin_main_module().request.query.get("id", "") or "")
            data, suffix = await image_library(self.service).preview(image_id)
            mime = "image/" + ("jpeg" if suffix == ".jpg" else suffix[1:])
            return json_response(
                {"url": f"data:{mime};base64," + base64.b64encode(data).decode()}
            )
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("读取图片失败，请检查存储", status_code=500)

    async def dashboard_image_update(self):
        try:
            data = await self._library_request()
            record = await image_library(self.service).update_semantics(
                data["id"],
                description=data.get("description"),
                reason=data.get("reason", ""),
                tags=data.get("tags", []),
                kind=data.get("kind", "image"),
            )
            return json_response(record)
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("保存图片语义失败，请检查存储", status_code=500)

    async def dashboard_image_delete(self):
        try:
            data = await self._library_request()
            await image_library(self.service).delete(data["id"])
            return json_response({"deleted": data["id"]})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("删除图片失败，请检查存储", status_code=500)
