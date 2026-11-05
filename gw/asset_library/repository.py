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

"""Phase 12 本机文件、内容、分类与本地素材的真实数据源工具。

本模块只承载**不涉及素材库树状态**的能力：
- 本机文件访问（严格锚定 `GW_ALLOWED_ROOTS`）；
- 素材文本内容与不可变版本历史；
- 确定性分类规则与后台任务状态；
- 本地素材索引与存储产物管理。

素材库/分类/条目树的状态在 `asset_library.service.AssetLibraryService` 中统一管理
（支持内存与落盘两种模式），避免同一份状态出现两个写入方。

证据边界：全部为单实例；多 worker 并发写属部署方职责，未闭环。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from gw.core import storage
from gw.core.errors import CleanroomException

NS_CLASSIFY_PROMPT = "asset_classify_prompt"
NS_CLASSIFY_JOBS = "asset_classify_jobs"
NS_CONTENT = "asset_content"
NS_LOCAL = "local_asset_index"
NS_AVATAR = "asset_avatar_registry"

# 本机素材文件副作用与索引身份校验共用单进程域锁。
_LOCAL_ASSET_DOMAIN_LOCK = threading.RLock()

#: 单次上传的字节上限（写死在契约里）。
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: 确定性分类规则：类别 → 扩展名集合。顺序即优先级，命中即返回。
EXTENSION_RULES: tuple[tuple[str, str], ...] = (
    ("image", ".png .jpg .jpeg .webp .bmp .gif .tif .tiff .avif .heic .jxl"),
    ("video", ".mp4 .mov .mkv .avi .webm .m4v .flv .wmv"),
    ("audio", ".wav .mp3 .flac .aac .ogg .m4a .wma"),
    ("document", ".pdf .txt .md .doc .docx .rtf .csv .json .yaml .yml .xml"),
    ("archive", ".zip .7z .rar .tar .gz .bz2 .xz"),
    ("workflow", ".godmap .flow .graph"),
)

DEFAULT_CLASSIFY_PROMPT = (
    "按扩展名与文件名关键词做确定性归类：图片 / 视频 / 音频 / 文档 / 压缩包 / 工作流。"
    "无法判定时归入「未分类」，禁止猜测内容语义。"
)

IMAGE_SUFFIXES = frozenset(".png .jpg .jpeg .webp .bmp .gif .tif .tiff .jxl".split())
UNSUPPORTED_IMAGE_PROBE_SUFFIXES = frozenset({".avif", ".heic"})


# ---------------------------------------------------------------------------
# 确定性分类
# ---------------------------------------------------------------------------


def classify_name(name: str) -> str:
    """按扩展名做确定性归类；无法判定返回「未分类」。"""
    suffix = Path(name or "").suffix.lower()
    if not suffix:
        return "未分类"
    for label, extensions in EXTENSION_RULES:
        if suffix in extensions.split():
            return label
    return "未分类"


def get_classification_prompt() -> dict[str, Any]:
    state = storage.JsonState(
        NS_CLASSIFY_PROMPT, lambda: {"prompt": DEFAULT_CLASSIFY_PROMPT}
    )
    return {
        "prompt": state.read().get("prompt", DEFAULT_CLASSIFY_PROMPT),
        "data_status": "ok",
    }


def set_classification_prompt(prompt: str) -> dict[str, Any]:
    clean = (prompt or "").strip()
    if not clean:
        raise CleanroomException(400, "INVALID_REQUEST", "提示词不能为空")
    storage.JsonState(
        NS_CLASSIFY_PROMPT, lambda: {"prompt": DEFAULT_CLASSIFY_PROMPT}
    ).write({"prompt": clean})
    return {"prompt": clean, "data_status": "ok"}


def _job_state() -> storage.JsonState:
    return storage.JsonState(
        NS_CLASSIFY_JOBS, lambda: {"jobs": {}, "sequence": 0, "active": None}
    )


def record_classification_job(
    results: list[dict[str, Any]], requested: int
) -> dict[str, Any]:
    """记录一次真实完成的分类任务，返回真实 job_id 与 poll_hint。"""

    def mutate(raw: dict[str, Any]) -> Any:
        raw["sequence"] = int(raw.get("sequence", 0)) + 1
        job_id = "acjob_%04d" % raw["sequence"]
        raw.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "state": "completed",
            "requested": requested,
            "classified": len(results),
            "results": results,
            "finished_at": storage.now_iso(),
        }
        raw["active"] = None
        return dict(raw["jobs"][job_id])

    job = _job_state().mutate(mutate)
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "results": job["results"],
        "classified": job["classified"],
        "poll_hint": "/api/asset-classification/jobs/%s" % job["job_id"],
    }


def stop_classification_job() -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        active = raw.get("active")
        if active and active in raw.get("jobs", {}):
            raw["jobs"][active]["state"] = "cancelled"
        raw["active"] = None
        return {"cancelled": bool(active), "active": None}

    return _job_state().mutate(mutate)


def get_classification_job(job_id: str) -> dict[str, Any]:
    job = _job_state().read().get("jobs", {}).get(job_id)
    if not job:
        raise CleanroomException(404, "CLASSIFICATION_JOB_NOT_FOUND", "分类任务不存在")
    return dict(job)


# ---------------------------------------------------------------------------
# 素材文本内容与不可变版本历史
# ---------------------------------------------------------------------------


def _content_state() -> storage.JsonState:
    return storage.JsonState(NS_CONTENT, lambda: {"documents": {}})


def _doc(raw: dict[str, Any], asset_id: str) -> dict[str, Any]:
    return raw["documents"].setdefault(
        asset_id, {"content": "", "version_id": None, "versions": []}
    )


def get_content(asset_id: str) -> dict[str, Any]:
    doc = _content_state().read()["documents"].get(asset_id)
    if doc is None or not doc.get("version_id"):
        return {
            "asset_id": asset_id,
            "content": "",
            "version_id": None,
            "data_status": "empty",
            "data_gaps": ["content_not_created"],
        }
    return {
        "asset_id": asset_id,
        "content": doc["content"],
        "version_id": doc["version_id"],
        "data_status": "ok",
    }


def set_content(asset_id: str, content: str) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        doc = _doc(raw, asset_id)
        if doc.get("version_id"):
            doc["versions"].append(
                {
                    "version_id": doc["version_id"],
                    "content": doc["content"],
                    "created_at": storage.now_iso(),
                }
            )
        version_id = "ver_%s_%04d" % (asset_id, len(doc["versions"]) + 1)
        doc["content"] = content
        doc["version_id"] = version_id
        return {
            "asset_id": asset_id,
            "version_id": version_id,
            "content": content,
            "data_status": "ok",
        }

    return _content_state().mutate(mutate)


def list_versions(asset_id: str) -> dict[str, Any]:
    doc = _content_state().read()["documents"].get(asset_id)
    if doc is None:
        return {"asset_id": asset_id, "versions": [], "data_status": "empty"}
    versions: list[dict[str, Any]] = []
    if doc.get("version_id"):
        versions.append(
            {
                "version_id": doc["version_id"],
                "content": doc["content"],
                "created_at": None,
                "current": True,
            }
        )
    for entry in doc.get("versions", []):
        versions.append(
            {
                "version_id": entry["version_id"],
                "content": entry["content"],
                "created_at": entry["created_at"],
                "current": False,
            }
        )
    return {"asset_id": asset_id, "versions": versions, "data_status": "ok"}


def get_version(asset_id: str, version_id: str) -> dict[str, Any]:
    doc = _content_state().read()["documents"].get(asset_id) or {}
    if doc.get("version_id") == version_id:
        return {
            "asset_id": asset_id,
            "version_id": version_id,
            "content": doc["content"],
            "current": True,
        }
    for entry in doc.get("versions", []):
        if entry["version_id"] == version_id:
            return {
                "asset_id": asset_id,
                "version_id": version_id,
                "content": entry["content"],
                "current": False,
            }
    raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")


def update_version(asset_id: str, version_id: str, content: str) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        doc = raw["documents"].get(asset_id)
        if doc is None:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")
        if doc.get("version_id") == version_id:
            doc["content"] = content
        else:
            for entry in doc.get("versions", []):
                if entry["version_id"] == version_id:
                    entry["content"] = content
                    break
            else:
                raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")
        return {
            "asset_id": asset_id,
            "version_id": version_id,
            "content": content,
            "data_status": "ok",
        }

    return _content_state().mutate(mutate)


def delete_version(asset_id: str, version_id: str) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        doc = raw["documents"].get(asset_id)
        if doc is None:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")
        if doc.get("version_id") == version_id:
            raise CleanroomException(
                409, "CANNOT_DELETE_CURRENT_VERSION", "不能删除当前版本"
            )
        before = len(doc.get("versions", []))
        doc["versions"] = [
            e for e in doc.get("versions", []) if e["version_id"] != version_id
        ]
        if len(doc["versions"]) == before:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")
        return {"asset_id": asset_id, "version_id": version_id, "deleted": True}

    return _content_state().mutate(mutate)


def restore_version(asset_id: str, version_id: str) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        doc = raw["documents"].get(asset_id)
        if doc is None:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")
        content = None
        for entry in doc.get("versions", []):
            if entry["version_id"] == version_id:
                content = entry["content"]
                break
        if content is None:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "版本不存在")
        if doc.get("version_id"):
            doc["versions"].append(
                {
                    "version_id": doc["version_id"],
                    "content": doc["content"],
                    "created_at": storage.now_iso(),
                }
            )
        doc["content"] = content
        doc["version_id"] = "ver_%s_%04d" % (asset_id, len(doc["versions"]) + 1)
        return {
            "asset_id": asset_id,
            "version_id": doc["version_id"],
            "content": content,
            "restored_from": version_id,
            "data_status": "ok",
        }

    return _content_state().mutate(mutate)


def content_pdf(asset_id: str) -> bytes:
    """完整中文正文导出；UTF-8附件保存分页/转义前原文。"""
    from gw.core.text_pdf import build_text_pdf

    doc = _content_state().read()["documents"].get(asset_id)
    text = (doc or {}).get("content") or "（该素材暂无已保存内容）"
    return build_text_pdf(text)


# ---------------------------------------------------------------------------
# 本机文件（严格锚定 GW_ALLOWED_ROOTS）
# ---------------------------------------------------------------------------


def _indexed_root_id(
    raw_path: str, entries: dict[str, Any] | None = None
) -> str | None:
    """相对路径在多根部署下仅当本地索引里恰有一个来源根拥有它时才返回该根。

    返回 ``root_identity_id`` 形态；零个或多个候选一律返回 None，由严格解析器
    继续给出 409 ROOT_ID_REQUIRED，不对多根做任何猜测。
    """
    roots = storage.allowed_roots()
    if len(roots) < 2 or not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        if Path(raw_path).expanduser().is_absolute():
            return None
        canonical = storage.canonical_relpath(raw_path)
    except (CleanroomException, OSError, RuntimeError, ValueError):
        return None
    if entries is None:
        entries = _local_state().read().get("entries", {})
    known = {storage.root_identity_id(root) for root in roots}
    owners: set[str] = set()
    for entry in entries.values():
        recorded = entry.get("canonical_relpath")
        if not recorded:
            try:
                recorded = storage.canonical_relpath(str(entry.get("display_path") or ""))
            except CleanroomException:
                continue
        if recorded != canonical:
            continue
        owner = storage.coerce_root_id(entry.get("_root_identity") or entry.get("root_id"))
        if owner in known:
            owners.add(owner)
    return next(iter(owners)) if len(owners) == 1 else None


def _resolve_local_path(
    raw_path: str,
    root_id: str | None = None,
    entries: dict[str, Any] | None = None,
) -> Path:
    """本地文件请求解析：显式 root_id > 唯一索引记录的根 > 单根唯一根；否则 409。"""
    selected = storage.coerce_root_id(root_id)
    if selected is None:
        selected = _indexed_root_id(raw_path, entries)
    return storage.resolve_request_path(raw_path, root_id=selected)


def _is_relative_request(raw_path: str) -> bool:
    try:
        return bool(str(raw_path).strip()) and not Path(str(raw_path)).expanduser().is_absolute()
    except (OSError, RuntimeError, ValueError):
        return False


def file_info(raw_path: str, root_id: str | None = None) -> dict[str, Any]:
    """返回允许根目录内真实文件元数据；图片额外读真实宽高。"""
    path = _resolve_local_path(raw_path, root_id)
    if not path.is_file():
        raise CleanroomException(404, "FILE_NOT_FOUND", "目标文件不存在")
    stat = path.stat()
    payload: dict[str, Any] = {
        "display_path": storage.relative_display(path),
        "root_id": storage.location_identity(path)[0],
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified_at": int(stat.st_mtime),
        "extension": path.suffix.lower(),
        "content_type": mimetypes.guess_type(path.name)[0]
        or "application/octet-stream",
        "data_status": "ok",
    }
    if payload["extension"] in UNSUPPORTED_IMAGE_PROBE_SUFFIXES:
        payload["data_status"] = "partial"
        payload["data_gaps"] = ["image_probe_unsupported_format"]
    elif payload["extension"] in IMAGE_SUFFIXES:
        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError:
            payload["data_status"] = "partial"
            payload["data_gaps"] = ["image_probe_dependency_missing"]
        else:
            image_recognized = False
            try:
                with Image.open(path) as image:
                    image_recognized = True
                    width, height = image.size
                    image.verify()
                payload["width"], payload["height"] = width, height
            except PermissionError:
                payload["data_status"] = "partial"
                payload["data_gaps"] = ["image_probe_permission_denied"]
            except UnidentifiedImageError:
                payload["data_status"] = "partial"
                payload["data_gaps"] = ["image_probe_corrupt"]
            except OSError as exc:
                payload["data_status"] = "partial"
                payload["data_gaps"] = [
                    "image_probe_corrupt"
                    if image_recognized and exc.errno is None
                    else "image_probe_unreadable"
                ]
            except Exception:
                payload["data_status"] = "partial"
                payload["data_gaps"] = ["image_probe_internal_error"]
    return payload


def reveal_file(raw_path: str, root_id: str | None = None) -> dict[str, Any]:
    """只解析并确认路径在允许根目录内，不启动任何外部进程。"""
    path = _resolve_local_path(raw_path, root_id)
    return {
        "display_path": storage.relative_display(path),
        "root_id": storage.location_identity(path)[0],
        "exists": path.exists(),
        "revealed": False,
        "reason": "未启动外部文件管理器；仅完成路径准入校验",
        "data_status": "ok",
    }


def save_upload(
    raw_path: str, content_b64: str, root_id: str | None = None
) -> dict[str, Any]:
    """把 base64 内容原子写入允许根目录；已存在路径要求显式覆盖。"""
    path = _resolve_local_path(raw_path, root_id)
    if path.exists() and path.is_dir():
        raise CleanroomException(400, "INVALID_PATH", "目标路径是目录")
    payload = _decode_upload_payload(content_b64)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise CleanroomException(
            409, "PATH_CONFLICT", "目标文件已存在；显式覆盖必须走版本化更新接口"
        )
    partial = path.parent / (path.stem + ".part" + path.suffix)
    try:
        with partial.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(partial, path)
    except OSError:
        raise CleanroomException(503, "FILE_WRITE_FAILED", "目标文件写入失败") from None
    finally:
        partial.unlink(missing_ok=True)
    return {
        "display_path": storage.relative_display(path),
        "root_id": storage.location_identity(path)[0],
        "size_bytes": len(payload),
        "written": True,
    }


def _decode_upload_payload(content_b64: str) -> bytes:
    try:
        payload = base64.b64decode(content_b64, validate=True)
    except Exception:
        raise CleanroomException(400, "INVALID_CONTENT", "内容不是合法 base64")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise CleanroomException(400, "CONTENT_TOO_LARGE", "上传内容超过上限")
    return payload


def _write_upload_stage(
    target: Path, payload: bytes | None = None, source=None
) -> Path:
    """在目标目录写完并同步临时文件，供上传提交或回滚时原子替换。"""
    descriptor, raw_stage = tempfile.mkstemp(
        prefix=".gw-upload-pending-", dir=str(target.parent)
    )
    stage = Path(raw_stage)
    try:
        with os.fdopen(descriptor, "wb") as output:
            if source is not None:
                source.seek(0)
                shutil.copyfileobj(source, output)
            else:
                output.write(payload or b"")
            output.flush()
            os.fsync(output.fileno())
        return stage
    except Exception:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upload_and_index_local_asset(
    raw_path: str, content_b64: str, kind: str = "auto", root_id: str | None = None
) -> dict[str, Any]:
    """原子协调本地素材上传、索引预检与索引提交，失败时补偿文件内容。"""
    payload = _decode_upload_payload(content_b64)
    with _LOCAL_ASSET_DOMAIN_LOCK:
        path = _resolve_local_path(raw_path, root_id)
        if path.exists() and path.is_dir():
            raise CleanroomException(400, "INVALID_PATH", "目标路径是目录")

        display_path = storage.relative_display(path)
        root_identity = _local_root_identity(path)
        current_entries = _local_state().read().get("entries", {})
        # 必须在建目录、暂存文件或覆盖目标之前检测跨根身份冲突。
        _local_index_entry_for_root(current_entries, display_path, root_identity)

        if path.exists() and not path.is_file():
            raise CleanroomException(400, "INVALID_PATH", "目标路径不是普通文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.is_file()
        stage: Path | None = None
        restore_stage: Path | None = None
        target_replaced = False

        with tempfile.SpooledTemporaryFile(
            max_size=4 * 1024 * 1024, mode="w+b"
        ) as backup:
            if existed:
                with path.open("rb") as original:
                    shutil.copyfileobj(original, backup)

            try:
                stage = _write_upload_stage(path, payload)
                os.replace(stage, path)
                stage = None
                target_replaced = True
                result = index_local_files([str(path)], kind)
                if result.get("count") != 1:
                    raise CleanroomException(
                        500,
                        "LOCAL_ASSET_INDEX_UPDATE_FAILED",
                        "上传文件未能登记到本地素材索引",
                    )
            except Exception as exc:
                if target_replaced:
                    try:
                        if existed:
                            restore_stage = _write_upload_stage(path, source=backup)
                            os.replace(restore_stage, path)
                            restore_stage = None
                        else:
                            path.unlink(missing_ok=True)
                    except Exception as rollback_error:
                        raise CleanroomException(
                            500,
                            "LOCAL_ASSET_UPLOAD_PARTIAL_FAILURE",
                            "索引提交失败且上传文件补偿未完成",
                        ) from rollback_error
                if isinstance(exc, CleanroomException):
                    raise
                raise CleanroomException(
                    500,
                    "LOCAL_ASSET_INDEX_UPDATE_FAILED",
                    "索引提交失败，已补偿上传文件",
                ) from exc
            finally:
                for temporary_path in (stage, restore_stage):
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except OSError:
                            pass

        return {
            "display_path": display_path,
            "size_bytes": len(payload),
            "written": True,
        }


def delete_local_files(
    raw_paths: list[str], root_id: str | None = None
) -> dict[str, Any]:
    with _LOCAL_ASSET_DOMAIN_LOCK:
        return _delete_local_files_locked(raw_paths, root_id)


def _delete_local_files_locked(
    raw_paths: list[str], root_id: str | None = None
) -> dict[str, Any]:
    """删除真实文件，并在路径隐藏前移除对应索引记录。"""
    deleted, skipped, partial_failures = [], [], []
    state = _local_state()
    for raw in raw_paths:
        path = _resolve_local_path(raw, root_id)
        if not path.is_file():
            skipped.append(storage.relative_display(path))
            continue

        display_path = storage.relative_display(path)
        root_identity = _local_root_identity(path)
        # 先校验现有索引归属，再暂存文件；多根同名路径冲突必须在任何磁盘副作用前失败。
        _local_index_entry_for_root(
            state.read().get("entries", {}), display_path, root_identity
        )
        tombstone = path.with_name(".gw-delete-pending-" + path.name)
        if tombstone.exists():
            partial_failures.append(
                {
                    "display_path": display_path,
                    "code": "DELETE_STAGING_PATH_EXISTS",
                    "index_updated": False,
                }
            )
            continue
        try:
            # 同目录原子改名暂存，便于索引写入失败时恢复原文件。
            path.replace(tombstone)
        except OSError:
            partial_failures.append(
                {
                    "display_path": display_path,
                    "code": "LOCAL_ASSET_DELETE_FAILED",
                    "index_updated": False,
                }
            )
            continue

        try:

            def remove_index_entry(raw_state: dict[str, Any]) -> bool:
                entries = raw_state.setdefault("entries", {})
                match = _local_index_entry_for_root(
                    entries, display_path, root_identity
                )
                if match is None:
                    return False
                del entries[match[0]]
                return True

            state.mutate(remove_index_entry)
        except Exception:
            if path.exists():
                restored = False
            else:
                try:
                    tombstone.replace(path)
                    restored = True
                except OSError:
                    restored = False
            if restored:
                partial_failures.append(
                    {
                        "display_path": display_path,
                        "code": "LOCAL_ASSET_INDEX_UPDATE_FAILED",
                        "index_updated": False,
                        "filesystem_state": "restored",
                    }
                )
            else:
                # 补偿也失败时，优先移除失效路径记录；如存储仍不可写则如实标记未闭环。
                index_removed = _remove_local_index_path_best_effort(
                    state, display_path, root_identity
                )
                partial_failures.append(
                    {
                        "display_path": display_path,
                        "code": "LOCAL_ASSET_DELETE_PARTIAL_FAILURE",
                        "index_updated": index_removed,
                        "filesystem_state": "staged",
                    }
                )
            continue

        try:
            tombstone.unlink()
            deleted.append(display_path)
        except OSError:
            # 原始展示路径已不存在且索引已移除，但暂存文件仍留在磁盘；不得谎报删除完成。
            partial_failures.append(
                {
                    "display_path": display_path,
                    "code": "LOCAL_ASSET_DELETE_PARTIAL_FAILURE",
                    "index_updated": True,
                    "filesystem_state": "staged",
                }
            )
    return {
        "deleted": deleted,
        "skipped": skipped,
        "partial_failures": partial_failures,
        "count": len(deleted),
    }


def move_local_file(
    source: str,
    target: str,
    source_root_id: str | None = None,
    target_root_id: str | None = None,
) -> dict[str, Any]:
    with _LOCAL_ASSET_DOMAIN_LOCK:
        return _move_local_file_locked(source, target, source_root_id, target_root_id)


def _move_local_file_locked(
    source: str,
    target: str,
    source_root_id: str | None = None,
    target_root_id: str | None = None,
) -> dict[str, Any]:
    """移动真实文件并同步索引；索引写入失败时补偿磁盘移动。

    相对目标未显式给出 target_root_id 时，按源文件所在根解析（同根移动），
    这是由源身份确定的，不是多根猜测。
    """
    src = _resolve_local_path(source, source_root_id)
    explicit_target_root = storage.coerce_root_id(target_root_id) or storage.coerce_root_id(
        source_root_id
    )
    if explicit_target_root is None and _is_relative_request(target):
        explicit_target_root = storage.location_identity(src)[0]
    dst = storage.resolve_request_path(target, root_id=explicit_target_root)
    if not src.is_file():
        raise CleanroomException(404, "FILE_NOT_FOUND", "源文件不存在")
    if dst.exists():
        raise CleanroomException(409, "TARGET_EXISTS", "目标路径已存在")
    source_key = storage.relative_display(src)
    target_key = storage.relative_display(dst)
    source_location = storage.location_identity(src)
    target_location = storage.location_identity(dst)
    source_root_identity = _local_root_identity(src)
    target_root_identity = _local_root_identity(dst)
    state = _local_state()
    before = state.read().get("entries", {})
    _local_index_entry_for_root(before, source_key, source_root_identity)
    if (
        source_location != target_location
        and _find_local_index_entry(before, target_key, target_root_identity) is not None
    ):
        raise CleanroomException(
            409, "LOCAL_ASSET_INDEX_TARGET_CONFLICT", "目标相对路径已有索引记录"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)

    try:

        def move_index_entry(raw: dict[str, Any]) -> dict[str, Any]:
            entries = raw.setdefault("entries", {})
            match = _local_index_entry_for_root(
                entries, source_key, source_root_identity
            )
            record_key = match[0] if match else None
            # 先在完整索引中确定性修复重复 ID，再弹出移动记录，避免被移动项漏出修复扫描。
            repairs = _repair_duplicate_local_asset_ids(raw)
            record = entries.pop(record_key, None) if record_key is not None else None
            if record is not None:
                record.update(
                    {
                        "name": dst.name,
                        "display_path": target_key,
                        "size_bytes": dst.stat().st_size,
                        "moved_at": storage.now_iso(),
                        "_root_identity": target_root_identity,
                        "root_id": target_root_identity,
                        "canonical_relpath": target_location[1],
                    }
                )
                entries[_local_entry_key(target_root_identity, target_key)] = record
            return {
                "asset_id": record.get("asset_id") if record else None,
                "id_repairs": repairs,
            }

        indexed = state.mutate(move_index_entry)
    except Exception as exc:
        if src.exists():
            restored = False
        else:
            try:
                dst.replace(src)
                restored = True
            except OSError:
                restored = False
        if restored:
            raise CleanroomException(
                500,
                "LOCAL_ASSET_INDEX_UPDATE_FAILED",
                "索引更新失败；文件已补偿回源路径",
            ) from exc

        # 磁盘反向补償失败時，先将索引尽力对齐到仍存在的目标路径。
        reconciled = _reconcile_local_index_move(
            state,
            source_key,
            target_key,
            dst,
            source_root_identity,
            target_root_identity,
        )
        if reconciled is not None:
            return {
                "from": source_key,
                "to": target_key,
                "moved": True,
                "asset_id": reconciled.get("asset_id"),
                "id_repairs": reconciled.get("id_repairs", []),
                "index_reconciled": True,
            }
        raise CleanroomException(
            500,
            "LOCAL_ASSET_MOVE_PARTIAL_FAILURE",
            "文件已移动，但索引更新与磁盘补偿均失败；请按返回路径重新索引",
            extra={
                "from": source_key,
                "to": target_key,
                "filesystem_state": "target",
                "index_consistent": False,
            },
            expose_extra_fields={"from", "to", "filesystem_state", "index_consistent"},
        ) from exc

    return {
        "from": source_key,
        "to": target_key,
        "moved": True,
        "asset_id": indexed.get("asset_id"),
        "id_repairs": indexed.get("id_repairs", []),
    }


# ---------------------------------------------------------------------------
# 本地素材索引与存储产物
# ---------------------------------------------------------------------------


def _local_state() -> storage.JsonState:
    return storage.JsonState(NS_LOCAL, lambda: {"entries": {}, "sequence": 0})


def _local_root_identity(path: Path) -> str:
    """返回允许根的不可逆身份摘要，不把根目录绝对路径暴露给索引接口。"""
    resolved = path.resolve()
    matches = []
    for root in storage.allowed_roots():
        canonical_root = root.resolve()
        try:
            resolved.relative_to(canonical_root)
        except ValueError:
            continue
        matches.append(canonical_root)
    if matches:
        canonical_root = max(matches, key=lambda root: len(root.parts))
        value = os.path.normcase(str(canonical_root))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    raise CleanroomException(
        403, "PATH_OUTSIDE_ALLOWED_ROOTS", "目标文件不在允许根目录内"
    )


def _local_entry_key(root_identity: str, display_path: str) -> str:
    return "%s:%s" % (root_identity, storage.canonical_relpath(display_path))


def _find_local_index_entry(
    entries: dict[str, Any], display_path: str, root_identity: str | None = None
):
    canonical = storage.canonical_relpath(display_path)
    matches = [
        (key, entry)
        for key, entry in entries.items()
        if key == display_path
        or entry.get("display_path") == display_path
        or entry.get("canonical_relpath") == canonical
    ]
    if root_identity is not None:
        owned = [
            match
            for match in matches
            if match[1].get("_root_identity") == root_identity
        ]
        legacy = [match for match in matches if not match[1].get("_root_identity")]
        matches = owned or legacy
    if len(matches) > 1:
        raise CleanroomException(
            409,
            "LOCAL_ASSET_PATH_AMBIGUOUS",
            "相对路径对应多条索引记录，拒绝猜测文件身份",
        )
    return matches[0] if matches else None


def _local_index_entry_for_root(
    entries: dict[str, Any], display_path: str, root_identity: str
):
    match = _find_local_index_entry(entries, display_path, root_identity)
    if match is None:
        return None
    _, entry = match
    stored_identity = entry.get("_root_identity")
    if not stored_identity:
        if len(storage.allowed_roots()) != 1:
            raise CleanroomException(
                409,
                "LOCAL_ASSET_PATH_AMBIGUOUS",
                "历史索引缺少允许根身份，当前多根配置下拒绝读写该相对路径",
            )
        entry["_root_identity"] = root_identity
    entry["root_id"] = root_identity
    entry["canonical_relpath"] = storage.canonical_relpath(display_path)
    return match


def _public_local_asset_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """移除仅供索引内部判定使用的根身份字段。"""
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _local_sequence(raw: dict[str, Any]) -> int:
    """把持久化高水位与当前记录合并，避免删除末尾条目后复用 asset_id。"""
    try:
        sequence = max(0, int(raw.get("sequence", 0) or 0))
    except (TypeError, ValueError):
        sequence = 0
    for entry in raw.get("entries", {}).values():
        asset_id = str(entry.get("asset_id") or "")
        tail = asset_id.removeprefix("local_")
        if tail.isdigit():
            sequence = max(sequence, int(tail))
    raw["sequence"] = sequence
    return sequence


def _next_local_asset_id(raw: dict[str, Any]) -> str:
    sequence = _local_sequence(raw) + 1
    raw["sequence"] = sequence
    return "local_%04d" % sequence


def _duplicate_local_asset_ids(raw: dict[str, Any]) -> list[str]:
    counts: dict[str, int] = {}
    for entry in raw.get("entries", {}).values():
        asset_id = str(entry.get("asset_id") or "")
        if asset_id:
            counts[asset_id] = counts.get(asset_id, 0) + 1
    return sorted(asset_id for asset_id, count in counts.items() if count > 1)


def _repair_duplicate_local_asset_ids(raw: dict[str, Any]) -> list[dict[str, str]]:
    """按展示路径稳定修复旧冲突，并返回完整映射，绝不让歧义 ID 指向任一对象。"""
    duplicates = set(_duplicate_local_asset_ids(raw))
    if not duplicates:
        _local_sequence(raw)
        return []
    repaired: list[dict[str, str]] = []
    entries = raw.setdefault("entries", {})
    for key, entry in sorted(
        entries.items(),
        key=lambda pair: (str(pair[1].get("display_path") or pair[0]), pair[0]),
    ):
        previous = str(entry.get("asset_id") or "")
        if previous not in duplicates:
            continue
        replacement = _next_local_asset_id(raw)
        entry["asset_id"] = replacement
        repaired.append(
            {
                "display_path": str(entry.get("display_path") or key),
                "previous_asset_id": previous,
                "asset_id": replacement,
            }
        )
    return repaired


def _remove_local_index_path_best_effort(
    state: storage.JsonState, display_path: str, root_identity: str
) -> bool:
    try:
        raw = state.read()
        entries = raw.setdefault("entries", {})
        match = _local_index_entry_for_root(entries, display_path, root_identity)
        if match is None:
            return False
        del entries[match[0]]
        state.write(raw)
        return True
    except Exception:
        return False


def _reconcile_local_index_move(
    state: storage.JsonState,
    source_key: str,
    target_key: str,
    target_path: Path,
    source_root_identity: str,
    target_root_identity: str,
) -> dict[str, Any] | None:
    try:
        raw = state.read()
        entries = raw.setdefault("entries", {})
        match = _local_index_entry_for_root(entries, source_key, source_root_identity)
        record_key = match[0] if match else None
        repairs = _repair_duplicate_local_asset_ids(raw)
        record = entries.pop(record_key, None) if record_key is not None else None
        if record is not None:
            record.update(
                {
                    "name": target_path.name,
                    "display_path": target_key,
                    "size_bytes": target_path.stat().st_size,
                    "moved_at": storage.now_iso(),
                    "_root_identity": target_root_identity,
                    "root_id": target_root_identity,
                    "canonical_relpath": storage.canonical_relpath(target_key),
                }
            )
            entries[_local_entry_key(target_root_identity, target_key)] = record
        state.write(raw)
        return {
            "asset_id": record.get("asset_id") if record else None,
            "id_repairs": repairs,
        }
    except Exception:
        return None


def _local_dir() -> Path:
    return storage.data_root() / "local_assets"


def _safe_name(name: str, *, what: str = "名称") -> str:
    clean = "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in "-_ ")
    if not clean:
        raise CleanroomException(400, "INVALID_REQUEST", "%s不合法" % what)
    return clean


def _local_asset_cursor_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    canonical_relpath = str(
        entry.get("canonical_relpath")
        or storage.canonical_relpath(str(entry.get("display_path") or ""))
    )
    return (
        canonical_relpath.casefold(),
        canonical_relpath,
        str(entry.get("_root_identity") or ""),
        str(entry.get("asset_id") or entry.get("id") or ""),
    )


def _encode_local_asset_cursor(kind: str | None, key: tuple[str, str, str, str]) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "last": list(key)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_local_asset_cursor(cursor: str, kind: str | None) -> tuple[str, str, str, str]:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        )
        last = payload["last"]
        if (
            type(payload.get("v")) is not int
            or payload.get("v") != 1
            or payload.get("kind") != kind
            or not isinstance(last, list)
            or len(last) != 4
            or any(not isinstance(value, str) for value in last)
        ):
            raise ValueError
        return tuple(last)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        raise CleanroomException(400, "INVALID_CURSOR", "本地素材分页游标无效") from None


def list_local_assets(
    kind: str | None = None,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise CleanroomException(400, "INVALID_LIMIT", "limit 必须为 1 到 500 的整数")
    raw = _local_state().read()
    entries = list(raw.get("entries", {}).values())
    if kind:
        entries = [entry for entry in entries if entry.get("kind") == kind]
    entries.sort(key=_local_asset_cursor_key)
    total = len(entries)
    if cursor:
        after = _decode_local_asset_cursor(cursor, kind)
        entries = [entry for entry in entries if _local_asset_cursor_key(entry) > after]
    page = entries[:limit]
    has_more = len(entries) > limit
    ambiguous = _duplicate_local_asset_ids(raw)
    return {
        "items": [_public_local_asset_entry(entry) for entry in page],
        "count": total,
        "total": total,
        "limit": limit,
        "has_more": has_more,
        "next_cursor": (
            _encode_local_asset_cursor(kind, _local_asset_cursor_key(page[-1]))
            if has_more and page
            else None
        ),
        "data_status": "ok" if total else "empty",
        "data_gaps": (
            [{"code": "LOCAL_ASSET_ID_AMBIGUOUS", "asset_ids": ambiguous}]
            if ambiguous
            else []
        ),
    }


def index_local_files(
    raw_paths: list[str], kind: str = "auto", root_id: str | None = None
) -> dict[str, Any]:
    with _LOCAL_ASSET_DOMAIN_LOCK:
        return _index_local_files_locked(raw_paths, kind, root_id)


def _index_local_files_locked(
    raw_paths: list[str], kind: str = "auto", root_id: str | None = None
) -> dict[str, Any]:
    """把允许根目录内真实文件登记到本地素材索引。"""

    def mutate(raw: dict[str, Any]) -> Any:
        repaired = _repair_duplicate_local_asset_ids(raw)
        added = []
        for raw_path in raw_paths:
            path = _resolve_local_path(raw_path, root_id, raw["entries"])
            if not path.is_file():
                continue
            key = storage.relative_display(path)
            root_identity = _local_root_identity(path)
            match = _local_index_entry_for_root(raw["entries"], key, root_identity)
            entry_key, entry = (
                match
                if match
                else (
                    _local_entry_key(root_identity, key),
                    {"asset_id": _next_local_asset_id(raw)},
                )
            )
            entry.update(
                {
                    "name": path.name,
                    "display_path": key,
                    "canonical_relpath": storage.location_identity(path)[1],
                    "root_id": root_identity,
                    "size_bytes": path.stat().st_size,
                    "kind": kind if kind != "auto" else classify_name(path.name),
                    "indexed_at": storage.now_iso(),
                    "_root_identity": root_identity,
                }
            )
            raw["entries"][entry_key] = entry
            added.append(_public_local_asset_entry(entry))
        return {"items": added, "count": len(added), "id_repairs": repaired}

    return _local_state().mutate(mutate)


def update_local_entry(asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        matches = [
            entry
            for entry in raw.get("entries", {}).values()
            if entry.get("asset_id") == asset_id
        ]
        if not matches:
            raise CleanroomException(404, "LOCAL_ASSET_NOT_FOUND", "本地素材不存在")
        if len(matches) > 1:
            raise CleanroomException(
                409,
                "LOCAL_ASSET_ID_AMBIGUOUS",
                "该素材标识对应多条历史记录，请先重新索引并读取新标识",
            )
        entry = matches[0]
        if payload.get("name"):
            entry["name"] = str(payload["name"]).strip()
        if payload.get("kind"):
            entry["kind"] = str(payload["kind"])
        entry["updated_at"] = storage.now_iso()
        return _public_local_asset_entry(entry)

    return _local_state().mutate(mutate)


def classify_local_entries(asset_ids: list[str]) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        results = []
        for entry in raw["entries"].values():
            if asset_ids and entry.get("asset_id") not in asset_ids:
                continue
            label = classify_name(str(entry.get("name") or ""))
            entry["kind"] = label
            entry["classified_at"] = storage.now_iso()
            results.append({"asset_id": entry.get("asset_id"), "kind": label})
        return {"results": results, "count": len(results)}

    return _local_state().mutate(mutate)


def caption_local_entries(asset_ids: list[str]) -> dict[str, Any]:
    """生成基于真实文件名的确定性说明（不做内容语义猜测）。"""

    def mutate(raw: dict[str, Any]) -> Any:
        results = []
        for entry in raw["entries"].values():
            if asset_ids and entry.get("asset_id") not in asset_ids:
                continue
            name = str(entry.get("name") or "")
            caption = "%s（%s，%s 字节）" % (
                name,
                classify_name(name),
                entry.get("size_bytes", 0),
            )
            entry["caption"] = caption
            results.append({"asset_id": entry.get("asset_id"), "caption": caption})
        return {"results": results, "count": len(results)}

    return _local_state().mutate(mutate)


def save_local_caption(asset_id: str, caption: str) -> dict[str, Any]:
    def mutate(raw: dict[str, Any]) -> Any:
        for entry in raw["entries"].values():
            if entry.get("asset_id") == asset_id:
                entry["caption"] = caption
                entry["updated_at"] = storage.now_iso()
                return {"asset_id": asset_id, "caption": caption, "saved": True}
        raise CleanroomException(404, "LOCAL_ASSET_NOT_FOUND", "本地素材不存在")

    return _local_state().mutate(mutate)


def create_local_folder(name: str) -> dict[str, Any]:
    clean = _safe_name(name, what="文件夹名称")
    folder = _local_dir() / clean
    folder.mkdir(parents=True, exist_ok=True)
    return {"name": clean, "display_path": "local_assets/" + clean, "created": True}


def _rename_source_local_folder(
    name: str, new_name: str, root_path: str
) -> dict[str, Any]:
    """在指定素材来源根内重命名目录，并同步其全部索引后代。"""
    if not Path(root_path).expanduser().is_absolute():
        raise CleanroomException(400, "INVALID_PATH", "素材来源目录必须是绝对路径")
    source_root = storage.resolve_request_path(root_path)
    if not source_root.is_dir():
        raise CleanroomException(404, "FOLDER_NOT_FOUND", "素材来源目录不存在")

    requested = str(name or "").strip().replace("\\", "/")
    canonical_requested = storage.canonical_relpath(requested)
    clean = _safe_name(new_name, what="新文件夹名称")
    root_id, _ = storage.location_identity(source_root)
    source_prefix = storage.relative_display(source_root).replace("\\", "/")
    if source_prefix == ".":
        source_prefix = ""
    canonical_prefix = (
        storage.canonical_relpath(source_prefix) if source_prefix else ""
    )

    # 目录树路径通常相对允许根；调用方也可传相对素材来源根的路径。
    if canonical_prefix and (
        canonical_requested == canonical_prefix
        or canonical_requested.startswith(canonical_prefix + "/")
    ):
        src = storage.resolve_request_path(requested, root_id=root_id)
    else:
        relative_parts = requested.split("/")
        src = storage.resolve_request_path(
            str(source_root.joinpath(*relative_parts)), root_id=root_id
        )

    try:
        src.relative_to(source_root)
    except ValueError:
        raise CleanroomException(400, "INVALID_PATH", "目录必须位于素材来源内") from None
    if src == source_root or not src.is_dir():
        raise CleanroomException(404, "FOLDER_NOT_FOUND", "文件夹不存在")

    dst = src.with_name(clean)
    if dst.exists():
        raise CleanroomException(409, "TARGET_EXISTS", "目标文件夹已存在")
    source_key = storage.relative_display(src)
    target_key = storage.relative_display(dst)
    source_canonical = storage.canonical_relpath(source_key)
    target_canonical = storage.canonical_relpath(target_key)
    source_root_identity = _local_root_identity(src)
    state = _local_state()
    known_root_ids = {
        storage.root_identity_id(root) for root in storage.allowed_roots()
    }
    root_identity_cache: dict[str, str | None] = {
        root_id: root_id,
        source_root_identity: root_id,
    }

    def belongs_to_root(entry: dict[str, Any]) -> bool:
        recorded_root = entry.get("_root_identity") or entry.get("root_id")
        if recorded_root is None or not str(recorded_root).strip():
            return len(known_root_ids) == 1 and root_id in known_root_ids
        value = str(recorded_root).strip()
        if value not in root_identity_cache:
            root_identity_cache[value] = storage.coerce_root_id(value)
        entry_root_id = root_identity_cache[value]
        return entry_root_id == root_id

    def canonical_entry_path(entry: dict[str, Any]) -> str | None:
        recorded_path = entry.get("canonical_relpath") or entry.get("display_path")
        if not recorded_path:
            return None
        try:
            return storage.canonical_relpath(str(recorded_path))
        except CleanroomException:
            return None

    def belongs_to_source(entry: dict[str, Any]) -> bool:
        if not belongs_to_root(entry):
            return False
        canonical_path = canonical_entry_path(entry)
        return bool(canonical_path and canonical_path.startswith(source_canonical + "/"))

    before = state.read().get("entries", {})
    moving_keys = {key for key, entry in before.items() if belongs_to_source(entry)}
    target_prefix = target_canonical + "/"
    for key, entry in before.items():
        if key in moving_keys or not belongs_to_root(entry):
            continue
        canonical_path = canonical_entry_path(entry)
        if canonical_path is None:
            continue
        if canonical_path == target_canonical or canonical_path.startswith(target_prefix):
            raise CleanroomException(
                409, "LOCAL_ASSET_INDEX_TARGET_CONFLICT", "目标路径已有索引记录"
            )

    src.replace(dst)
    try:

        def move_folder_index(raw: dict[str, Any]) -> int:
            entries = raw.setdefault("entries", {})
            current_moving = {
                key: entry
                for key, entry in entries.items()
                if belongs_to_source(entry)
            }
            moving_source_keys = set(current_moving)
            for key, entry in entries.items():
                if key in moving_source_keys or not belongs_to_root(entry):
                    continue
                canonical_path = canonical_entry_path(entry)
                if canonical_path and (
                    canonical_path == target_canonical
                    or canonical_path.startswith(target_prefix)
                ):
                    raise CleanroomException(
                        409, "LOCAL_ASSET_INDEX_TARGET_CONFLICT", "目标路径已有索引记录"
                    )
            updated: list[tuple[str, str, dict[str, Any]]] = []
            for key, entry in current_moving.items():
                old_path = str(entry.get("display_path") or "").replace("\\", "/")
                suffix = old_path[len(source_key) :]
                new_path = target_key + suffix
                new_key = _local_entry_key(source_root_identity, new_path)
                if new_key in entries and new_key not in moving_source_keys:
                    raise CleanroomException(
                        409, "LOCAL_ASSET_INDEX_TARGET_CONFLICT", "目标路径已有索引记录"
                    )
                updated.append((key, new_key, entry))

            for old_key, _new_key, _entry in updated:
                entries.pop(old_key, None)
            for _old_key, new_key, entry in updated:
                old_path = str(entry.get("display_path") or "").replace("\\", "/")
                suffix = old_path[len(source_key) :]
                new_path = target_key + suffix
                entry.update(
                    {
                        "display_path": new_path,
                        "canonical_relpath": storage.canonical_relpath(new_path),
                        "_root_identity": source_root_identity,
                        "root_id": source_root_identity,
                        "moved_at": storage.now_iso(),
                    }
                )
                entries[new_key] = entry
            return len(updated)

        moved_count = state.mutate(move_folder_index)
    except Exception as exc:
        try:
            dst.replace(src)
        except OSError:
            raise CleanroomException(
                500,
                "LOCAL_ASSET_FOLDER_PARTIAL_FAILURE",
                "文件夹已移动，但索引更新与磁盘回滚均失败",
                extra={"from": source_key, "to": target_key},
                expose_extra_fields={"from", "to"},
            ) from exc
        if isinstance(exc, CleanroomException):
            raise
        raise CleanroomException(
            500,
            "LOCAL_ASSET_INDEX_UPDATE_FAILED",
            "索引更新失败；文件夹已回滚到原路径",
        ) from exc

    return {
        "from": source_key,
        "to": target_key,
        "renamed": True,
        "moved_count": moved_count,
    }


def rename_local_folder(
    name: str, new_name: str, *, root_path: str | None = None
) -> dict[str, Any]:
    if root_path:
        with _LOCAL_ASSET_DOMAIN_LOCK:
            return _rename_source_local_folder(name, new_name, root_path)
    src = _local_dir() / _safe_name(name, what="文件夹名称")
    clean = _safe_name(new_name, what="新文件夹名称")
    if not src.is_dir():
        raise CleanroomException(404, "FOLDER_NOT_FOUND", "文件夹不存在")
    dst = _local_dir() / clean
    if dst.exists():
        raise CleanroomException(409, "TARGET_EXISTS", "目标文件夹已存在")
    src.replace(dst)
    return {
        "from": "local_assets/" + src.name,
        "to": "local_assets/" + dst.name,
        "renamed": True,
    }


def storage_files() -> dict[str, Any]:
    """只列服务端产物，不枚举根目录JSON/数据库等内部状态。"""
    items = []
    for relative, _path in storage.iter_output_paths():
        try:
            info = storage.resolve_output_path(relative).stat()
        except (CleanroomException, OSError):
            continue
        items.append(
            {
                "name": relative,
                "size_bytes": info.st_size,
                "modified_at": int(info.st_mtime),
            }
        )
    items.sort(key=lambda item: item["name"])
    return {
        "items": items,
        "count": len(items),
        "allowed_roots_configured": bool(storage.allowed_roots()),
        "data_status": "ok" if items else "empty",
    }


def delete_storage_files(names: list[str]) -> dict[str, Any]:
    """完整预检后逐个删除准入产物；磁盘失败如实报告，不承诺文件系统事务。"""
    for name in names:
        storage.resolve_output_path(name, must_exist=False)
    deleted, skipped, failed = [], [], []
    for name in dict.fromkeys(names):
        try:
            candidate = storage.resolve_output_path(name, must_exist=False)
            if not candidate.exists():
                skipped.append(name)
                continue
            candidate.unlink()
            deleted.append(name)
        except CleanroomException:
            failed.append({"name": name, "code": "OUTPUT_PATH_CHANGED"})
        except OSError:
            failed.append({"name": name, "code": "OUTPUT_DELETE_FAILED"})
    return {
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "count": len(deleted),
        "data_status": "partial" if failed else "ok",
    }


# ---------------------------------------------------------------------------
# 头像登记（本地登记，不调用外部平台）
# ---------------------------------------------------------------------------


def _avatar_state() -> storage.JsonState:
    return storage.JsonState(NS_AVATAR, lambda: {"items": {}})


def avatar_status(asset_id: str) -> dict[str, Any]:
    entry = _avatar_state().read()["items"].get(asset_id)
    if not entry:
        return {
            "asset_id": asset_id,
            "registered": False,
            "avatar_path": None,
            "data_status": "ok",
            "data_gaps": ["avatar_not_registered"],
        }
    return {
        "asset_id": asset_id,
        "registered": True,
        "avatar_path": entry["display_path"],
        "registered_at": entry["registered_at"],
        "data_status": "ok",
    }


def register_avatar(
    asset_id: str, avatar_path: str, root_id: str | None = None
) -> dict[str, Any]:
    path = _resolve_local_path(avatar_path, root_id)
    if not path.is_file():
        raise CleanroomException(404, "FILE_NOT_FOUND", "头像文件不存在")
    display = storage.relative_display(path)
    entry = {
        "asset_id": asset_id,
        "display_path": display,
        "registered_at": storage.now_iso(),
    }

    def mutate(raw: dict[str, Any]) -> Any:
        raw["items"][asset_id] = entry
        return dict(entry)

    _avatar_state().mutate(mutate)
    return {
        "asset_id": asset_id,
        "registered": True,
        "avatar_path": display,
        "registered_at": entry["registered_at"],
        "data_status": "ok",
    }
