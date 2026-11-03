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
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Iterable, Literal
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
    ".jxl": "image/jxl",
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
#: 视频后缀；预览走 ffmpeg 抽帧而不是 PIL。
_VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}
)
#: 预览缓存命中统计（单进程只读探针，供规模回归与排障读取）。
_PREVIEW_CACHE_LOCK = threading.Lock()
_PREVIEW_CACHE_STATS = {"hit": 0, "miss": 0, "degraded": 0}


def preview_cache_stats() -> dict[str, int]:
    """返回预览缓存命中/未命中/降级计数快照（不参与业务判定）。"""
    with _PREVIEW_CACHE_LOCK:
        return dict(_PREVIEW_CACHE_STATS)


def reset_preview_cache_stats() -> None:
    """清零预览缓存统计；仅供测试与排障探针使用。"""
    with _PREVIEW_CACHE_LOCK:
        for key in _PREVIEW_CACHE_STATS:
            _PREVIEW_CACHE_STATS[key] = 0


def _count_preview_cache(kind: str) -> None:
    with _PREVIEW_CACHE_LOCK:
        _PREVIEW_CACHE_STATS[kind] = _PREVIEW_CACHE_STATS.get(kind, 0) + 1


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
                "format": "jxl",
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
    thumbnails = dict(raw.get("thumbnails") or {})
    if thumbnails.get("format") not in ("jxl", "webp"):
        thumbnails["format"] = "jxl"
    return {
        **thumbnails,
        "revision": raw["revision"],
        "data_status": "ok",
        "data_gaps": [],
    }


def patch_thumbnail_settings(
    payload: dict[str, Any], *, require_expected_version: bool = False
) -> dict[str, Any]:
    payload = payload or {}
    expected_version = payload.get("expected_version")
    if require_expected_version and expected_version is None:
        raise CleanroomException(
            400, "INVALID_REQUEST", "更新缩略图设置必须携带 expected_version"
        )

    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        revision = int(raw.get("revision") or 1)
        if expected_version is not None:
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 1
            ):
                raise CleanroomException(
                    400, "INVALID_REQUEST", "expected_version 必须是正整数"
                )
            if expected_version != revision:
                raise CleanroomException(
                    409,
                    "VERSION_CONFLICT",
                    "缩略图设置已变化，请重新读取后重试",
                )
        current = raw["thumbnails"]
        current.setdefault("format", "jxl")
        for key in ("mode", "directory", "cache_folder"):
            if payload.get(key) is not None:
                current[key] = str(payload.get(key))
        if payload.get("format") is not None:
            image_format = payload.get("format")
            if image_format not in ("jxl", "webp"):
                raise CleanroomException(
                    400, "INVALID_REQUEST", 'format 只接受 "jxl" 或 "webp"'
                )
            current["format"] = image_format
        if payload.get("width") is not None:
            width = int(payload.get("width"))
            current["width"] = max(64, min(2048, width))
        if current.get("mode") not in ("source", "custom"):
            raise CleanroomException(
                400, "INVALID_REQUEST", "mode 只接受 source/custom"
            )
        raw["revision"] = revision + 1
        return {**dict(current), "revision": raw["revision"]}

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
    """解析输入：优先 asset_id（走注册表），其次显式 path（必须落在允许根目录内）。

    内部调用方可传 ``_resolved_path``（PATH 对象，HTTP JSON 无法构造）跳过注册表
    回查：全库预热每项都要出图，重复 load 整份 asset_registry.json 会成为主要
    串行开销。该快路径仍要求文件真实存在，语义与注册表解析一致。
    """
    prepared = (payload or {}).get("_resolved_path")
    if isinstance(prepared, Path) and prepared.is_file():
        return prepared
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


def _cache_directory_not_allowed(exc: CleanroomException) -> CleanroomException:
    """把通用路径准入错误翻译成缓存场景专用码（QIN-515 AC3）。

    不回显调用方原始路径；前端用自己手上的目录文案说清「写哪里失败、为什么」。
    """
    if exc.code == "LOCAL_FILE_ACCESS_NOT_ADMITTED":
        message = "尚未配置允许根目录，缓存无法写入所选目录；请管理员先在 设置 › 允许根 登记该目录"
    else:
        message = "所选缓存目录不在允许根目录内，缓存无法写入；请管理员把该目录加入允许根后重试"
    translated = CleanroomException(403, "CACHE_DIRECTORY_NOT_ALLOWED", message)
    translated.__cause__ = exc
    return translated


def _custom_cache_directory(settings: dict[str, Any], folder: str) -> tuple[str, Path]:
    """指定目录模式的真实落盘位置：``<所选目录>/<cache_folder>``（必须在允许根内）。"""
    directory = str((settings or {}).get("directory") or "").strip()
    if not directory:
        raise CleanroomException(
            400, "INVALID_CACHE_DIRECTORY", "指定缓存目录模式必须提供目录"
        )
    try:
        root = storage.resolve_within_roots(directory)
    except CleanroomException as exc:
        raise _cache_directory_not_allowed(exc) from exc
    if not root.is_dir():
        raise CleanroomException(
            404, "CACHE_DIRECTORY_NOT_FOUND", "指定缓存目录不存在或不是目录"
        )
    try:
        # cache_folder 已通过单段 ASCII 校验，拼接后仍是根内固定一层子目录。
        target = storage.resolve_cache_write_directory(str(root / folder))
    except CleanroomException as exc:
        if exc.code in ("PATH_OUTSIDE_ALLOWED_ROOTS", "LOCAL_FILE_ACCESS_NOT_ADMITTED"):
            raise _cache_directory_not_allowed(exc) from exc
        raise
    return storage.relative_display(target), target


# 缓存目录解析 + 建目录必须串行：Windows 上并发 mkdir 与 resolve 会撞出
# OSError（目录正在创建时 lstat/resolve 失败），预热线程池一并发就会丢项。
_CACHE_DIRECTORY_LOCK = threading.RLock()


def resolve_cache_directory(
    settings: dict[str, Any], source: Path
) -> tuple[str, Path]:
    """缓存目录唯一解析入口（生成 / 预览 / 删除共用，QIN-515 单一口径）。

    - ``mode=source``：受管数据目录 ``media_output/<cache_folder>/<源目录分桶>``；
    - ``mode=custom``：用户所选允许根内 ``<所选目录>/<cache_folder>``，真实写入。

    返回 ``(脱敏展示路径, 真实目录)``；越界一律失败关闭，磁盘零写入。
    解析与建目录在进程内串行，避免并发首次访问同一分桶时的目录创建竞态。
    """
    with _CACHE_DIRECTORY_LOCK:
        return _resolve_cache_directory_locked(settings, source)


def _resolve_cache_directory_locked(
    settings: dict[str, Any], source: Path
) -> tuple[str, Path]:
    folder = _cache_folder_name(settings)
    mode = str((settings or {}).get("mode") or "source")
    if mode == "custom":
        return _custom_cache_directory(settings, folder)
    relative = "%s/%s" % (cache_root_relative(), storage.root_identity_id(source.parent))
    directory = storage.resolve_output_directory(relative, must_exist=False)
    directory.mkdir(parents=True, exist_ok=True)
    return relative, storage.resolve_output_directory(relative)


def cache_root_relative() -> str:
    """受管缓存根相对路径（``media_output/<cache_folder>``）。

    缩略图、预览与视频分镜共用同一口径：换缓存目录名时全部产物一起换根，
    不允许各模块自行推导缓存位置。
    """
    return "media_output/" + _cache_folder_name(get_thumbnail_settings())


# ---------------------------------------------------------------------------
# 缓存目录切换：旧缓存迁移 / 删除（QIN-515 切换确认弹窗）
# ---------------------------------------------------------------------------

CACHE_SWITCH_ACTIONS = ("migrate", "delete", "keep")


def cache_root_path(settings: dict[str, Any]) -> Path:
    """缓存根目录（不按素材源分桶），用于整棵缓存树的迁移与清理。

    - ``mode=source`` → 受管 ``media_output/<cache_folder>``；
    - ``mode=custom`` → ``<所选目录>/<cache_folder>``（必须仍在允许根内）。
    """
    folder = _cache_folder_name(settings)
    if str((settings or {}).get("mode") or "source") == "custom":
        return _custom_cache_directory(settings, folder)[1]
    return storage.resolve_output_directory(
        "media_output/" + folder, must_exist=False
    )


def is_cache_artifact_name(name: str) -> bool:
    """缓存产物文件名判定：迁移与清理只碰这些，绝不动素材、代理与转码产物。"""
    from gw.asset_registry import media_probe

    if media_probe.is_storyboard_cache_name(name):
        return True
    # 扩展名随 QIN-513 的 format 设置变化：png(.jpg.png) / jxl / webp，视频预览为 .frame.jpg。
    return name.endswith((".jpg.png", ".jxl", ".webp", ".frame.jpg"))


#: 预览缓存分类（QIN-525：删除缓存弹窗按此勾选；``all`` 为并集，不落盘为类别）。
PREVIEW_CACHE_CATEGORIES = ("image", "video", "storyboard", "other")


def cache_artifact_category(name: str) -> str:
    """把缓存产物文件名归到删除弹窗的四个类别；未知产物归 ``other``。"""
    from gw.asset_registry import media_probe

    if media_probe.is_storyboard_cache_name(name):
        return "storyboard"
    if name.endswith(".frame.jpg"):
        return "video"
    if name.endswith((".jpg.png", ".jxl", ".webp")):
        return "image"
    return "other"


def _cache_artifact_key(name: str) -> str:
    """从缓存文件名还原素材标识，用于「已缓存素材数」去重。

    预览缓存为 ``<key>-w0256-<identity>.<ext>``，预生成缩略图为
    ``<asset_id>-w0256.<ext>``；两者都以 ``-w####`` 分界，取前缀即可。
    """
    match = re.match(r"^(?P<key>.+?)-w\d{4}", name)
    if match:
        return match.group("key")
    return name.split(".", 1)[0]


def _iter_cache_artifacts(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        child
        for child in root.rglob("*")
        if child.is_file() and is_cache_artifact_name(child.name)
    )


def _prune_empty_dirs(root: Path) -> None:
    """自底向上清理空目录；非空或权限不足一律跳过，不误删内容。"""
    if not root.is_dir():
        return
    for child in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            child.rmdir()
        except OSError:
            continue
    try:
        root.rmdir()
    except OSError:
        return


def _move_cache_artifacts(old_root: Path, new_root: Path) -> tuple[int, int]:
    """把旧缓存树按相对路径搬到新根；目标已存在则保留目标，返回 (迁移, 跳过)。"""
    moved = skipped = 0
    for path in _iter_cache_artifacts(old_root):
        target = new_root / path.relative_to(old_root)
        if target.exists():
            skipped += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved += 1
        except OSError:
            skipped += 1
    return moved, skipped


def _delete_cache_artifacts(old_root: Path) -> int:
    removed = 0
    for path in _iter_cache_artifacts(old_root):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _cache_roots_overlap(old_root: Path, new_root: Path) -> bool:
    try:
        old = old_root.resolve()
        new = new_root.resolve()
    except OSError:
        return False
    return old in new.parents or new in old.parents


def switch_thumbnail_cache(payload: dict[str, Any]) -> dict[str, Any]:
    """切换缩略图缓存目录，并按 ``cache_action`` 处理旧缓存。

    ``migrate`` 迁移、``delete`` 删除、``keep`` 保留；新目录越界或不存在时
    失败关闭且不改动设置（磁盘零写入）。旧目录已不可准入时只切换不清理，
    用 ``cache_retained`` 告知调用方旧缓存仍在原处。
    """
    action = str(payload.get("cache_action") or "keep")
    if action not in CACHE_SWITCH_ACTIONS:
        raise CleanroomException(
            400,
            "INVALID_CACHE_ACTION",
            "cache_action 只接受 migrate / delete / keep",
        )
    current = get_thumbnail_settings()
    target = dict(current)
    for key in ("mode", "directory", "cache_folder"):
        if payload.get(key) is not None:
            target[key] = str(payload.get(key))
    if str(target.get("mode")) not in ("source", "custom"):
        raise CleanroomException(400, "INVALID_REQUEST", "mode 只接受 source/custom")

    # 先解析新根：越界/不存在在这里失败关闭，设置与磁盘都不会被改动。
    new_root = cache_root_path(target)
    try:
        old_root: Path | None = cache_root_path(current)
    except CleanroomException:
        # 旧目录已移出允许根：不越界清理，只切换并如实回报。
        old_root = None

    moved = deleted = skipped = 0
    retained = False
    if action != "keep" and old_root is not None and old_root != new_root:
        if _cache_roots_overlap(old_root, new_root):
            raise CleanroomException(
                400,
                "CACHE_DIRECTORY_OVERLAP",
                "新旧缓存目录互相包含，无法迁移或清理；请选择互不包含的目录",
            )
        if action == "migrate":
            moved, skipped = _move_cache_artifacts(old_root, new_root)
        else:
            deleted = _delete_cache_artifacts(old_root)
        _prune_empty_dirs(old_root)
    elif action != "keep" and old_root is None:
        retained = True

    data = patch_thumbnail_settings(
        {
            "mode": target.get("mode"),
            "directory": target.get("directory") or "",
            "cache_folder": target.get("cache_folder"),
        }
    )
    return {
        **data,
        "cache_action": action,
        "cache_moved": moved,
        "cache_deleted": deleted,
        "cache_skipped": skipped,
        "cache_retained": retained,
    }


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


ThumbnailFormat = Literal["jxl", "webp"]
MAX_THUMBNAIL_BYTES = 15 * 1024


def _decode_jxl_source(path: Path) -> bytes:
    ffmpeg = _tool("ffmpeg")
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _unavailable("/api/media", "JXL_DECODE_FAILED", "JXL 素材解码失败")
    if result.returncode != 0 or not result.stdout:
        _unavailable("/api/media", "JXL_DECODE_FAILED", "JXL 素材解码失败")
    return result.stdout


def _encode_jxl_thumbnail(png_bytes: bytes) -> bytes:
    ffmpeg = _tool("ffmpeg")
    result_bytes = b""
    for distance in (2.0, 3.0, 4.0, 5.0, 7.0, 10.0, 13.0, 15.0):
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "-i",
                    "pipe:0",
                    "-frames:v",
                    "1",
                    "-c:v",
                    "libjxl",
                    "-distance",
                    str(distance),
                    "-effort",
                    "4",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                ],
                input=png_bytes,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            _unavailable(
                "/api/media", "JXL_ENCODING_FAILED", "JXL 缩略图编码失败"
            )
        if result.returncode != 0 or not result.stdout:
            _unavailable("/api/media", "JXL_ENCODING_FAILED", "JXL 缩略图编码失败")
        result_bytes = result.stdout
        if len(result_bytes) <= MAX_THUMBNAIL_BYTES:
            return result_bytes
    return result_bytes


#: QIN-525：缩略图渲染分派。位图走 PIL 重采样，视频抽帧，音频画真实包络，
#: 字体渲染样张，文档 / Office / 3D / 未知后缀统一绘制类型占位图。
_AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"})
_FONT_SUFFIXES = frozenset({".ttf", ".otf", ".woff", ".woff2"})
_DOCUMENT_SUFFIXES = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".md", ".json", ".csv", ".rtf", ".epub", ".srt", ".ass",
    }
)
_MODEL_SUFFIXES = frozenset(
    {".obj", ".fbx", ".glb", ".gltf", ".stl", ".blend", ".usd", ".usdz"}
)
_RENDER_KIND_LABELS = {
    "audio": "音频",
    "font": "字体",
    "model": "3D",
    "document": "文档",
    "other": "文件",
}
_PLACEHOLDER_BG = (20, 16, 12)
_PLACEHOLDER_FG = (232, 226, 214)
_PLACEHOLDER_ACCENT = (201, 162, 39)
_PLACEHOLDER_MUTED = (138, 130, 118)


def _preview_render_kind(path: Path) -> str:
    """判定缩略图渲染路径；未知后缀仍按位图尝试，失败时由调用方降级占位图。"""
    suffix = path.suffix.lower()
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    if suffix in _AUDIO_SUFFIXES:
        return "audio"
    if suffix in _FONT_SUFFIXES:
        return "font"
    if suffix in _MODEL_SUFFIXES:
        return "model"
    if suffix in _DOCUMENT_SUFFIXES:
        return "document"
    return "image"


def _vendor_font(size: int):
    """加载仓库白名单中文字体；缺失或加载失败返回 None，由调用方回落默认字体。"""
    try:
        from PIL import ImageFont  # type: ignore
    except Exception:
        return None

    root = Path(__file__).resolve().parents[2]
    for name in ("SourceHanSansCN-Medium.otf", "SourceHanSansCN-Normal.otf"):
        candidate = root / "web" / "vendor" / "fonts" / name
        if not candidate.is_file():
            continue
        try:
            return ImageFont.truetype(str(candidate), size)
        except Exception:
            continue
    return None


def _image_font(size: int):
    """优先白名单中文字体，缺失时用 Pillow 默认字体，保证占位图永远能画出来。"""
    from PIL import ImageFont  # type: ignore

    font = _vendor_font(size)
    if font is not None:
        return font
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _encode_derived_image(image, image_format: str) -> bytes:
    """把派生预览图编码为目标缓存格式，沿用位图缩略图的体积上限策略。"""
    import io as _io

    if image_format == "webp":
        buffer = _io.BytesIO()
        for quality in (72, 60, 48, 36, 28, 24):
            buffer = _io.BytesIO()
            image.save(buffer, format="WEBP", quality=quality, method=6)
            if buffer.tell() <= MAX_THUMBNAIL_BYTES or quality == 24:
                break
        return buffer.getvalue()
    buffer = _io.BytesIO()
    image.save(buffer, format="PNG")
    if image_format == "jxl":
        return _encode_jxl_thumbnail(buffer.getvalue())
    return buffer.getvalue()


def _draw_centered_text(draw, text: str, font, fill, center_x: float, top: float) -> float:
    """按中心线绘制一行文本，返回该行底部坐标；不依赖字体的固定行高。"""
    try:
        left, top_box, right, bottom_box = draw.textbbox((0, 0), text, font=font)
    except Exception:
        return top
    draw.text(
        (center_x - (right - left) / 2 - left, top - top_box),
        text,
        font=font,
        fill=fill,
    )
    return top + (bottom_box - top_box)


def _placeholder_thumbnail_bytes(
    path: Path, width: int, image_format: str, render_kind: str
) -> bytes:
    """绘制类型占位图：扩展名徽标 + 文件名 + 类型标签，离线也能辨认素材。"""
    Image = _pil()
    if Image is None:
        _unavailable("/api/media", "PIL_NOT_AVAILABLE", "未安装 PIL，缩略图能力不可用")
    from PIL import ImageDraw  # type: ignore

    side = max(64, min(2048, int(width)))
    image = Image.new("RGB", (side, side), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(image)
    inset = max(3, side // 28)
    draw.rectangle(
        (inset, inset, side - inset, side - inset),
        outline=_PLACEHOLDER_ACCENT,
        width=max(1, side // 160),
    )

    extension = path.suffix.lower().lstrip(".").upper()
    badge = extension if extension and len(extension) <= 6 else _RENDER_KIND_LABELS.get(
        render_kind, "文件"
    )
    badge_font = _image_font(max(16, int(side * 0.24)))
    label_font = _image_font(max(9, side // 22))
    caption_font = _image_font(max(8, side // 26))

    center_x = side / 2
    badge_bottom = _draw_centered_text(
        draw, badge, badge_font, _PLACEHOLDER_ACCENT, center_x, side * 0.32
    )
    kind_label = _RENDER_KIND_LABELS.get(render_kind, "文件")
    label_bottom = _draw_centered_text(
        draw, kind_label, label_font, _PLACEHOLDER_FG, center_x, badge_bottom + side * 0.04
    )

    name = path.name or path.stem
    if len(name) > 24:
        name = name[:23] + "…"
    _draw_centered_text(
        draw, name, caption_font, _PLACEHOLDER_MUTED, center_x, label_bottom + side * 0.05
    )
    return _encode_derived_image(image.convert("RGBA"), image_format)


def _font_sample_thumbnail_bytes(path: Path, width: int, image_format: str) -> bytes:
    """渲染字体样张：用被预览字体本身绘制字形，加载失败时降级占位图。"""
    Image = _pil()
    if Image is None:
        _unavailable("/api/media", "PIL_NOT_AVAILABLE", "未安装 PIL，缩略图能力不可用")
    from PIL import ImageDraw, ImageFont  # type: ignore

    side = max(64, min(2048, int(width)))
    try:
        sample_font = ImageFont.truetype(str(path), int(side * 0.3))
    except Exception:
        return _placeholder_thumbnail_bytes(path, width, image_format, "font")

    image = Image.new("RGB", (side, side), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(image)
    inset = max(3, side // 28)
    draw.rectangle(
        (inset, inset, side - inset, side - inset),
        outline=_PLACEHOLDER_ACCENT,
        width=max(1, side // 160),
    )
    caption_font = _image_font(max(8, side // 26))
    center_x = side / 2
    sample_bottom = _draw_centered_text(
        draw, "Aa", sample_font, _PLACEHOLDER_FG, center_x, side * 0.2
    )
    sample_bottom = _draw_centered_text(
        draw, "永字八法", sample_font, _PLACEHOLDER_FG, center_x, sample_bottom + side * 0.02
    )
    name = path.stem
    if len(name) > 22:
        name = name[:21] + "…"
    _draw_centered_text(
        draw, name, caption_font, _PLACEHOLDER_MUTED, center_x, sample_bottom + side * 0.05
    )
    return _encode_derived_image(image.convert("RGBA"), image_format)


def _audio_waveform_thumbnail_bytes(path: Path, width: int, image_format: str) -> bytes:
    """把真实 PCM 包络画成波形缩略图；解码失败时由调用方降级占位图。"""
    Image = _pil()
    if Image is None:
        _unavailable("/api/media", "PIL_NOT_AVAILABLE", "未安装 PIL，缩略图能力不可用")
    from PIL import ImageDraw  # type: ignore

    buckets, _duration = _pcm_envelope(path)
    side = max(64, min(2048, int(width)))
    image = Image.new("RGB", (side, side), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(image)
    center_y = side / 2
    draw.line((0, center_y, side, center_y), fill=_PLACEHOLDER_MUTED, width=1)

    count = len(buckets)
    if count:
        step = side / float(count)
        half = side * 0.36
        for index, (minimum, maximum) in enumerate(buckets):
            x = int(index * step)
            draw.line(
                (x, center_y - maximum * half, x, center_y - minimum * half),
                fill=_PLACEHOLDER_ACCENT,
                width=max(1, int(step)),
            )

    caption_font = _image_font(max(8, side // 26))
    name = path.name
    if len(name) > 24:
        name = name[:23] + "…"
    _draw_centered_text(
        draw, name, caption_font, _PLACEHOLDER_MUTED, side / 2, side * 0.88
    )
    return _encode_derived_image(image.convert("RGBA"), image_format)


def _derived_thumbnail_bytes(
    path: Path, width: int, image_format: str, render_kind: str
) -> bytes:
    """非位图素材的派生预览：音频波形 / 字体样张 / 其余类型占位图。"""
    if render_kind == "audio":
        try:
            return _audio_waveform_thumbnail_bytes(path, width, image_format)
        except CleanroomException:
            pass
        except Exception:
            pass
    if render_kind == "font":
        return _font_sample_thumbnail_bytes(path, width, image_format)
    return _placeholder_thumbnail_bytes(path, width, image_format, render_kind)


def _thumbnail_bytes(
    path: Path, width: int, image_format: Literal["png", "jxl", "webp"] = "png"
) -> bytes:
    render_kind = _preview_render_kind(path)
    if render_kind != "image":
        return _derived_thumbnail_bytes(path, width, image_format, render_kind)
    Image = _pil()
    if Image is None:
        _unavailable("/api/media", "PIL_NOT_AVAILABLE", "未安装 PIL，缩略图能力不可用")
    import io as _io

    try:
        try:
            image_source = Image.open(path)
        except Exception:
            if path.suffix.lower() != ".jxl":
                raise
            image_source = Image.open(_io.BytesIO(_decode_jxl_source(path)))
        with image_source as image:
            # 缩略图只要 256px：让 JPEG 解码器直接以 1/2、1/4、1/8 尺度出图，
            # 大图可省一个数量级的解码时间（PNG 不支持 draft，按原样解码）。
            try:
                image.draft("RGB", (max(1, int(width)), max(1, int(width))))
            except Exception:  # noqa: BLE001 - draft 只是加速，失败就按原尺寸解码
                pass
            # 只有源本身带透明通道才保留 alpha；把不透明大图转 RGBA 会白白放大
            # 内存带宽，实测占单张耗时的三成以上。
            keep_alpha = image.mode in ("RGBA", "LA", "PA") or (
                image.mode == "P" and "transparency" in image.info
            )
            # 已经是目标通道数就别再复制一份：convert("RGB") 对 RGB 源也是一次
            # 全图拷贝（3MB 图约 3ms），全库预热里乘 300 多项就是一整秒核时。
            target_mode = "RGBA" if keep_alpha else "RGB"
            if image.mode != target_mode:
                image = image.convert(target_mode)
            ratio = max(1, int(width)) / float(image.width or 1)
            height = max(1, int(round(image.height * ratio)))
            resized = image.resize((max(1, int(width)), height))
            buffer = _io.BytesIO()
            if image_format == "webp":
                # method=6 是最慢档，缩略图上与 4 的体积差不到 2%，耗时差一倍。
                for quality in (72, 60, 48, 36, 32, 28, 24):
                    buffer = _io.BytesIO()
                    resized.save(buffer, format="WEBP", quality=quality, method=4)
                    if buffer.tell() <= MAX_THUMBNAIL_BYTES or quality == 24:
                        result_bytes = buffer.getvalue()
                        break
            elif image_format == "jxl":
                resized.save(buffer, format="PNG")
                result_bytes = _encode_jxl_thumbnail(buffer.getvalue())
            else:
                resized.save(buffer, format="PNG")
                return buffer.getvalue()

            while len(result_bytes) > MAX_THUMBNAIL_BYTES:
                scale = min(
                    0.9,
                    (MAX_THUMBNAIL_BYTES / len(result_bytes)) ** 0.5 * 0.9,
                )
                reduced_width = max(1, int(resized.width * scale))
                reduced_height = max(1, int(resized.height * scale))
                if (reduced_width, reduced_height) == resized.size:
                    if resized.width >= resized.height and resized.width > 1:
                        reduced_width -= 1
                    elif resized.height > 1:
                        reduced_height -= 1
                    else:
                        _unavailable(
                            "/api/media",
                            "THUMBNAIL_SIZE_LIMIT",
                            "缩略图无法压缩至 15 KiB 以内",
                        )
                resized = resized.resize((reduced_width, reduced_height))
                if image_format == "webp":
                    buffer = _io.BytesIO()
                    resized.save(buffer, format="WEBP", quality=24, method=4)
                    result_bytes = buffer.getvalue()
                else:
                    buffer = _io.BytesIO()
                    resized.save(buffer, format="PNG")
                    result_bytes = _encode_jxl_thumbnail(buffer.getvalue())
            return result_bytes
    except CleanroomException:
        raise
    except Exception:
        # 后缀像图片但实际解码失败（损坏文件、伪装扩展名）：降级类型占位图，
        # 不让全库预热因为单个坏文件记一条失败。
        try:
            return _placeholder_thumbnail_bytes(path, width, image_format, "other")
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
    cache_label, directory = resolve_cache_directory(settings, path)
    image_format = _thumbnail_format(settings)
    name = _thumbnail_filename(
        key,
        width=width if key.startswith("media-") else None,
        image_format=image_format,
    )
    relative = cache_label + "/" + name
    # 目录已由唯一解析入口校验（source 走受管产物根，custom 走允许根内真实目录）。
    target = directory / name
    item: dict[str, Any] = {
        "asset_id": asset_id,
        "source": storage.relative_display(path),
        "output": relative,
        "width": width,
        "format": image_format,
    }
    if not replace_existing and _cache_is_fresh(target, path):
        item.update({"status": "cached", "bytes": target.stat().st_size})
        return item
    data = _thumbnail_bytes(path, width, image_format=image_format)
    _atomic_write_bytes(target, data, durable=False)
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
    transcode_videos = bool((payload or {}).get("transcode_videos"))
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

    transcoded = 0
    transcode_cached = 0
    transcode_failed = 0
    transcode_outputs: list[str] = []
    transcode_errors: list[dict[str, str]] = []
    video_candidates = 0
    if transcode_videos:
        for asset_id, item in zip(asset_ids or [""], items, strict=True):
            try:
                path = _registry_path(asset_id) if asset_id else _resolve_input(payload)
            except CleanroomException:
                # 无法解析素材类型时不猜测为视频，也不虚报转码失败。
                continue
            if path.suffix.lower() not in _VIDEO_SUFFIXES:
                continue
            video_candidates += 1
            try:
                converted = _transcode_thumbnail_video(
                    path,
                    settings,
                    replace_existing=replace_existing,
                )
            except CleanroomException as exc:
                code, message = exc.code, exc.message
            except Exception:
                code, message = "MEDIA_PROCESS_FAILED", "媒体转码失败"
            else:
                item["transcode_status"] = converted["status"]
                item["transcode_output"] = converted["output"]
                item["transcode_bytes"] = converted["bytes"]
                transcode_outputs.append(converted["output"])
                if converted["status"] == "generated":
                    transcoded += 1
                else:
                    transcode_cached += 1
                continue

            transcode_failed += 1
            error = {"code": code, "message": message}
            item["transcode_status"] = "failed"
            item["transcode_error"] = error
            transcode_errors.append({"asset_id": asset_id, **error})

    if (
        len(items) == 1
        and items[0].get("status") == "failed"
        and first_error is not None
        and video_candidates == 0
    ):
        raise first_error
    generated = sum(1 for item in items if item.get("status") == "generated")
    cached = sum(1 for item in items if item.get("status") == "cached")
    failed = sum(1 for item in items if item.get("status") == "failed")
    outputs = [item["output"] for item in items if item.get("output")]
    successful = [item for item in items if item.get("status") != "failed"]
    head = successful[0] if successful else (items[0] if items else {})
    data_gaps = []
    if failed:
        data_gaps.append("thumbnail_failures_present")
    if transcode_failed:
        data_gaps.append("video_transcode_failures_present")
    return {
        "asset_id": str((payload or {}).get("asset_id") or "") or head.get("asset_id", ""),
        "source": head.get("source", ""),
        "output": head.get("output", ""),
        "outputs": outputs,
        "items": items,
        "width": width,
        "format": _thumbnail_format(settings),
        "generated": generated,
        "cached": cached,
        "failed": failed,
        "skipped": 0,
        "transcoded": transcoded,
        "transcode_cached": transcode_cached,
        "transcode_failed": transcode_failed,
        "transcode_outputs": transcode_outputs,
        "transcode_errors": transcode_errors,
        "data_status": "partial" if data_gaps else "ok",
        "data_gaps": data_gaps,
    }


def _preview_source_identity(path: Path) -> str:
    """预览缓存键的源身份摘要：源文件 size + mtime_ns。

    源文件被替换（哪怕时间戳回拨）都会产生不同摘要，旧缓存不再命中，
    避免把过期图当成本次结果返回。
    """
    stat = path.stat()
    raw = "%d:%d" % (stat.st_size, stat.st_mtime_ns)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _preview_cache_stem(key: str, width: int) -> str:
    """预览缓存文件名主干：``<key>-w<宽度>``，顺带复用安全字符校验。"""
    return _thumbnail_filename(key, width=width)[: -len(".jpg.png")]


def _preview_cache_name(
    key: str,
    width: int,
    identity: str,
    *,
    video: bool,
    image_format: Literal["png", "jxl", "webp"] = "png",
) -> str:
    """预览缓存文件名包含格式；视频帧预览继续使用 ``.frame.jpg``。

    宽度进入文件名，保证 256 / 512 各自独立命中互不覆盖（AC5）。
    """
    stem = _preview_cache_stem(key, width)
    if video:
        return "%s-%s.frame.jpg" % (stem, identity)
    extension = {"png": ".jpg.png", "jxl": ".jxl", "webp": ".webp"}.get(
        image_format
    )
    if extension is None:
        raise CleanroomException(400, "INVALID_REQUEST", "缩略图格式不受支持")
    return "%s-%s%s" % (stem, identity, extension)


def _cached_preview_bytes(target: Path) -> bytes | None:
    """读取并校验缓存文件可解码；截断 / 非图片返回 None，由调用方清理重算。"""
    try:
        data = target.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    if target.suffix.lower() == ".jxl":
        if data.startswith(b"\x00\x00\x00\x0cJXL \r\n\x87\n") or data.startswith(
            b"\xff\x0a"
        ):
            return data
        return None
    Image = _pil()
    if Image is None:
        return None
    import io as _io

    try:
        with Image.open(_io.BytesIO(data)) as image:
            image.verify()
    except Exception:
        return None
    return data


def _image_pixel_width(target: Path) -> int | None:
    """只读图片头取像素宽度；解不出图片返回 None（不整图解码）。"""
    Image = _pil()
    if Image is None:
        return None
    try:
        with Image.open(target) as image:
            return int(image.size[0])
    except Exception:
        if target.suffix.lower() == ".jxl":
            import io as _io

            try:
                with Image.open(_io.BytesIO(_decode_jxl_source(target))) as image:
                    return int(image.size[0])
            except Exception:
                return None
        return None


def _canonical_generated_hit(
    settings: dict[str, Any],
    source: Path,
    key: str,
    width: int,
    *,
    directory: Path | None = None,
) -> Path | None:
    """预生成缩略图（QIN-508 命名）是否可直接供预览复用。

    ``asset_id`` 命名的产物不含宽度，必须核对真实像素宽度，避免把其它宽度的图
    当成本次请求的宽度返回；``media-`` 命名的产物已带宽度，直接以新鲜度判定。
    """
    if directory is None:
        _, directory = resolve_cache_directory(settings, source)
    name = _thumbnail_filename(
        key,
        width=width if key.startswith("media-") else None,
        image_format=_thumbnail_format(settings),
    )
    target = directory / name
    if not _cache_is_fresh(target, source):
        return None
    if not key.startswith("media-") and _image_pixel_width(target) != width:
        return None
    return target


def _prune_stale_preview_cache(directory: Path, name_prefix: str, keep: Path) -> None:
    """清理同素材同宽度的旧预览缓存（源身份变化后残留的孤儿文件）。"""
    try:
        entries = list(directory.glob(name_prefix + "*"))
    except OSError:
        return
    for entry in entries:
        if entry == keep:
            continue
        try:
            if entry.is_file():
                entry.unlink()
        except OSError:
            continue


def _live_preview_bytes(
    path: Path,
    width: int,
    *,
    video: bool,
    image_format: Literal["png", "jxl", "webp"] = "png",
) -> tuple[bytes, str]:
    """实时生成预览（缓存不可用时唯一出路），不写盘。"""
    if video:
        return _video_frame_bytes(path, width), "image/jpeg"
    extension = {"png": ".png", "jxl": ".jxl", "webp": ".webp"}[image_format]
    return (
        _thumbnail_bytes(path, width, image_format=image_format),
        MIME_BY_SUFFIX[extension],
    )


def preview_bytes(payload: dict[str, Any]) -> dict[str, Any]:
    """给图片/BMP 等返回真实重采样缩略字节；视频走 ffmpeg 抽帧。

    查找顺序：预览缓存（按素材 + 宽度 + 源身份）→ 预生成缩略图产物 → 实时生成。
    命中路径不重新解码源文件；缓存配置越界、磁盘错误、坏文件一律降级为实时
    生成，不把预览整体打成 5xx。
    """
    path = _resolve_input(payload)
    width = int((payload or {}).get("w") or 256)
    width = max(64, min(2048, width))
    video = path.suffix.lower() in _VIDEO_SUFFIXES
    image_format: Literal["png", "jxl", "webp"] = "jxl"
    media_type = "image/jpeg" if video else MIME_BY_SUFFIX[".jxl"]
    asset_id = str((payload or {}).get("asset_id") or "").strip()
    key = _thumbnail_key(asset_id, path)
    source = storage.relative_display(path)

    try:
        identity = _preview_source_identity(path)
        # 预热线程已取过快照时直接复用，避免每项都重读 media_settings.json。
        prepared_settings = (payload or {}).get("_settings")
        settings = (
            prepared_settings
            if isinstance(prepared_settings, dict)
            else get_thumbnail_settings()
        )
        image_format = _thumbnail_format(settings)
        media_type = MIME_BY_SUFFIX["." + image_format]
        # 批量预热会把同一分桶的目录解析结果传进来：目录解析带进程内锁（Windows
        # 并发建目录会撞车），逐项解析等于把整批并发压成串行。
        prepared_dir = (payload or {}).get("_cache_dir")
        if isinstance(prepared_dir, Path):
            directory = prepared_dir
        else:
            _, directory = resolve_cache_directory(settings, path)
        target = directory / _preview_cache_name(
            key, width, identity, video=video, image_format=image_format
        )
    except (CleanroomException, OSError) as exc:
        # 缓存目录不可解析（未登记允许根、目录被移除、磁盘不可用）：降级实时生成。
        # 降级必须留痕：否则「生成了但没落盘」只能靠事后数文件才发现。
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        _count_preview_cache("degraded")
        data, media_type = _live_preview_bytes(
            path, width, video=video, image_format=image_format
        )
        return {"bytes": data, "media_type": media_type, "source": source}

    if _cache_is_fresh(target, path):
        cached = _cached_preview_bytes(target)
        if cached is not None:
            _count_preview_cache("hit")
            _prune_stale_preview_cache(
                directory, _preview_cache_stem(key, width) + "-", target
            )
            return {"bytes": cached, "media_type": media_type, "source": source}
        # 坏缓存（截断 / 非图片）：清掉后按未命中重算。
        try:
            target.unlink()
        except OSError:
            pass

    if not video:
        try:
            canonical = _canonical_generated_hit(
                settings, path, key, width, directory=directory
            )
        except (CleanroomException, OSError):
            canonical = None
        if canonical is not None:
            cached = _cached_preview_bytes(canonical)
            if cached is not None:
                _count_preview_cache("hit")
                _prune_stale_preview_cache(
                    directory, _preview_cache_stem(key, width) + "-", canonical
                )
                media_type = MIME_BY_SUFFIX.get(canonical.suffix.lower(), media_type)
                return {"bytes": cached, "media_type": media_type, "source": source}

    _count_preview_cache("miss")
    data, media_type = _live_preview_bytes(
        path, width, video=video, image_format=image_format
    )
    try:
        _atomic_write_bytes(target, data, durable=False)
        # 预热是批量场景：每写一项就扫一遍目录找旧源身份残留是 O(n²)，几百项就会
        # 把并发吃掉。旧文件不会被命中（identity 已变），留给「重建缓存 / 删除缓存」
        # 统一清理，浏览器侧单次预览仍照常清理。
        if not (payload or {}).get("_skip_prune"):
            _prune_stale_preview_cache(
                directory, _preview_cache_stem(key, width) + "-", target
            )
    except (CleanroomException, OSError):
        # 写盘失败不影响本次预览可用性，但如实记为降级，下次仍会重试落盘。
        _count_preview_cache("degraded")
    return {"bytes": data, "media_type": media_type, "source": source}


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


def _pcm_envelope(path: Path) -> tuple[list[tuple[float, float]], float]:
    """把音频解码为 16-bit PCM 并按固定桶算真实包络，返回 (桶, 时长秒)。

    波形接口与音频缩略图共用这一条实现，禁止出现第二套采样口径。
    """
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
    return buckets, duration


def waveform(payload: dict[str, Any]) -> dict[str, Any]:
    """用 ffmpeg 把音频解码为 16-bit PCM，再按固定桶算真实 RMS 包络。

    无音频流 → 400；ffmpeg 缺失或失败 → 503；**禁止**随机/常量假曲线。
    """
    path = _resolve_input(payload)
    buckets, duration = _pcm_envelope(path)
    return {
        "duration": round(duration, 4),
        "minimum": [b[0] for b in buckets],
        "maximum": [b[1] for b in buckets],
        "bucket_count": len(buckets),
        "sample_rate": 16000,
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
    target = out_dir / _transcode_output_name(path)
    size = _transcode_video_file(path, target, ffmpeg=ffmpeg)
    return {
        "source": storage.relative_display(path),
        "output": target.resolve()
        .relative_to(storage.data_root().resolve())
        .as_posix(),
        "bytes": size,
        "data_status": "ok",
        "data_gaps": [],
    }


def _transcode_output_name(path: Path) -> str:
    stat = path.stat()
    identity = "|".join(
        (storage.relative_display(path), str(stat.st_size), str(stat.st_mtime_ns))
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return "%s.mp4" % digest


def _transcode_video_file(path: Path, target: Path, *, ffmpeg: str | None = None) -> int:
    ffmpeg = ffmpeg or _tool("ffmpeg")
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
    return int(target.stat().st_size)


def _transcode_thumbnail_video(
    path: Path,
    settings: dict[str, Any],
    *,
    replace_existing: bool,
) -> dict[str, Any]:
    cache_label, directory = resolve_cache_directory(settings, path)
    name = _transcode_output_name(path)
    target = directory / name
    output = "%s/%s" % (cache_label.rstrip("/\\"), name)
    if not replace_existing and _transcode_cache_is_fresh(target, path):
        return {"status": "cached", "output": output, "bytes": int(target.stat().st_size)}
    size = _transcode_video_file(path, target)
    return {"status": "generated", "output": output, "bytes": size}


def _transcode_cache_is_fresh(target: Path, source: Path) -> bool:
    try:
        target_stat = target.stat()
        return (
            target.is_file()
            and target_stat.st_size > 0
            and target_stat.st_mtime_ns >= source.stat().st_mtime_ns
        )
    except OSError:
        return False


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


# ---------------------------------------------------------------------------
# 全库预览缓存预热（QIN-519：创作者显式触发，真异步守护线程）
# ---------------------------------------------------------------------------

PREVIEW_ALL_SCOPE = "library"
PREVIEW_ALL_WIDTH = 256
#: fill：增量补齐（已有新鲜缓存跳过）；rebuild：先清空整棵缓存树再全量重生成。
PREVIEW_ALL_MODES = ("fill", "rebuild")
# 限速：每批之间让出 20ms，避免全库预热打满创作者 PC 或 NAS 的 CPU。
PREVIEW_ALL_THROTTLE_SECONDS = 0.02
# 并发度：Pillow 的解码/重采样/编码都在 C 层并释放 GIL，ffmpeg 走子进程，串行跑
# 等于把 CPU 空出来。实测同一批 354 项（24 核）：纯解码 16 路 8.3x / 24 路 9.2x
# 加速，加进程池并无额外收益（spawn 开销吃掉了 GIL 之外的那点提升），所以只用
# 线程池，上限放宽到 32 便于在高核机器上显式压满。
PREVIEW_ALL_DEFAULT_WORKERS = 16
PREVIEW_ALL_MAX_WORKERS = 32
_PREVIEW_ALL_FLUSH_EVERY = 10
# 状态写入的瞬时冲突重试次数（Windows 上并发读 + os.replace 会短暂 PermissionError）。
_PREVIEW_ALL_WRITE_ATTEMPTS = 5
# 本进程内真正存活的预热任务；进程重启后该集合为空，据此识别遗留 running 记录。
_PREVIEW_ALL_LIVE: set[str] = set()
_PREVIEW_ALL_LIVE_LOCK = threading.Lock()


def _preview_all_asset_ids() -> list[str]:
    """全库可预热素材 id（预热范围，不生成任何东西）。

    QIN-525：枚举口径统一收敛到注册表的 ``preview_asset_ids()``，与资产面板
    facets 同源；不再按顶层 ``kind`` 白名单过滤，否则 ``kind="indexed"`` 的
    索引素材会被整体漏掉，任务空跑却报「已完成」。
    """
    from gw.asset_registry import repository as asset_repo

    try:
        return asset_repo.preview_asset_ids()
    except Exception:  # noqa: BLE001 - 注册表不可读时按空范围失败关闭，不越权预热
        return []


def _preview_cache_hit(
    asset_id: str,
    path: Path,
    width: int,
    settings: dict[str, Any],
    *,
    directory: Path | None = None,
) -> bool:
    """与 ``preview_bytes`` 同一口径探测缓存是否新鲜（只 stat，不解码不读字节）。

    探测本身失败（目录不可解析、源文件 stat 失败等）一律按「未命中」处理：
    命中判定只是省一次生成，不能因为它把可生成的项记成失败。

    ``directory`` 由批量预热传入：缓存目录按源目录分桶，同一分桶在一次预热里
    解析一次即可，逐项解析会把进程内的目录锁压成串行瓶颈。
    """
    try:
        image_format = _thumbnail_format(settings)
        identity = _preview_source_identity(path)
        if directory is None:
            _, directory = resolve_cache_directory(settings, path)
        key = _thumbnail_key(asset_id, path)
        video = path.suffix.lower() in _VIDEO_SUFFIXES
        target = directory / _preview_cache_name(
            key, width, identity, video=video, image_format=image_format
        )
        return _cache_is_fresh(target, path)
    except (CleanroomException, OSError):
        return False


def _update_preview_all_job(job_id: str, **changes: Any) -> None:
    """按稳定 job_id 局部更新任务记录；任务已被清理时静默跳过。

    Windows 上 ``os.replace`` 与并发读取存在共享冲突（``PermissionError``），
    瞬时冲突重试即可；写入彻底失败也不允许把异常抛回预热线程——否则线程静默
    死亡，任务永久停在 ``running``，把后续所有预热请求一起顶死。
    """

    def mutate(raw: dict[str, Any]) -> Any:
        job = (raw.get("jobs") or {}).get(job_id)
        if job is None:
            return None
        job.update(changes)
        raw["revision"] = int(raw.get("revision") or 1) + 1
        return job

    for attempt in range(_PREVIEW_ALL_WRITE_ATTEMPTS):
        try:
            _job_state().mutate(mutate)
            return
        except OSError:
            if attempt + 1 >= _PREVIEW_ALL_WRITE_ATTEMPTS:
                raise
            time.sleep(0.02 * (attempt + 1))


def _mark_preview_all_live(job_id: str, live: bool) -> None:
    """登记本进程内真正存活的预热任务；用于识别重启后遗留的 ``running`` 记录。"""
    with _PREVIEW_ALL_LIVE_LOCK:
        if live:
            _PREVIEW_ALL_LIVE.add(job_id)
        else:
            _PREVIEW_ALL_LIVE.discard(job_id)


def _preview_all_live(job_id: str) -> bool:
    with _PREVIEW_ALL_LIVE_LOCK:
        return job_id in _PREVIEW_ALL_LIVE


class _PreviewAllControl:
    """运行中任务的暂停 / 继续 / 取消信号（进程内，不落盘）。

    只做信号，不改任务状态：状态由驱动线程在安全点写入，避免 API 线程与
    预热线程互相覆盖 ``status``。
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        # 置位 = 允许继续；清除 = 暂停中。
        self._running = threading.Event()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self._cancel.set()
        # 唤醒正在等待「继续」的驱动线程，否则取消在暂停态下永远不生效。
        self._running.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def wait_until_runnable(self, *, tick: float = 0.15) -> bool:
        """阻塞到可继续；返回 ``True`` 表示已被取消、应立即终止。"""
        while not self._running.wait(timeout=tick):
            if self._cancel.is_set():
                return True
        return self._cancel.is_set()


_PREVIEW_ALL_CONTROLS: dict[str, _PreviewAllControl] = {}
_PREVIEW_ALL_CONTROL_LOCK = threading.Lock()


def _preview_all_control(job_id: str) -> _PreviewAllControl:
    with _PREVIEW_ALL_CONTROL_LOCK:
        control = _PREVIEW_ALL_CONTROLS.get(job_id)
        if control is None:
            control = _PreviewAllControl()
            _PREVIEW_ALL_CONTROLS[job_id] = control
        return control


def _drop_preview_all_control(job_id: str, control: _PreviewAllControl | None = None) -> None:
    """移除任务的信号对象；只删自己那一份，避免误删同 id 的新任务。"""
    with _PREVIEW_ALL_CONTROL_LOCK:
        if control is None or _PREVIEW_ALL_CONTROLS.get(job_id) is control:
            _PREVIEW_ALL_CONTROLS.pop(job_id, None)


def _preview_all_workers(payload: dict[str, Any] | None) -> int:
    """并发度：显式 ``workers`` 优先，否则按 CPU 数收敛到默认值。"""
    raw = (payload or {}).get("workers")
    if raw is not None:
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            raise CleanroomException(
                400,
                "INVALID_PREVIEW_WORKERS",
                "workers 必须是 1 到 %d 的整数" % PREVIEW_ALL_MAX_WORKERS,
            ) from None
        return max(1, min(PREVIEW_ALL_MAX_WORKERS, requested))
    cpu = os.cpu_count() or 4
    return max(1, min(PREVIEW_ALL_DEFAULT_WORKERS, cpu))


def _run_preview_all(
    job_id: str,
    asset_ids: list[str],
    width: int,
    throttle: float,
    *,
    mode: str = "fill",
    workers: int = 1,
    resolved_paths: dict[str, Path] | None = None,
) -> None:
    """预热线程入口：兜住任何逃逸异常，保证任务不会永久停在 ``running``。"""
    try:
        _drive_preview_all(
            job_id,
            asset_ids,
            width,
            throttle,
            mode=mode,
            workers=workers,
            resolved_paths=resolved_paths,
        )
    except BaseException as exc:  # noqa: BLE001 - 线程死亡必须收敛为可见失败
        _fail_preview_all_job(job_id, "PREVIEW_ALL_CRASHED", exc)
    finally:
        _mark_preview_all_live(job_id, False)


def _fail_preview_all_job(job_id: str, code: str, exc: BaseException) -> None:
    """把任务收敛为 ``failed`` 并留下栈证据；连状态都写不进时只能吞掉。"""
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    try:
        _update_preview_all_job(
            job_id,
            status="failed",
            state="failed",
            error=code,
            finished_at=storage.now_iso(),
        )
    except Exception:  # noqa: BLE001 - 状态文件不可写时已无补救余地
        pass


def _drive_preview_all(
    job_id: str,
    asset_ids: list[str],
    width: int,
    throttle: float,
    *,
    mode: str = "fill",
    workers: int = 1,
    resolved_paths: dict[str, Path] | None = None,
) -> None:
    """守护线程主体：按批并发预热，已有新鲜缓存直接跳过（幂等），单项失败不中断整批。

    ``mode="rebuild"`` 时先清空整棵预览缓存树再全量重生成；清空失败按失败关闭，
    绝不在「没清掉旧缓存」的情况下继续冒充重建。

    并发按批提交（每批 ``workers`` 的两倍），批边界检查暂停/取消信号：既不把
    完成度写成猜值，也让取消/暂停在百毫秒级生效；已在飞行中的项会跑完再退。
    """
    started = time.monotonic()
    total = len(asset_ids)
    generated = skipped = failed = cleared = 0
    processed = 0
    last_flush = 0.0
    control = _preview_all_control(job_id)
    try:
        settings: dict[str, Any] | None = get_thumbnail_settings()
    except CleanroomException:
        settings = None
    if mode == "rebuild":
        if settings is None:
            _update_preview_all_job(
                job_id,
                status="failed",
                state="failed",
                error="PREVIEW_CACHE_UNAVAILABLE",
                finished_at=storage.now_iso(),
            )
            return
        try:
            root = cache_root_path(settings)
            cleared = _delete_cache_artifacts(root)
            _prune_empty_dirs(root)
        except (CleanroomException, OSError):
            _update_preview_all_job(
                job_id,
                status="failed",
                state="failed",
                error="PREVIEW_CACHE_CLEAR_FAILED",
                finished_at=storage.now_iso(),
            )
            return

    # 源文件路径由启动方一次读完（枚举 + 解析合并成一次注册表读取）；为空时按
    # 既有口径回落：逐项解析会慢，但只在注册表合并读取不可用时发生。
    resolved: dict[str, Path] = {
        str(k): v for k, v in (resolved_paths or {}).items() if isinstance(v, Path)
    }
    if not resolved and asset_ids:
        try:
            from gw.asset_registry import repository as asset_repo

            resolved = asset_repo.media_files(asset_ids)
        except Exception:  # noqa: BLE001 - 批量预解析失败不改变逐项失败语义
            resolved = {}
    # 缓存目录按「源目录分桶」，一次预热里同一分桶只解析一次：解析入口带进程内
    # 锁（Windows 并发建目录会撞车），逐项解析会把整批并发压回串行（实测每项
    # 4.5ms × 每项两次 × 354 项 ≈ 3.2 秒纯排队）。
    cache_dirs: dict[Path, Path] = {}
    cache_dir_lock = threading.Lock()
    # 先串行把这一批涉及的分桶解析好：让 12/16 路线程开局同时抢同一把目录锁会
    # 在开头排出一段纯等待（实测每项 4.5ms、并发下中位排队到 50ms）。
    bucket_samples: dict[Path, Path] = {}
    for _path in resolved.values():
        bucket_samples.setdefault(_path.parent, _path)
    for _sample in bucket_samples.values():
        try:
            _, _directory = resolve_cache_directory(settings, _sample)
        except (CleanroomException, OSError):
            # 该分桶解析不了：留给 worker 现场解析并按既有降级语义处理。
            continue
        cache_dirs[_sample.parent] = _directory

    def cache_directory_for(path: Path) -> Path:
        bucket = path.parent
        with cache_dir_lock:
            cached = cache_dirs.get(bucket)
        if cached is not None:
            return cached
        _, directory = resolve_cache_directory(settings, path)
        with cache_dir_lock:
            cache_dirs[bucket] = directory
        return directory

    def warm_one(asset_id: str) -> str:
        """单项预热结果；任何异常都折算成该项失败，绝不冒泡打断整批。"""
        try:
            path = resolved.get(asset_id)
            if path is None:
                path = _registry_path(asset_id)
            # 目录只解析一次：命中判定和落盘用同一份，避免每项两次抢同一把锁。
            directory = cache_directory_for(path)
            if (
                mode != "rebuild"
                and settings is not None
                and _preview_cache_hit(
                    asset_id,
                    path,
                    width,
                    settings,
                    directory=directory,
                )
            ):
                return "skipped"
            # 生成与落盘完全复用浏览器预览同一条路径，不做第二套实现；把已解析
            # 的 path / 设置快照传进去只是省掉重复读取，不改落盘口径。
            preview_bytes(
                {
                    "asset_id": asset_id,
                    "w": width,
                    "_resolved_path": path,
                    "_settings": settings,
                    "_cache_dir": directory,
                    "_skip_prune": True,
                }
            )
            return "generated"
        except Exception as exc:  # noqa: BLE001 - 坏文件/解码异常只影响该项
            # 单项失败必须在日志里留痕：否则全库预热只会给一个失败计数，无从定位。
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            return "failed"

    def flush(force: bool = False) -> None:
        nonlocal last_flush
        now = time.monotonic()
        if not force and now - last_flush < 0.5:
            return
        last_flush = now
        _update_preview_all_job(
            job_id,
            progress={
                "processed": processed,
                "total": total,
                "generated": generated,
                "skipped": skipped,
                "failed": failed,
            },
        )

    cancelled = False
    pool = ThreadPoolExecutor(
        max_workers=max(1, workers), thread_name_prefix="preview-warm"
    )
    try:
        # 滑动窗口而不是「整批提交再等整批返回」：批内只要有一个大文件（本库
        # 28MB PNG、mp4 抽帧）就会拖着其余线程空转，批越大尾部浪费越多。窗口
        # 里始终保持在飞的项，谁先完成就补谁，把 straggler 的影响压到最小。
        window = max(1, workers * 2)
        pending: set[Any] = set()
        iterator = iter(asset_ids)

        def submit_next() -> bool:
            for asset_id in iterator:
                pending.add(pool.submit(warm_one, asset_id))
                return True
            return False

        while len(pending) < window:
            if not submit_next():
                break
        since_throttle = 0
        while pending:
            if control.cancelled:
                cancelled = True
                break
            if control.paused:
                _update_preview_all_job(job_id, status="paused", state="paused")
                if control.wait_until_runnable():
                    cancelled = True
                    break
                if control.cancelled:
                    cancelled = True
                    break
                _update_preview_all_job(job_id, status="running", state="running")
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                outcome = future.result()
                if outcome == "generated":
                    generated += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    failed += 1
                processed = min(total, processed + 1)
                since_throttle += 1
            while len(pending) < window:
                if not submit_next():
                    break
            flush()
            # 限速按窗口触发：最后一批也要让出一次，否则小任务（项数不足一个窗口）
            # 会完全跳过限速，把「真异步」的节奏打乱。
            if throttle > 0 and (since_throttle >= window or not pending):
                since_throttle = 0
                time.sleep(throttle)
        if cancelled and pending:
            # 取消后不再派发，但已提交的项必须收尾并如实计入：否则磁盘上有文件、
            # 结果里没有，取消一次就丢一次统计（幂等缓存不会错，但结果会撒谎）。
            done, pending = wait(pending)
            for future in done:
                try:
                    outcome = future.result()
                except Exception:  # noqa: BLE001 - 收尾阶段同样不冒泡
                    outcome = "failed"
                if outcome == "generated":
                    generated += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    failed += 1
                processed = min(total, processed + 1)
            flush(force=True)
        # 收尾强制落一次进度：进度写盘有 0.5 秒节流，直接写 result 会让「已完成」
        # 的任务带着落后的 progress（末尾几项看起来没做完）。
        flush(force=True)
    finally:
        # 取消后不再派发新批次；飞行中的项跑完即返回，不在这里无限等待。
        pool.shutdown(wait=True, cancel_futures=True)
        _drop_preview_all_control(job_id, control)

    elapsed = round(time.monotonic() - started, 1)
    result = {
        "total": total,
        "processed": processed,
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
        "cleared": cleared,
        "mode": mode,
        "width": width,
        "workers": workers,
        "elapsed_seconds": elapsed,
    }
    if cancelled:
        _update_preview_all_job(
            job_id,
            status="cancelled",
            state="cancelled",
            result={**result, "cancelled": True},
            error=None,
            finished_at=storage.now_iso(),
        )
        return
    status = "failed" if total and failed == total else "succeeded"
    _update_preview_all_job(
        job_id,
        status=status,
        state=status,
        result=result,
        error="PREVIEW_ALL_FAILED" if status == "failed" else None,
        finished_at=storage.now_iso(),
    )


def start_preview_all_job(payload: dict[str, Any]) -> tuple[str, str, int]:
    """启动全库预览缓存预热：立即返回稳定 job_id，由守护线程真实推进。

    ``mode="fill"`` 增量补齐（已有新鲜缓存跳过），``mode="rebuild"`` 先清空整棵
    预览缓存树再全量重生成。同一时刻只保留一个运行中的预热任务（重复提交复用
    既有 job_id，避免重复遍历）；不做任何静默预热，必须由创作者显式触发。
    """
    mode = str((payload or {}).get("mode") or "fill").strip().lower()
    if mode not in PREVIEW_ALL_MODES:
        raise CleanroomException(
            400,
            "INVALID_PREVIEW_MODE",
            "mode 只接受 fill / rebuild",
        )
    state = _job_state().read()
    for record in (state.get("jobs") or {}).values():
        if record.get("kind") != "preview_all":
            continue
        if record.get("status") not in ("queued", "running", "paused"):
            continue
        existing_id = str(record.get("job_id") or "")
        if _preview_all_live(existing_id):
            return (
                existing_id,
                "/api/asset-thumbnails/jobs/%s" % existing_id,
                int((record.get("progress") or {}).get("total") or 0),
            )
        # 进程重启、崩溃或线程异常退出都会留下永远 ``running`` 的记录；不复原就
        # 会顶死后续每一次预热请求（按钮点了没反应）。收敛为可见失败后继续建新任务。
        _update_preview_all_job(
            existing_id,
            status="failed",
            state="failed",
            error="PREVIEW_ALL_INTERRUPTED",
            finished_at=storage.now_iso(),
        )

    # 枚举与路径一次读完：分两次读 800KB 注册表是预热启动的固定开销（各约
    # 0.25s / 0.32s），合并后只剩一次；解析不到的项在 worker 内回落单条查询。
    resolved_paths: dict[str, Path] = {}
    try:
        from gw.asset_registry import repository as asset_repo

        resolved_paths = asset_repo.preview_asset_paths()
    except Exception:  # noqa: BLE001 - 注册表不可读时按空范围失败关闭，不越权预热
        resolved_paths = {}
    asset_ids = sorted(resolved_paths)
    try:
        width = int((payload or {}).get("width") or PREVIEW_ALL_WIDTH)
    except (TypeError, ValueError):
        width = PREVIEW_ALL_WIDTH
    width = max(64, min(2048, width))
    workers = _preview_all_workers(payload)
    throttle = PREVIEW_ALL_THROTTLE_SECONDS
    raw_throttle = (payload or {}).get("throttle_ms")
    if raw_throttle is not None:
        try:
            throttle = max(0.0, min(1.0, float(raw_throttle) / 1000.0))
        except (TypeError, ValueError):
            throttle = PREVIEW_ALL_THROTTLE_SECONDS

    def mutate(raw: dict[str, Any]) -> str:
        raw["sequence"] = int(raw.get("sequence", 0)) + 1
        job_id = "pjob_%04d" % raw["sequence"]
        raw.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "status": "running",
            "state": "running",
            "kind": "preview_all",
            "mode": mode,
            "workers": workers,
            "progress": {
                "processed": 0,
                "total": len(asset_ids),
                "generated": 0,
                "skipped": 0,
                "failed": 0,
            },
            "result": None,
            "error": None,
            "created_at": storage.now_iso(),
            "finished_at": None,
        }
        raw["revision"] = int(raw.get("revision") or 1) + 1
        return job_id

    job_id = _job_state().mutate(mutate)
    if asset_ids or mode == "rebuild":
        # 新建任务前清掉同 id 的旧信号（极端情况下序号复用），保证新线程拿到干净的
        # 暂停/取消状态，不被上一个任务的遗留信号带着跑。
        _drop_preview_all_control(job_id)
        # 重建即使范围为空也要跑：清空动作本身是用户请求的一部分。
        # 先登记存活再起线程：避免并发提交把「还没来得及登记的活任务」误判为遗留。
        _mark_preview_all_live(job_id, True)
        try:
            threading.Thread(
                target=_run_preview_all,
                args=(job_id, asset_ids, width, throttle),
                kwargs={
                    "mode": mode,
                    "workers": workers,
                    "resolved_paths": resolved_paths,
                },
                name="preview-all-%s" % job_id,
                daemon=True,
            ).start()
        except (OSError, RuntimeError):
            _mark_preview_all_live(job_id, False)
            _update_preview_all_job(
                job_id,
                status="failed",
                state="failed",
                error="PREVIEW_ALL_START_FAILED",
                finished_at=storage.now_iso(),
            )
    else:
        _update_preview_all_job(
            job_id,
            status="succeeded",
            state="succeeded",
            result={
                "total": 0,
                "generated": 0,
                "skipped": 0,
                "failed": 0,
                "cleared": 0,
                "mode": mode,
                "width": width,
                "elapsed_seconds": 0.0,
            },
            finished_at=storage.now_iso(),
        )
    return job_id, "/api/asset-thumbnails/jobs/%s" % job_id, len(asset_ids)


PREVIEW_ALL_CONTROL_ACTIONS = ("pause", "resume", "cancel")


def _read_job_record(job_id: str) -> dict[str, Any] | None:
    """带瞬时冲突重试地读取单条任务记录；不存在返回 ``None``。"""
    for attempt in range(_PREVIEW_ALL_WRITE_ATTEMPTS):
        try:
            return (_job_state().read().get("jobs") or {}).get(job_id)
        except OSError:
            if attempt + 1 >= _PREVIEW_ALL_WRITE_ATTEMPTS:
                raise
            time.sleep(0.02 * (attempt + 1))
    return None


def control_preview_all_job(
    job_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """暂停 / 继续 / 取消运行中的预览预热任务，返回控制后的任务快照。

    只投递信号，不在这里等待线程收敛：取消后飞行中的项仍会跑完，``status``
    由驱动线程在下一个安全点写成 ``cancelled``，前端轮询即可看到终态。
    """
    action = str((payload or {}).get("action") or "").strip().lower()
    if action not in PREVIEW_ALL_CONTROL_ACTIONS:
        raise CleanroomException(
            400, "INVALID_PREVIEW_ACTION", "action 只接受 pause / resume / cancel"
        )
    record = _read_job_record(job_id)
    if not record or record.get("kind") != "preview_all":
        raise CleanroomException(404, "PREVIEW_JOB_NOT_FOUND", "预览缓存任务不存在")
    if record.get("status") not in ("queued", "running", "paused"):
        raise CleanroomException(
            409, "PREVIEW_JOB_FINISHED", "任务已结束，不能再暂停或取消"
        )
    with _PREVIEW_ALL_CONTROL_LOCK:
        control = _PREVIEW_ALL_CONTROLS.get(job_id)
    if control is None:
        # 进程重启后遗留的 running 记录没有任何存活信号，按中断收敛而不是假装能控制。
        _update_preview_all_job(
            job_id,
            status="failed",
            state="failed",
            error="PREVIEW_ALL_INTERRUPTED",
            finished_at=storage.now_iso(),
        )
        raise CleanroomException(
            409, "PREVIEW_JOB_NOT_RUNNING", "任务已随服务重启中断"
        )
    if action == "pause":
        control.pause()
        _update_preview_all_job(job_id, status="paused", state="paused")
    elif action == "resume":
        control.resume()
        _update_preview_all_job(job_id, status="running", state="running")
    else:
        # 取消：保持 running 直到驱动线程在安全点写出 cancelled，避免前端提前
        # 认为任务已终止却仍在写盘。
        control.cancel()
    return get_job(job_id)



def cache_statistics() -> dict[str, Any]:
    """预览缓存统计：分类文件数、占用字节与已缓存素材数（供设置面板展示）。

    只枚举并 stat 缓存树，不解码、不写盘；缓存根不可解析时返回
    ``available=False``，前端据此显示「缓存目录不可用」而不是复述旧数字。
    """
    try:
        settings = get_thumbnail_settings()
        root = cache_root_path(settings)
    except (CleanroomException, OSError):
        return {
            "available": False,
            "total_assets": 0,
            "cached_assets": 0,
            "bytes": 0,
            "categories": [
                {"id": key, "files": 0, "bytes": 0}
                for key in PREVIEW_CACHE_CATEGORIES
            ],
        }

    counts: dict[str, int] = {key: 0 for key in PREVIEW_CACHE_CATEGORIES}
    sizes: dict[str, int] = {key: 0 for key in PREVIEW_CACHE_CATEGORIES}
    keys: set[str] = set()
    total_bytes = 0
    for path in _iter_cache_artifacts(root):
        category = cache_artifact_category(path.name)
        counts[category] += 1
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        sizes[category] += size
        total_bytes += size
        if category in ("image", "video"):
            keys.add(_cache_artifact_key(path.name))
    return {
        "available": True,
        "total_assets": len(_preview_all_asset_ids()),
        "cached_assets": len(keys),
        "bytes": total_bytes,
        "categories": [
            {"id": key, "files": counts[key], "bytes": sizes[key]}
            for key in PREVIEW_CACHE_CATEGORIES
        ],
    }


def _storyboard_error_item(asset_id: str, exc: CleanroomException) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "status": "failed",
        "error": {"code": exc.code, "message": exc.message},
    }


def generate_storyboards(payload: dict[str, Any]) -> dict[str, Any]:
    """批量预生成视频九宫格与片段缩略图，并把真实路径回写注册表。

    逐项处理并返回真实 ``generated / cached / failed`` 计数：单项失败不中断整批，
    失败项的 ``video_segments[].thumbnail`` 保持原值（由 media_probe 失败关闭保证）。
    """
    from gw.asset_registry import media_probe

    asset_ids = _asset_ids(payload)
    if not asset_ids:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 asset_ids")
    force = bool((payload or {}).get("force"))
    items: list[dict[str, Any]] = []
    first_error: CleanroomException | None = None
    for asset_id in dict.fromkeys(asset_ids):
        try:
            result = media_probe.storyboard_thumbnails(
                asset_id, cache_root=cache_root_relative(), force=force
            )
        except CleanroomException as exc:
            if first_error is None:
                first_error = exc
            items.append(_storyboard_error_item(asset_id, exc))
            continue
        produced = int(result.get("generated") or 0)
        grid = result.get("storyboard") or {}
        items.append(
            {
                "asset_id": asset_id,
                "status": "generated" if produced else "cached",
                # 中间帧合成后即删除，对外产物是那张固定横板的 3×3 单图。
                "output": grid.get("image", ""),
                "frames": int(result.get("frames") or 0),
                "frames_generated": int(grid.get("generated") or 0),
                "segment_total": int(result.get("segment_total") or 0),
                "segments_updated": int(result.get("segments_updated") or 0),
            }
        )
    if len(items) == 1 and items[0].get("status") == "failed" and first_error is not None:
        raise first_error
    generated = sum(1 for item in items if item.get("status") == "generated")
    cached = sum(1 for item in items if item.get("status") == "cached")
    failed = sum(1 for item in items if item.get("status") == "failed")
    outputs = [item["output"] for item in items if item.get("output")]
    successful = [item for item in items if item.get("status") != "failed"]
    head = successful[0] if successful else {}
    if successful:
        # 真有分镜产物才把能力开关置真：前端据此显示九宫格入口。
        from gw.asset_registry import repository as registry

        try:
            registry.update_feature("video_storyboard", True)
        except CleanroomException:
            # 能力开关写失败不影响已落盘产物，但不得虚报开关已生效。
            pass
    return {
        "asset_id": head.get("asset_id", ""),
        "output": head.get("output", ""),
        "outputs": outputs,
        "items": items,
        "generated": generated,
        "cached": cached,
        "failed": failed,
        "frames": sum(int(item.get("frames") or 0) for item in items),
        "segments_updated": sum(int(item.get("segments_updated") or 0) for item in items),
        "data_status": "partial" if failed else "ok",
        "data_gaps": ["storyboard_failures_present"] if failed else [],
    }


def run_background_storyboards(payload: dict[str, Any]) -> tuple[str, str]:
    """真实生成分镜并登记为可回读的后台任务（返回稳定 job_id + poll_hint）。"""
    result = generate_storyboards(payload)
    outputs = result.get("outputs") or []
    file_name = Path(outputs[0]).name if outputs else ""
    total = max(1, len(result.get("items") or []))

    def mutate(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raw["sequence"] = int(raw.get("sequence", 0)) + 1
        job_id = "sjob_%04d" % raw["sequence"]
        record = {
            "job_id": job_id,
            "kind": "storyboards",
            "status": "succeeded",
            "state": "succeeded",
            "progress": {"processed": total, "total": total},
            "result": result,
            "output": result.get("output", ""),
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


def _thumbnail_format(settings: dict[str, Any]) -> ThumbnailFormat:
    image_format = (settings or {}).get("format")
    return image_format if image_format in ("jxl", "webp") else "jxl"


def _thumbnail_filename(
    asset_id: str,
    *,
    width: int | None = None,
    image_format: Literal["png", "jxl", "webp"] = "png",
) -> str:
    if (
        not asset_id
        or len(asset_id) > 128
        or not all(ch.isalnum() or ch in "_-" for ch in asset_id)
    ):
        raise CleanroomException(400, "INVALID_ASSET_ID", "素材标识不是安全单段")
    suffix = "-w%04d" % width if width else ""
    extension = {"png": ".jpg.png", "jxl": ".jxl", "webp": ".webp"}.get(
        image_format
    )
    if extension is None:
        raise CleanroomException(400, "INVALID_REQUEST", "缩略图格式不受支持")
    return "%s%s%s" % (asset_id, suffix, extension)


def _atomic_write_bytes(target: Path, data: bytes, *, durable: bool = True) -> None:
    """派生物统一原子写：先写同目录 .part，再替换最终文件。

    ``durable=False`` 跳过 fsync：缓存类产物是可再生的派生数据，进程崩溃也不会
    留下半截文件（``replace`` 原子），断电丢失只会导致下次重新生成。全库预热时
    每项一次 fsync 会把 8 路并发压成单线程（实测 354 项从 3 秒退化到 15 秒）。
    """
    partial = target.parent / (target.stem + ".part" + target.suffix)
    try:
        partial.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as output:
            output.write(data)
            output.flush()
            if durable:
                os.fsync(output.fileno())
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def _thumbnail_cache_names(asset_id: str, names: Iterable[str]) -> list[str]:
    """按资产匹配缩略图产物名（精确名 + 预览派生前缀；不误命中其他素材）。"""
    extensions = (".jpg.png", ".jxl", ".webp")
    hits = [asset_id + extension for extension in extensions if asset_id + extension in names]
    # 预览派生缓存（QIN-509 命名）：<asset_id>-w<宽度>-<源身份>.<格式> / .frame.jpg。
    # 素材 id 不含 "-w"，因此该前缀只会命中同一素材的预览产物。
    hits.extend(
        name
        for name in names
        if name.startswith(asset_id + "-w")
        and name.endswith((".jpg.png", ".jxl", ".webp", ".frame.jpg"))
    )
    return hits


def _thumbnail_cache_index() -> dict[str, list[tuple[str, str | None, Path | None]]]:
    """按当前缓存模式建立「文件名 → 候选位置」索引。

    自定义根模式只覆盖所选目录本身，切换模式后不会误删受管根下的同名产物；
    受管根模式沿用全量扫描，历史 cache_folder 遗留产物也能清理。
    """
    settings = get_thumbnail_settings()
    mode = str((settings or {}).get("mode") or "source")
    index: dict[str, list[tuple[str, str | None, Path | None]]] = {}
    if mode == "custom":
        label, directory = _custom_cache_directory(settings, _cache_folder_name(settings))
        try:
            entries = sorted(entry for entry in directory.iterdir() if entry.is_file())
        except OSError:
            entries = []
        for entry in entries:
            index.setdefault(entry.name, []).append(
                (label + "/" + entry.name, None, entry)
            )
        return index
    for relative, path in storage.iter_output_paths("media_output"):
        index.setdefault(path.name, []).append((relative, relative, None))
    return index


def _verified_cache_file(path: Path) -> Path:
    """自定义根内文件删除前的再校验：仍必须落在允许根内，链接逃逸即拒绝。"""
    return storage.resolve_within_roots(str(path))


def _resolve_cache_delete_target(path: Path) -> Path:
    """删除缓存前的再校验入口。

    受管缓存根位于数据根内，走产物路径准入（``resolve_output_path``）；
    自定义缓存根位于允许根内，走 ``resolve_within_roots``。链接逃逸一律拒绝。
    """
    try:
        relative = path.resolve().relative_to(storage.data_root().resolve()).as_posix()
    except (ValueError, OSError):
        return storage.resolve_within_roots(str(path))
    return storage.resolve_output_path(relative)


def _delete_cache_categories(payload: dict[str, Any]) -> set[str]:
    """解析删除弹窗勾选的缓存分类；``all`` 展开为四类并集。"""
    raw = (payload or {}).get("categories")
    if raw is None:
        return set(PREVIEW_CACHE_CATEGORIES)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        raise CleanroomException(400, "INVALID_REQUEST", "categories 必须是数组")
    requested = {str(item).strip().lower() for item in raw if str(item).strip()}
    if "all" in requested:
        return set(PREVIEW_CACHE_CATEGORIES)
    unknown = requested - set(PREVIEW_CACHE_CATEGORIES)
    if unknown:
        raise CleanroomException(
            400,
            "INVALID_CACHE_CATEGORY",
            "categories 只接受 image / video / storyboard / other / all",
        )
    if not requested:
        raise CleanroomException(400, "INVALID_REQUEST", "categories 不能为空")
    return requested


def _delete_cache_scope(payload: dict[str, Any]) -> dict[str, Any]:
    """按分类清空预览缓存树（QIN-525「删除缓存」）。

    只处理 ``is_cache_artifact_name()`` 命中的缓存产物；每个文件删除前重新做
    允许根与链接逃逸校验，失败逐条记录，绝不虚报整批成功。
    """
    categories = _delete_cache_categories(payload)
    settings = get_thumbnail_settings()
    root = cache_root_path(settings)
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    for path in _iter_cache_artifacts(root):
        if cache_artifact_category(path.name) not in categories:
            continue
        display = storage.relative_display(path)
        try:
            _resolve_cache_delete_target(path).unlink()
            removed.append(display)
        except CleanroomException:
            failed.append({"name": display, "code": "CACHE_PATH_CHANGED"})
        except OSError:
            failed.append({"name": display, "code": "CACHE_DELETE_FAILED"})
    _prune_empty_dirs(root)
    return {
        "kind": "cache",
        "scope": "all",
        "categories": sorted(categories),
        "removed": len(removed),
        # 全库可能上千项：只回传前 100 条明细，避免响应体撑爆。
        "removed_files": removed[:100],
        "failed": failed[:100],
        "metadata_cleared": 0,
        "data_status": "partial" if failed else "ok",
        "data_gaps": [],
    }


def delete_outputs(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """仅删除本模块精确生成的缓存；所有候选先预检，失败不虚报整批完成。"""
    if kind not in ("thumbnails", "storyboards"):
        raise CleanroomException(
            400, "INVALID_REQUEST", "kind只接受thumbnails/storyboards"
        )
    if str((payload or {}).get("scope") or "").strip() == "all":
        return _delete_cache_scope(payload)
    ids = _asset_ids(payload)
    if not ids:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少asset_ids")
    from gw.asset_registry import media_probe

    candidates: list[tuple[str, str | None, Path | None]] = []
    owners: dict[str, str] = {}
    # 缓存按 cache_folder/分桶分层存储，位置随设置变化；删除按精确文件名索引，
    # 不假设固定目录层级，也不触碰代理与分镜产物。
    thumbnail_index: dict[str, list[tuple[str, str | None, Path | None]]] | None = None
    for asset_id in dict.fromkeys(ids):
        if kind == "thumbnails":
            if thumbnail_index is None:
                thumbnail_index = _thumbnail_cache_index()
            for name in _thumbnail_cache_names(asset_id, thumbnail_index.keys()):
                candidates.extend(thumbnail_index.get(name, []))
        else:
            # 新缓存根（cache_folder 口径）与旧固定根一并扫描：历史产物不留孤儿，
            # 同时保留「先解析目录准入、再匹配精确文件名」的链接拒绝语义。
            for directory in (
                media_probe.storyboard_directory(
                    asset_id, cache_root=cache_root_relative()
                ),
                media_probe.legacy_storyboard_directory(asset_id),
            ):
                if not directory.exists():
                    continue
                # 版本桶与默认桶一并清理；逐文件准入校验，不跟随链接。
                for child in sorted(directory.rglob("*")):
                    if not child.is_file():
                        continue
                    relative = child.relative_to(
                        storage.data_root().resolve()
                    ).as_posix()
                    storage.resolve_output_path(relative)
                    if media_probe.is_storyboard_cache_name(child.name):
                        candidates.append((relative, relative, None))
                        owners.setdefault(relative, asset_id)
    removed, failed = [], []
    for display, relative, absolute in candidates:
        try:
            target = (
                storage.resolve_output_path(relative)
                if relative is not None
                else _verified_cache_file(absolute)
            )
            target.unlink()
            removed.append(display)
        except CleanroomException:
            failed.append({"name": display, "code": "CACHE_PATH_CHANGED"})
        except OSError:
            failed.append({"name": display, "code": "CACHE_DELETE_FAILED"})
    metadata_cleared = 0
    if kind == "storyboards":
        metadata_cleared = _clear_storyboard_metadata(
            ids, owners, {item["name"] for item in failed}
        )
        _sync_storyboard_feature()
    return {
        "kind": kind,
        "asset_ids": ids,
        "removed": len(removed),
        "removed_files": removed,
        "failed": failed,
        "metadata_cleared": metadata_cleared,
        "data_status": "partial" if failed else "ok",
        "data_gaps": ["legacy_shared_frames_preserved"]
        if kind == "storyboards"
        else [],
    }


#: 分镜产物路径特征；只清理本模块写出的缩略图字段，不误清其他来源的登记值。
_STORYBOARD_PATH_MARKER = "/storyboards/"


def _is_storyboard_thumbnail(value: Any) -> bool:
    text = str(value or "").replace("\\", "/")
    return _STORYBOARD_PATH_MARKER in text


def _clear_storyboard_metadata(
    asset_ids: list[str], owners: dict[str, str], failed: set[str]
) -> int:
    """删除分镜缓存后清空对应 ``video_segments[].thumbnail``，不留指向空文件的路径。"""
    from gw.asset_registry import repository as registry

    cleared = 0
    for asset_id in dict.fromkeys(asset_ids):
        try:
            record = registry.get_asset(asset_id)
        except CleanroomException:
            continue
        metadata = record.get("metadata")
        segments = metadata.get("video_segments") if isinstance(metadata, dict) else None
        if not isinstance(segments, list) or not segments:
            continue
        updated: list[Any] = []
        changed = False
        for segment in segments:
            if not isinstance(segment, dict) or not _is_storyboard_thumbnail(
                segment.get("thumbnail")
            ):
                updated.append(segment)
                continue
            thumb = str(segment["thumbnail"])
            if owners.get(thumb, asset_id) != asset_id or thumb in failed:
                # 该帧删除失败或不属于本素材时保留原值，避免元数据与磁盘不一致。
                updated.append(segment)
                continue
            updated.append({**segment, "thumbnail": None})
            changed = True
        if not changed:
            continue
        try:
            registry.update_asset(asset_id, {"video_segments": updated})
        except CleanroomException:
            continue
        cleared += 1
    return cleared


def _sync_storyboard_feature() -> None:
    """分镜产物全部清空后把能力开关置假，保证开关状态与磁盘一致。"""
    from gw.asset_registry import repository as registry

    for relative, _path in storage.iter_output_paths("media_output"):
        normalized = relative.replace("\\", "/")
        if _STORYBOARD_PATH_MARKER not in normalized:
            continue
        # 帧文件（.png）与九宫格合成单图（.jpg）任一类还在，就说明产物未清空。
        if normalized.endswith(".png") or normalized.endswith(".jpg"):
            return
    try:
        registry.update_feature("video_storyboard", False)
    except CleanroomException:
        pass
