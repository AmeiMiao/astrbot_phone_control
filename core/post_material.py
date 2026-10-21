"""按内容身份关联抓包正文与原图；不把同屏推荐内容混入正文。"""

import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ID_KEYS = {
    "id",
    "content_id",
    "post_id",
    "link_id",
    "item_id",
    "aweme_id",
    "bvid",
    "aid",
}
SKIP_KEYS = {
    "comments",
    "replies",
    "recommendations",
    "recommend",
    "related",
    "avatar",
    "user",
    "author",
}


def content_object(value, identity):
    if isinstance(value, dict):
        if any(str(value.get(k, "")) == identity for k in ID_KEYS):
            return value
        for key, child in value.items():
            if key.lower() not in SKIP_KEYS:
                match = content_object(child, identity)
                if match is not None:
                    return match
    if isinstance(value, list):
        for child in value:
            match = content_object(child, identity)
            if match is not None:
                return match
    return None


def extract(value, texts, urls, image=False):
    if isinstance(value, dict):
        for key, child in value.items():
            lower = key.lower()
            if lower in SKIP_KEYS:
                continue
            extract(
                child,
                texts,
                urls,
                image
                or lower
                in {
                    "image",
                    "images",
                    "image_list",
                    "pic",
                    "pics",
                    "pictures",
                    "image_urls",
                    "cover",
                },
            )
            if lower in {
                "title",
                "text",
                "content",
                "description",
                "desc",
                "message",
            } and isinstance(child, str):
                texts.append(child)
    elif isinstance(value, list):
        for child in value:
            extract(child, texts, urls, image)
    elif image and isinstance(value, str) and value.startswith(("https://", "http://")):
        urls.add(value)


def load_post_material(directory, identity):
    directory = Path(directory)
    records = []
    for path in directory.glob("resource-*.json"):
        record = json.loads(path.read_text())
        records.append(record)
    texts = []
    urls = set()
    for record in records:
        if not record.get("text_path"):
            continue
        path = (directory / record["text_path"]).resolve()
        if path.parent != directory.resolve():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        obj = content_object(payload, identity)
        if obj is None:
            request = urlsplit(record.get("url", ""))
            parts = request.path.strip("/").split("/")
            values = [v for vs in parse_qs(request.query).values() for v in vs]
            if identity in parts + values or record.get("url") == identity:
                obj = payload
        if obj is not None:
            extract(obj, texts, urls)
    images = []
    for record in records:
        if record.get("url") in urls and record.get("image_path"):
            path = (directory / record["image_path"]).resolve()
            if path.parent == directory.resolve() and path.is_file():
                images.append(path)
    return "\n".join(dict.fromkeys(texts)), list(dict.fromkeys(images))
