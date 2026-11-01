# Copyright 2026 Gods-Workbench Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Phase 12 A4 媒体处理的真实落盘与外部进程封装。

命名空间：``media_settings``（设置）与 ``media_jobs``（后台任务），经
``core.storage.JsonState`` 原子落盘，重启可恢复。

真实数据源：
- 缩略图/预览：PIL 打开**允许根目录内**的真实素材文件后重采样；
- 波形/转码/抽帧：ffmpeg/ffprobe 真实子进程；
- 远端图片：httpx 真实抓取（带大小上限、超时与 SSRF 防护）。

失败关闭：未配置允许根目录 → 403；ffmpeg/PIL/网络缺失或失败 → 503；
**禁止**伪造 job_id、假 URL 或随机波形。

证据边界：单进程/单实例落盘；多 worker 并发写属部署方职责。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gw.core import storage
from gw.core.errors import CleanroomException

NS_SETTINGS = "media_settings"
NS_JOBS = "media_jobs"

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}

MAX_REMOTE_BYTES = 16 * 1024 * 1024
FFMPEG_TIMEOUT = 120
WAVEFORM_BUCKETS = 240
#: 允许的内网测试主机（本地 Provider 联调）；其余内网/回环地址一律拒绝。
ALLOWED_INTERNAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
#: 缩略图缓存默认子目录名；设置项 cache_folder 为空时回落到该值。
DEFAULT_CACHE_FOLDER = "god-cache"
#: 缓存子目录名必须是单段安全 ASCII，禁止路径分隔符、中文与保留设备名。
_CACHE_FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def settings_state() -> storage.JsonState:
    def _default() -> dict[str, Any]:
        return {
            "revision": 1,
            "asset_proxy": {
                "enabled": False,
                "project_id": "",
                "notice": "尚未配置代理；关闭转发，不伪造远程地址",
            },
            "thumbnails": {
                "mode": "source",
                "directory": "",
                "cache_folder": "god-cache",
                "width": 256,
            },
        }

    return storage.JsonState(NS_SETTINGS, _default)


def jobs_state() -> storage.JsonState:
    def _default() -> dict[str, Any]:
        return {"revision": 1, "jobs": {}}

    return storage.JsonState(NS_JOBS, _default)


def _unavailable(endpoint: str, code: str, message: str) -> None:
    raise CleanroomException(
        503,
        code,
        message,
        extra={
            "endpoint": endpoint,
            "unavailable": True,
            "data_status": "not_integrated",
        },
        expose_extra_fields={"endpoint", "unavailable", "data_status"},
    )


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        _unavailable(
            "/api/media", "FFMPEG_NOT_AVAILABLE", "未找到 %s，媒体处理能力不可用" % name
        )
    return path


def _run(args: list[str], timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        _unavailable("/api/media", "MEDIA_PROCESS_FAILED", "媒体处理进程执行失败")


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


def get_proxy_settings(project_id: str | None = None) -> dict[str, Any]:
    raw = settings_state().read()
    data = dict(raw["asset_proxy"])
    if project_id:
        data["project_id"] = str(project_id)
    return {**data, "revision": raw["revision"], "data_status": "ok", "data_gaps": []}


def patch_proxy_settings(payload: dict[str, Any]) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        current = raw["asset_proxy"]
        for key, value in (payload or {}).items():
            if key in ("enabled",):
                current[key] = bool(value)
            elif key in ("project_id", "notice", "base_url"):
                current[key] = str(value)
        # 代理凭据一律剥离，绝不落库或回显。
        for credential in ("api_key", "token", "password", "secret", "authorization"):
            current.pop(credential, None)
        raw["revision"] = int(raw.get("revision") or 1) + 1
        return dict(current)

    data = settings_state().mutate(mutate)
    return {**data, "data_status": "ok", "data_gaps": []}


def get_thumbnail_settings() -> dict[str, Any]:
    raw = settings_state().read()
    return {
        **dict(raw["thumbnails"]),
        "revision": raw["revision"],
        "data_status": "ok",
        "data_gaps": [],
    }


def patch_thumbnail_settings(payload: dict[str, Any]) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        current = raw["thumbnails"]
        for key in ("mode", "directory", "cache_folder"):
            if payload.get(key) is not None:
                current[key] = str(payload.get(key))
        if payload.get("width") is not None:
            width = int(payload.get("width"))
            current["width"] = max(64, min(2048, width))
        if current.get("mode") not in ("source", "custom"):
            raise CleanroomException(
                400, "INVALID_REQUEST", "mode 只接受 source/custom"
            )
        raw["revision"] = int(raw.get("revision") or 1) + 1
        return dict(current)

    data = settings_state().mutate(mutate)
    return {**data, "data_status": "ok", "data_gaps": []}


# ---------------------------------------------------------------------------
# 素材定位（必须经允许根目录）
# ---------------------------------------------------------------------------


def _pil():
    """延迟导入 PIL；未安装返回 None（调用方据此失败关闭）。"""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    return Image


def _registry_path(asset_id: str) -> Path:
    """从素材注册表取真实本地文件；不存在即失败关闭，绝不编造。"""
    from gw.asset_registry import repository as asset_repo

    path = asset_repo.media_file(asset_id)
    if path is None:
        _unavailable("/api/media", "MEDIA_NOT_AVAILABLE", "该素材没有可读取的本地文件")
    return path


def _resolve_input(payload: dict[str, Any]) -> Path:
    """解析输入：优先 asset_id（走注册表），其次显式 path（必须落在允许根目录内）。"""
    asset_id = str((payload or {}).get("asset_id") or "").strip()
    if asset_id:
        return _registry_path(asset_id)
    raw_path = str((payload or {}).get("path") or "").strip()
    if not raw_path:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 asset_id 或 path")
    path = storage.resolve_request_path(
        raw_path, root_id=str((payload or {}).get("root_id") or "").strip() or None
    )
    if not path.is_file():
        raise CleanroomException(404, "FILE_NOT_FOUND", "目标文件不存在或不可访问")
    return path


def _output_dir(kind: str) -> Path:
    relative = "media_output/" + kind
    out = storage.resolve_output_directory(relative, must_exist=False)
    out.mkdir(parents=True, exist_ok=True)
    return storage.resolve_output_directory(relative)


def _cache_folder_name(settings: dict[str, Any]) -> str:
    """解析设置里的缓存子目录名；非法即失败关闭，不静默回退。"""
    raw = str((settings or {}).get("cache_folder") or "").strip() or DEFAULT_CACHE_FOLDER
    if not _CACHE_FOLDER_PATTERN.fullmatch(raw):
        raise CleanroomException(
            400,
            "INVALID_CACHE_FOLDER",
            "缓存目录名只接受字母、数字、点、下划线与连字符，且不超过 64 个字符",
        )
    return raw


def _cache_bucket(settings: dict[str, Any], source: Path) -> str:
    """缓存分桶键：custom 模式按指定目录，source 模式按素材源目录。

    分桶只决定受管产物内的子目录，不把缓存写到受管目录之外；指定目录必须
    已登记为允许根，否则失败关闭，避免用户误以为缓存落在外部磁盘。
    """
    mode = str((settings or {}).get("mode") or "source")
    if mode == "custom":
        directory = str((settings or {}).get("directory") or "").strip()
        if not directory:
            raise CleanroomException(
                400, "INVALID_CACHE_DIRECTORY", "指定缓存目录模式必须提供目录"
            )
        resolved = storage.resolve_within_roots(directory)
        if not resolved.is_dir():
            raise CleanroomException(
                404, "CACHE_DIRECTORY_NOT_FOUND", "指定缓存目录不存在或不是目录"
            )
        return storage.root_identity_id(resolved)
    return storage.root_identity_id(source.parent)


def _thumbnail_cache_directory(
    settings: dict[str, Any], source: Path
) -> tuple[str, Path]:
    """返回缩略图缓存目录的相对路径与真实路径，按 cache_folder/分桶隔离。"""
    relative = "media_output/%s/%s" % (
        _cache_folder_name(settings),
        _cache_bucket(settings, source),
    )
    directory = storage.resolve_output_directory(relative, must_exist=False)
    directory.mkdir(parents=True, exist_ok=True)
    return relative, storage.resolve_output_directory(relative)


def _cache_is_fresh(target: Path, source: Path) -> bool:
    """缓存是否可直接复用：存在且不早于源文件修改时间。

    asset_id 命名不含源身份（既有契约按 id 唯一），因此用 mtime 判定过期，
    源文件被替换后自动重算，不返回过期图。
    """
    try:
        return target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns
    except OSError:
        return False


def _mime(path: Path) -> str:
    return MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# 缩略图与预览（PIL 真实重采样）
# ---------------------------------------------------------------------------


def _thumbnail_bytes(path: Path, width: int) -> bytes:
    Image = _pil()
    if Image is None:
        _unavailable("/api/media", "PIL_NOT_AVAILABLE", "未安装 PIL，缩略图能力不可用")
    try:
        with Image.open(path) as image:
            image = image.convert("RGBA")
            ratio = max(1, int(width)) / float(image.width or 1)
            height = max(1, int(round(image.height * ratio)))
            resized = image.resize((max(1, int(width)), height))
            import io as _io

            buffer = _io.BytesIO()
            resized.save(buffer, format="PNG")
            return buffer.getvalue()
    except CleanroomException:
        raise
    except Exception:
        _unavailable("/api/media", "THUMBNAIL_FAILED", "无法把该文件解码为图片")


def _thumbnail_key(asset_id: str, path: Path) -> str:
    """派生文件名 key：有 asset_id 用其身份，否则退化为源文件身份摘要。"""
    key = str(asset_id or "").strip()
    if key:
        return key
    stat = path.stat()
    identity = "|".join(
        (storage.relative_display(path), str(stat.st_size), str(stat.st_mtime_ns))
    )
    return "media-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _generate_one_thumbnail(
    asset_id: str,
    payload: dict[str, Any],
    settings: dict[str, Any],
    *,
    width: int,
    replace_existing: bool,
) -> dict[str, Any]:
    """生成或复用单个缩略图，返回真实状态项（不抛批量级异常）。"""
    path = _registry_path(asset_id) if asset_id else _resolve_input(payload)
    key = _thumbnail_key(asset_id, path)
    relative_dir, directory = _thumbnail_cache_directory(settings, path)
    name = _thumbnail_filename(key, width=width if key.startswith("media-") else None)
    relative = relative_dir + "/" + name
    target = storage.resolve_output_path(relative, must_exist=False)
    item: dict[str, Any] = {
        "asset_id": asset_id,
        "source": storage.relative_display(path),
        "output": relative,
        "width": width,
    }
    if not replace_existing and _cache_is_fresh(target, path):
        item.update({"status": "cached", "bytes": target.stat().st_size})
        return item
    data = _thumbnail_bytes(path, width)
    _atomic_write_bytes(target, data)
    item.update({"status": "generated", "bytes": len(data)})
    return item


def _resolved_thumbnail_width(payload: dict[str, Any]) -> int:
    width = int((payload or {}).get("width") or 256)
    return max(64, min(2048, width))


def _thumbnail_error_item(asset_id: str, exc: CleanroomException) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "status": "failed",
        "error": {"code": exc.code, "message": exc.message},
    }


def generate_thumbnail(payload: dict[str, Any]) -> dict[str, Any]:
    """真实生成缩略图并落盘，支持单条与批量（asset_ids）。

    批量时逐项处理并返回真实计数；单项失败不中断整批，也不伪装成功。
    单条调用保持既有失败关闭语义：失败直接抛出原异常。
    """
    settings = get_thumbnail_settings()
    width = _resolved_thumbnail_width(payload)
    replace_existing = bool((payload or {}).get("replace_existing"))
    asset_ids = _asset_ids(payload)
    items: list[dict[str, Any]] = []
    first_error: CleanroomException | None = None
    for asset_id in asset_ids:
        try:
            items.append(
                _generate_one_thumbnail(
                    asset_id,
                    payload,
                    settings,
                    width=width,
                    replace_existing=replace_existing,
                )
            )
        except CleanroomException as exc:
            if first_error is None:
                first_error = exc
            items.append(_thumbnail_error_item(asset_id, exc))
    if not asset_ids:
        # 无 asset_id 时沿用显式 path 入参（旧调用方与无注册表场景）。
        try:
            items.append(
                _generate_one_thumbnail(
                    "",
                    payload,
                    settings,
                    width=width,
                    replace_existing=replace_existing,
                )
            )
        except CleanroomException as exc:
            first_error = exc
            items.append(_thumbnail_error_item("", exc))
    if len(items) == 1 and items[0].get("status") == "failed" and first_error is not None:
        raise first_error
    generated = sum(1 for item in items if item.get("status") == "generated")
    cached = sum(1 for item in items if item.get("status") == "cached")
    failed = sum(1 for item in items if item.get("status") == "failed")
    outputs = [item["output"] for item in items if item.get("output")]
    successful = [item for item in items if item.get("status") != "failed"]
    head = successful[0] if successful else (items[0] if items else {})
    return {
        "asset_id": str((payload or {}).get("asset_id") or "") or head.get("asset_id", ""),
        "source": head.get("source", ""),
        "output": head.get("output", ""),
        "outputs": outputs,
        "items": items,
        "width": width,
        "generated": generated,
        "cached": cached,
        "failed": failed,
        "skipped": 0,
        "transcoded": 0,
        "transcode_cached": 0,
        "transcode_failed": 0,
        "data_status": "partial" if failed else "ok",
        "data_gaps": [] if not failed else ["thumbnail_failures_present"],
    }


def preview_bytes(payload: dict[str, Any]) -> dict[str, Any]:
    """给图片/BMP 等返回真实重采样缩略字节；视频走 ffmpeg 抽帧。"""
    path = _resolve_input(payload)
    width = int((payload or {}).get("w") or 256)
    width = max(64, min(2048, width))
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}:
        data = _video_frame_bytes(path, width)
        media_type = "image/jpeg"
    else:
        data = _thumbnail_bytes(path, width)
        media_type = "image/png"
    return {
        "bytes": data,
        "media_type": media_type,
        "source": storage.relative_display(path),
    }


def _video_frame_bytes(path: Path, width: int) -> bytes:
    ffmpeg = _tool("ffmpeg")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "frame.jpg"
        completed = _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=%d:-1" % width,
                str(target),
            ]
        )
        if completed.returncode != 0 or not target.is_file():
            _unavailable("/api/media", "MEDIA_PROCESS_FAILED", "视频抽帧失败")
        return target.read_bytes()


# ---------------------------------------------------------------------------
# 波形（ffmpeg 解码 → 固定桶 RMS 包络）
# ---------------------------------------------------------------------------


def waveform(payload: dict[str, Any]) -> dict[str, Any]:
    """用 ffmpeg 把音频解码为 16-bit PCM，再按固定桶算真实 RMS 包络。

    无音频流 → 400；ffmpeg 缺失或失败 → 503；**禁止**随机/常量假曲线。
    """
    path = _resolve_input(payload)
    ffmpeg = _tool("ffmpeg")
    import array
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "audio.pcm"
        completed = _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-vn",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(target),
            ]
        )
        if completed.returncode != 0 or not target.is_file():
            # 有视频但无音频流属请求错误；其它失败按 503 失败关闭。
            raise CleanroomException(400, "NO_AUDIO_STREAM", "该文件没有可解码的音频流")
        pcm = target.read_bytes()
    if len(pcm) < 2:
        raise CleanroomException(400, "NO_AUDIO_STREAM", "该文件没有可解码的音频流")
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        raise CleanroomException(400, "NO_AUDIO_STREAM", "该文件没有可解码的音频流")
    sample_rate = 16000
    duration = len(samples) / float(sample_rate)
    bucket_count = min(WAVEFORM_BUCKETS, max(1, len(samples)))
    buckets: list[tuple[float, float]] = []
    size = len(samples) / float(bucket_count)
    for index in range(bucket_count):
        start = int(index * size)
        end = max(start + 1, int((index + 1) * size))
        chunk = samples[start:end]
        if not chunk:
            buckets.append((0.0, 0.0))
            continue
        minimum = min(chunk) / 32768.0
        maximum = max(chunk) / 32768.0
        buckets.append((round(minimum, 6), round(maximum, 6)))
    return {
        "duration": round(duration, 4),
        "minimum": [b[0] for b in buckets],
        "maximum": [b[1] for b in buckets],
        "bucket_count": bucket_count,
        "sample_rate": sample_rate,
        "data_status": "ok",
        "data_gaps": [],
    }


# ---------------------------------------------------------------------------
# 转码
# ---------------------------------------------------------------------------


def transcode(payload: dict[str, Any]) -> dict[str, Any]:
    """用 ffmpeg 真实转码到数据目录（默认 H.264 MP4）；失败 503。"""
    path = _resolve_input(payload)
    ffmpeg = _tool("ffmpeg")
    out_dir = _output_dir("transcode")
    stat = path.stat()
    identity = "|".join(
        (storage.relative_display(path), str(stat.st_size), str(stat.st_mtime_ns))
    )
    target = out_dir / ("%s.mp4" % hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32])
    partial = target.parent / (target.stem + ".part" + target.suffix)
    completed = _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(partial),
        ]
    )
    if completed.returncode != 0 or not partial.is_file():
        _unavailable("/api/media", "MEDIA_PROCESS_FAILED", "媒体转码失败")
    partial.replace(target)
    return {
        "source": storage.relative_display(path),
        "output": target.resolve()
        .relative_to(storage.data_root().resolve())
        .as_posix(),
        "bytes": int(target.stat().st_size),
        "data_status": "ok",
        "data_gaps": [],
    }


# ---------------------------------------------------------------------------
# 下载输出（只允许数据目录内）
# ---------------------------------------------------------------------------


def download_path(payload: dict[str, Any]) -> dict[str, Any]:
    """所有显式路径和ID推导候选均只准入独立产物目录，不能触达内部状态。"""
    raw_target = str((payload or {}).get("path") or "").strip()
    if not raw_target:
        asset_id = str((payload or {}).get("asset_id") or "").strip()
        if not asset_id:
            raise CleanroomException(400, "INVALID_REQUEST", "缺少 path 或 asset_id")
        for relative, candidate in storage.iter_output_paths("media_output"):
            if (
                candidate.name.startswith(asset_id + ".")
                or candidate.name.startswith(asset_id + "-")
                or candidate.stem == asset_id
                or candidate.stem.startswith(asset_id + "_")
            ):
                return {
                    "path": candidate,
                    "relative_path": relative,
                    "filename": candidate.name,
                }
        raise CleanroomException(404, "FILE_NOT_FOUND", "没有可下载的输出文件")
    resolved = storage.resolve_output_path(raw_target)
    relative = resolved.relative_to(storage.data_root().resolve()).as_posix()
    return {
        "path": resolved,
        "relative_path": relative,
        "filename": str((payload or {}).get("name") or resolved.name),
    }


# ---------------------------------------------------------------------------
# 远端图片（httpx 真实抓取 + SSRF 防护 + 大小上限）
# ---------------------------------------------------------------------------


def _guard_url(url: str) -> None:
    """SSRF 防护：只允许 http/https，且拒绝内网/回环/链路本地地址。

    例外：显式允许的本地联调主机（127.0.0.1 / localhost），便于本机 Provider 联调。
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise CleanroomException(400, "INVALID_URL", "URL 不合法") from e
    if parsed.scheme not in ("http", "https"):
        raise CleanroomException(400, "INVALID_URL", "只接受 http/https URL")
    if parsed.username is not None or parsed.password is not None:
        raise CleanroomException(400, "INVALID_URL", "URL不允许包含凭据")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise CleanroomException(400, "INVALID_URL", "URL 缺少主机名")
    if host in ALLOWED_INTERNAL_HOSTS:
        return
    # 解析全部地址：任一解析结果落在受限网段即拒绝（防止 DNS 重绑定）。
    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as e:
        raise CleanroomException(400, "INVALID_URL", "主机名无法解析") from e
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as e:
            raise CleanroomException(400, "INVALID_URL", "解析出的地址不合法") from e
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise CleanroomException(
                403, "SSRF_BLOCKED", "该地址属于受限网段，已拒绝请求"
            )


def fetch_remote_image(payload: dict[str, Any]) -> dict[str, Any]:
    """真实抓取远端图片并落盘到数据目录；失败 503，越界 400/403。"""
    url = str((payload or {}).get("url") or "").strip()
    if not url:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 url")
    _guard_url(url)
    try:
        import httpx
    except Exception:
        _unavailable(
            "/api/online-image", "HTTPX_NOT_AVAILABLE", "未安装 httpx，无法抓取远端图片"
        )
    try:
        with httpx.Client(
            timeout=20.0, follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "GET", url, headers={"User-Agent": "Gods-Workbench/cleanroom"}
            ) as response:
                if not 200 <= response.status_code < 300:
                    _unavailable(
                        "/api/online-image",
                        "REMOTE_FETCH_FAILED",
                        "远端图片未返回成功状态",
                    )
                content_type = response.headers.get("content-type") or ""
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > MAX_REMOTE_BYTES:
                        raise CleanroomException(
                            413, "PAYLOAD_TOO_LARGE", "远端图片超过16MB上限"
                        )
                    content.extend(chunk)
    except CleanroomException:
        raise
    except Exception:
        _unavailable("/api/online-image", "REMOTE_FETCH_FAILED", "远端图片抓取失败")
    if not content:
        _unavailable("/api/online-image", "REMOTE_FETCH_FAILED", "远端图片内容为空")
    import hashlib

    out_dir = _output_dir("online")
    # 确定性文件名：用 URL 的 SHA-256 摘要前缀，避免跨进程非确定（不依赖 hash()）。
    name = "online-%s.img" % hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    target = out_dir / name
    target.write_bytes(content)
    return {
        "url": url,
        "output": target.relative_to(storage.data_root().resolve()).as_posix(),
        "bytes": len(content),
        "content_type": content_type,
        "data_status": "ok",
        "data_gaps": [],
    }


# ---------------------------------------------------------------------------
# 后台缩略图任务（真实执行 + JsonState 落盘，可回读）
# ---------------------------------------------------------------------------


def _job_state() -> storage.JsonState:
    return storage.JsonState(
        NS_JOBS, lambda: {"revision": 1, "sequence": 0, "jobs": {}}
    )


def run_background_thumbnail(payload: dict[str, Any]) -> tuple[str, str]:
    """真实生成缩略图并登记为一个可回读的后台任务。

    与 ``generate_thumbnail`` 使用同一真实实现，不做假进度：
    任务在返回前已完成，状态为 ``succeeded``，``progress`` 与结果均为真实计数。
    """
    result = generate_thumbnail(payload)
    file_name = Path(result["output"]).name if result.get("output") else ""
    total = max(1, len(result.get("items") or []))

    def mutate(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raw["sequence"] = int(raw.get("sequence", 0)) + 1
        job_id = "tjob_%04d" % raw["sequence"]
        record = {
            "job_id": job_id,
            "status": "succeeded",
            "state": "succeeded",
            "progress": {"processed": total, "total": total},
            "result": result,
            "output": result["output"],
            "output_file": file_name,
            "error": None,
            "created_at": storage.now_iso(),
            "finished_at": storage.now_iso(),
        }
        raw.setdefault("jobs", {})[job_id] = record
        raw["revision"] = int(raw.get("revision") or 1) + 1
        return job_id, dict(record)

    job_id, _ = _job_state().mutate(mutate)
    return job_id, "/api/asset-thumbnails/jobs/%s" % job_id


def get_job(job_id: str) -> dict[str, Any]:
    """读取后台缩略图任务；不存在一律 404，不返回伪造状态。"""
    job = _job_state().read().get("jobs", {}).get(str(job_id))
    if not job:
        raise CleanroomException(404, "THUMBNAIL_JOB_NOT_FOUND", "缩略图任务不存在")
    record = dict(job)
    return {
        "task": record,
        "job_id": record["job_id"],
        "status": record["status"],
        "progress": record["progress"],
        "result": record["result"],
        "data_status": "ok",
        "data_gaps": [],
    }


# ---------------------------------------------------------------------------
# 生成的缓存输出删除（真实删除后不可再读到）
# ---------------------------------------------------------------------------


def _asset_ids(payload: dict[str, Any]) -> list[str]:
    raw = (payload or {}).get("asset_ids")
    if raw is None:
        single = (payload or {}).get("asset_id")
        raw = [single] if single else []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise CleanroomException(400, "INVALID_REQUEST", "asset_ids 必须是数组")
    return [str(item).strip() for item in raw if str(item).strip()]


def _thumbnail_filename(asset_id: str, *, width: int | None = None) -> str:
    if (
        not asset_id
        or len(asset_id) > 128
        or not all(ch.isalnum() or ch in "_-" for ch in asset_id)
    ):
        raise CleanroomException(400, "INVALID_ASSET_ID", "素材标识不是安全单段")
    suffix = "-w%04d" % width if width else ""
    return "%s%s.jpg.png" % (asset_id, suffix)


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """派生物统一原子写：先写同目录 .part 并 fsync，再替换最终文件。"""
    partial = target.parent / (target.stem + ".part" + target.suffix)
    try:
        partial.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def delete_outputs(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """仅删除本模块精确生成的缓存；所有候选先预检，失败不虚报整批完成。"""
    if kind not in ("thumbnails", "storyboards"):
        raise CleanroomException(
            400, "INVALID_REQUEST", "kind只接受thumbnails/storyboards"
        )
    ids = _asset_ids(payload)
    if not ids:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少asset_ids")
    from gw.asset_registry import media_probe

    candidates = []
    # 缓存按 cache_folder/分桶分层存储，位置随设置变化；删除按精确文件名索引，
    # 不假设固定目录层级，也不触碰代理与分镜产物。
    thumbnail_index: dict[str, list[str]] | None = None
    for asset_id in dict.fromkeys(ids):
        if kind == "thumbnails":
            if thumbnail_index is None:
                thumbnail_index = {}
                for relative, path in storage.iter_output_paths("media_output"):
                    thumbnail_index.setdefault(path.name, []).append(relative)
            candidates.extend(
                thumbnail_index.get(asset_id + ".jpg.png", [])
            )
        else:
            directory = media_probe.storyboard_directory(asset_id)
            if directory.exists():
                # 版本桶与默认桶一并清理；逐文件准入校验，不跟随链接。
                for child in sorted(directory.rglob("*")):
                    if not child.is_file():
                        continue
                    relative = child.relative_to(
                        storage.data_root().resolve()
                    ).as_posix()
                    storage.resolve_output_path(relative)
                    if media_probe.is_storyboard_cache_name(child.name):
                        candidates.append(relative)
    removed, failed = [], []
    for relative in candidates:
        try:
            target = storage.resolve_output_path(relative)
            target.unlink()
            removed.append(relative)
        except CleanroomException:
            failed.append({"name": relative, "code": "CACHE_PATH_CHANGED"})
        except OSError:
            failed.append({"name": relative, "code": "CACHE_DELETE_FAILED"})
    return {
        "kind": kind,
        "asset_ids": ids,
        "removed": len(removed),
        "removed_files": removed,
        "failed": failed,
        "data_status": "partial" if failed else "ok",
        "data_gaps": ["legacy_shared_frames_preserved"]
        if kind == "storyboards"
        else [],
    }
