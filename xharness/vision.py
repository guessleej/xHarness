"""Images in a request.

A provider marked `vision = true` receives the images themselves (OpenAI
image_url parts, built when the request is sent). For one that cannot see,
`[vision] provider = "<name>"` names a model that can: each image is first
described by it, and the description travels with the request as text --
so a text model can still work from a screenshot, and the description is in
the log. With neither, the model is told plainly that it cannot see the
images instead of guessing at them.

Images are stored under .xharness/images in the working directory, named by
their hash, and the session log records the paths, not the pixels.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Callable

from .llm import OpenAIAdapter

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
DESCRIBE_PROMPT = (
    "Describe this image for someone who cannot see it, in the language and script of the request that "
    "follows it (Traditional Chinese for Traditional Chinese). Transcribe every piece of visible text exactly, "
    "including numbers, error messages, table cells and code. Then describe layout, charts or anything else "
    "relevant. Do not guess at what is not visible.\n\nThe request: "
)


def save_images(items: list[dict[str, Any]], cwd: str) -> list[str]:
    """Decode uploaded images (base64), store them, return their paths. Raises ValueError on bad input."""
    if len(items) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images per message")
    directory = os.path.join(cwd, ".xharness", "images")
    paths: list[str] = []
    for item in items:
        kind = str(item.get("type") or "").lower()
        extension = IMAGE_TYPES.get(kind)
        if not extension:
            raise ValueError(f"unsupported image type: {kind or '(none)'}; use PNG, JPEG, WebP or GIF")
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("image data is not valid base64") from error
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"each image must be under {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        os.makedirs(directory, exist_ok=True)
        ignore = os.path.join(cwd, ".xharness", ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as handle:
                handle.write("*\n")
        path = os.path.join(directory, f"{hashlib.sha256(data).hexdigest()[:20]}.{extension}")
        if not os.path.exists(path):
            with open(path, "wb") as handle:
                handle.write(data)
        paths.append(path)
    return paths


def data_uri(path: str) -> str | None:
    extension = os.path.splitext(path)[1].lstrip(".").lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(extension)
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None
    if not mime or len(data) > MAX_IMAGE_BYTES:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def describer_from_config(config: Any) -> tuple[str, Callable[[str, str], str]] | None:
    """(model name, describe(path, request)) for [vision] provider, or None."""
    name = str((getattr(config, "vision", None) or {}).get("provider") or "")
    entry = (getattr(config, "providers", {}) or {}).get(name)
    if not name or not entry or not entry.get("model"):
        return None
    adapter = OpenAIAdapter(**{**entry, "vision": True})

    def describe(path: str, request: str) -> str:
        message = {"role": "user", "content": DESCRIBE_PROMPT + request[:500], "images": [path]}
        return (adapter.stream([message], []).content or "").strip()

    return adapter.model, describe
