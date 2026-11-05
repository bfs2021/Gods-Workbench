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

"""收集箱（QIN-547）：统一的默认收集目录。

边界与不变量：

- 收集箱本身是**真实磁盘文件夹**，默认 ``data_root()/inbox``，位置可在设置里改；
- 改位置时可选「迁移已有文件」；同一位置重复提交是幂等的（不产生重复数据）；
- 列表同时呈现三类来源：收集箱本地文件、遗留 ``registry_uploads/collect_inbox`` 收集文件、
  托管资产迁移后的 URL 条目；
- 迁移托管资产按 ``asset_id`` 记账，可重复执行；外部 URL 保留为 URL 条目，
  仅 ``file://`` 或绝对路径且真实存在的本地文件才会被移动进收集箱；
- 迁移不删除任何托管资产 JSON 记录，也不改 ``collector.store_content`` 的受管落盘路径。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from gw.asset_library.repository import classify_name
from gw.core import storage
from gw.core.errors import CleanroomException

NS_INBOX = "asset_inbox"
INBOX_DIRNAME = "inbox"
#: 单个收集文件上限，与本地素材上传保持一致。
MAX_INBOX_UPLOAD_BYTES = 32 * 1024 * 1024
#: 遗留浏览器收集目录（相对 data_root）；只读展示，不搬迁，避免破坏受管回读契约。
LEGACY_COLLECT_PARTS = ("registry_uploads", "collect_inbox")

_lock = threading.RLock()


def _default_raw() -> dict[str, Any]:
    return {
        "version": 1,
        "path": None,
        "last_viewed_at": None,
        "last_viewed_epoch": None,
        "migrated": {},
    }


def _state() -> storage.JsonState:
    return storage.JsonState(NS_INBOX, _default_raw)


# ---------------------------------------------------------------------------
# 位置解析
# ---------------------------------------------------------------------------


def default_inbox_root() -> Path:
    """默认收集箱位置：数据根目录下的真实文件夹。"""
    return storage.data_root() / INBOX_DIRNAME


def inbox_root(raw: dict[str, Any] | None = None) -> Path:
    """当前生效的收集箱目录（未配置时回落默认位置）。"""
    payload = raw if raw is not None else _state().read()
    configured = str(payload.get("path") or "").strip()
    return Path(configured) if configured else default_inbox_root()


def _resolve_configured_dir(raw_path: Any) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise CleanroomException(400, "INVALID_PATH", "收集箱位置不能为空")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise CleanroomException(400, "INVALID_PATH", "收集箱位置必须是绝对路径")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise CleanroomException(400, "INVALID_PATH", "收集箱位置无法解析") from None
    if resolved.exists() and not resolved.is_dir():
        raise CleanroomException(400, "INVALID_PATH", "收集箱位置必须是文件夹")
    return resolved


def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CleanroomException(
            503, "INBOX_DIRECTORY_UNAVAILABLE", "收集箱目录不可写"
        ) from None
    return path


def _ensure_dir_tolerant(path: Path) -> None:
    """只读接口也保证收集箱是真实文件夹；不可写时不静默失败，由写入路径报错。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# 列表与角标
# ---------------------------------------------------------------------------


def _timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iter_files(root: Path) -> list[Path]:
    """递归枚举真实普通文件；跳过软链接，绝不跟随到收集箱之外。"""
    if not root.is_dir():
        return []
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
            except OSError:
                continue
    return sorted(files, key=lambda path: str(path).casefold())


def _local_items(root: Path, source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    prefix = Path(*LEGACY_COLLECT_PARTS).as_posix()
    for path in _iter_files(root):
        try:
            relative = path.relative_to(root).as_posix()
            stat = path.stat()
        except (OSError, ValueError):
            continue
        items.append(
            {
                "id": f"{source}:{relative}",
                "source": source,
                "name": path.name,
                "file": relative if source == "inbox" else f"{prefix}/{relative}",
                "kind": classify_name(path.name),
                "size_bytes": stat.st_size,
                "modified_at": int(stat.st_mtime),
                "_epoch": stat.st_mtime,
                "url": None,
                "asset_id": None,
            }
        )
    return items


def _url_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """托管资产迁移后保留在收集箱里的 URL 条目（本地文件已迁走的记录不再重复展示）。"""
    items: list[dict[str, Any]] = []
    for asset_id, record in sorted(raw.get("migrated", {}).items()):
        if not isinstance(record, dict) or record.get("file"):
            continue
        items.append(
            {
                "id": f"asset:{asset_id}",
                "source": "asset_library",
                "name": str(record.get("name") or asset_id),
                "file": None,
                "kind": str(record.get("kind") or "url"),
                "size_bytes": 0,
                "modified_at": int(_timestamp(record.get("migrated_at"))),
                "_epoch": _timestamp(record.get("migrated_at")),
                "url": record.get("url"),
                "asset_id": asset_id,
            }
        )
    return items


def _all_items(raw: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = raw if raw is not None else _state().read()
    root = inbox_root(payload)
    _ensure_dir_tolerant(root)
    legacy_root = storage.data_root().joinpath(*LEGACY_COLLECT_PARTS)
    items = _local_items(root, "inbox")
    if legacy_root.resolve(strict=False) != root.resolve(strict=False):
        items.extend(_local_items(legacy_root, "collect_inbox"))
    items.extend(_url_items(payload))
    items.sort(key=lambda item: (-int(item["modified_at"]), str(item["name"]).casefold()))
    return items


def _unread_count(items: list[dict[str, Any]], raw: dict[str, Any]) -> int:
    viewed = float(raw.get("last_viewed_epoch") or 0)
    if viewed <= 0:
        return len(items)
    return sum(1 for item in items if float(item.get("_epoch") or 0) > viewed)


def settings_snapshot(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = raw if raw is not None else _state().read()
    root = inbox_root(payload)
    items = _all_items(payload)
    return {
        "path": str(root),
        "default_path": str(default_inbox_root()),
        "is_default": not str(payload.get("path") or "").strip(),
        "exists": root.is_dir(),
        "count": len(items),
        "unread_count": _unread_count(items, payload),
        "last_viewed_at": payload.get("last_viewed_at"),
        "last_viewed_epoch": payload.get("last_viewed_epoch"),
        "migrated_asset_count": len(payload.get("migrated", {})),
        "data_status": "ok" if items else "empty",
    }


def list_items(
    limit: int = 100,
    offset: int = 0,
    source: str | None = None,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise CleanroomException(400, "INVALID_LIMIT", "limit 必须为 1 到 500 的整数")
    if type(offset) is not int or offset < 0:
        raise CleanroomException(400, "INVALID_OFFSET", "offset 必须为非负整数")
    raw = _state().read()
    items = _all_items(raw)
    total = len(items)
    unread = _unread_count(items, raw)
    if source:
        items = [item for item in items if item["source"] == source]
    page = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in items[offset : offset + limit]
    ]
    return {
        "items": page,
        "count": len(items),
        "total": total,
        "unread_count": unread,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(items),
        "source": source,
        "settings": settings_snapshot(raw),
        "data_status": "ok" if items else "empty",
    }


def mark_viewed() -> dict[str, Any]:
    """记录「已看过收集箱」时间点，角标按该时间点之后的新素材计算。"""

    def mutate(raw: dict[str, Any]) -> dict[str, Any]:
        raw["last_viewed_at"] = storage.now_iso()
        raw["last_viewed_epoch"] = time.time()
        return {
            "last_viewed_at": raw["last_viewed_at"],
            "last_viewed_epoch": raw["last_viewed_epoch"],
            "unread_count": 0,
        }

    return _state().mutate(mutate)


# ---------------------------------------------------------------------------
# 改位置（可选迁移已有文件）
# ---------------------------------------------------------------------------


def _move_into(target_root: Path, relative: str, source: Path) -> str:
    target = target_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_file() and _same_content(source, target):
            return "skipped"
        return "conflict"
    shutil.move(str(source), str(target))
    return "moved"


def _same_content(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return _digest(left) == _digest(right)
    except OSError:
        return False


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _prune_empty_dirs(root: Path) -> None:
    for path in sorted(_iter_files_shallow(root), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def _iter_files_shallow(root: Path) -> list[Path]:
    """收集箱内的全部子目录（由深到浅清理空目录）。"""
    if not root.is_dir():
        return []
    directories: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                    directories.append(Path(entry.path))
                    stack.append(Path(entry.path))
            except OSError:
                continue
    return directories


def update_path(raw_path: Any, migrate: bool = True) -> dict[str, Any]:
    """修改收集箱位置；同一位置重复提交幂等，不产生重复数据。"""
    with _lock:
        raw = _state().read()
        old_root = inbox_root(raw)
        new_root = _resolve_configured_dir(raw_path)
        if new_root == old_root.resolve(strict=False):
            return {
                **settings_snapshot(raw),
                "unchanged": True,
                "migrated": [],
                "skipped": [],
                "conflicts": [],
                "failed": [],
                "migrate_requested": bool(migrate),
            }
        if _is_within(new_root, old_root) or _is_within(old_root, new_root):
            raise CleanroomException(
                400, "INBOX_PATH_NESTED", "新位置不能与当前收集箱互为上下级目录"
            )
        _ensure_dir(new_root)
        moved: list[str] = []
        skipped: list[str] = []
        conflicts: list[str] = []
        failed: list[dict[str, str]] = []
        if migrate and old_root.is_dir():
            for source in _iter_files(old_root):
                try:
                    relative = source.relative_to(old_root).as_posix()
                except ValueError:
                    continue
                try:
                    outcome = _move_into(new_root, relative, source)
                except OSError:
                    failed.append({"file": relative, "code": "INBOX_MOVE_FAILED"})
                    continue
                if outcome == "moved":
                    moved.append(relative)
                elif outcome == "skipped":
                    skipped.append(relative)
                else:
                    conflicts.append(relative)
            _prune_empty_dirs(old_root)

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            payload["path"] = str(new_root)
            return payload

        raw = _state().mutate(mutate)
        snapshot = settings_snapshot(raw)
        return {
            **snapshot,
            "unchanged": False,
            "migrated": moved,
            "skipped": skipped,
            "conflicts": conflicts,
            "failed": failed,
            "migrate_requested": bool(migrate),
        }


# ---------------------------------------------------------------------------
# 迁移托管资产
# ---------------------------------------------------------------------------


def _library_items() -> list[dict[str, Any]]:
    from gw.asset_library.service import default_asset_library_service

    return default_asset_library_service.iter_library_items()


def _inside_allowed_root(path: Path) -> bool:
    """迁移只允许搬运用户已挂载素材根内的文件，避免把系统或任意路径文件卷进收集箱。"""
    for root in storage.allowed_roots():
        try:
            canonical_root = root.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            path.relative_to(canonical_root)
        except ValueError:
            continue
        return True
    return False


def _local_file_from_url(url: Any) -> Path | None:
    """只认 ``file://`` 或绝对路径且真实存在、位于已挂载素材根内的普通文件。"""
    text = str(url or "").strip()
    if not text:
        return None
    candidate: Path
    if text.lower().startswith("file://"):
        raw_path = unquote(urlsplit(text).path)
        if os.name == "nt" and len(raw_path) > 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        candidate = Path(raw_path)
    else:
        try:
            candidate = Path(text).expanduser()
        except (OSError, RuntimeError, ValueError):
            return None
        if not candidate.is_absolute():
            return None
    try:
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file() or not _inside_allowed_root(resolved):
        return None
    return resolved


def _unique_relative(root: Path, name: str) -> str:
    base = Path(str(name or "").replace("\\", "/")).name.strip()
    if base in {"", ".", ".."}:
        base = "collected.bin"
    stem, suffix = os.path.splitext(base)
    candidate = base
    index = 2
    while (root / candidate).exists():
        candidate = f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def migrate_asset_library() -> dict[str, Any]:
    """把托管资产记录并入收集箱；按 asset_id 记账，重复执行不会多出数据。"""
    with _lock:
        raw = _state().read()
        root = _ensure_dir(inbox_root(raw))
        migrated = raw.setdefault("migrated", {})
        moved: list[str] = []
        url_entries: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for item in _library_items():
            asset_id = str(item.get("asset_id") or "").strip()
            if not asset_id:
                continue
            if asset_id in migrated:
                skipped.append(asset_id)
                continue
            name = str(item.get("name") or asset_id).strip() or asset_id
            url = item.get("url")
            record: dict[str, Any] = {
                "asset_id": asset_id,
                "name": name,
                "url": url,
                "library_id": item.get("library_id"),
                "category_id": item.get("category_id"),
                "category_type": item.get("category_type"),
                "migrated_at": storage.now_iso(),
                "file": None,
                "kind": "url",
            }
            local_file = _local_file_from_url(url)
            if local_file is not None:
                relative = _unique_relative(root, local_file.name)
                try:
                    shutil.move(str(local_file), str(root / relative))
                except OSError:
                    failed.append({"asset_id": asset_id, "code": "INBOX_MIGRATE_FAILED"})
                else:
                    record.update({"file": relative, "kind": classify_name(relative)})
                    moved.append(asset_id)
            if record["file"] is None:
                url_entries.append(asset_id)
                # 只有真正的访问地址才算链接条目；url 为空的托管记录标注为「仅记录」，
                # 避免在收集箱里显示成可点击却无地址的链接。
                if not str(record.get("url") or "").strip():
                    record["kind"] = "record"
            migrated[asset_id] = record

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            payload.setdefault("migrated", {}).update(migrated)
            return payload

        raw = _state().mutate(mutate)
        snapshot = settings_snapshot(raw)
        return {
            "moved_files": len(moved),
            "url_entries": len(url_entries),
            "skipped": len(skipped),
            "failed": failed,
            "total_records": len(raw.get("migrated", {})),
            "settings": snapshot,
            "data_status": "partial" if failed else "ok",
        }


# ---------------------------------------------------------------------------
# 手动上传
# ---------------------------------------------------------------------------


def add_upload(filename: Any, content: bytes) -> dict[str, Any]:
    """把手动上传的素材直接写进收集箱（默认落点），不做索引登记。"""
    base = Path(str(filename or "").replace("\\", "/")).name.strip()
    if base in {"", ".", ".."}:
        raise CleanroomException(400, "INVALID_REQUEST", "上传文件名无效")
    if not content:
        raise CleanroomException(400, "INVALID_REQUEST", "上传内容为空")
    if len(content) > MAX_INBOX_UPLOAD_BYTES:
        raise CleanroomException(400, "CONTENT_TOO_LARGE", "上传内容超过上限")
    with _lock:
        raw = _state().read()
        root = _ensure_dir(inbox_root(raw))
        relative = _unique_relative(root, base)
        target = root / relative
        partial = target.with_name(".gw-inbox-pending-" + target.name)
        try:
            with partial.open("wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(partial, target)
        except OSError:
            partial.unlink(missing_ok=True)
            raise CleanroomException(503, "FILE_WRITE_FAILED", "收集箱写入失败") from None
        stat = target.stat()
        return {
            "id": f"inbox:{relative}",
            "source": "inbox",
            "name": target.name,
            "file": relative,
            "kind": classify_name(target.name),
            "size_bytes": stat.st_size,
            "modified_at": int(stat.st_mtime),
            "url": None,
            "asset_id": None,
        }
