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

"""我的资源磁盘文件夹整理服务（QIN-545）。

提供用户挂载根目录内的文件夹新建（任意深度）、重命名、子树移动、文件移动（冲突处理与占用检测）、
删除移入回收站及操作撤销接口，并执行受影响路径索引的增量更新。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any

from gw.asset_library import repository as asset_lib_repo
from gw.core import storage
from gw.core.errors import CleanroomException
from gw.asset_library.naming import (
    _handle_os_path_error,
    validate_folder_component_name,
    validate_target_path,
)
from gw.settings.service import default_storage_settings_service

logger = logging.getLogger(__name__)

_ORGANIZE_LOCK = threading.RLock()


def _is_file_locked_error(exc: Exception) -> bool:
    """识别文件被外部进程打开占用的错误。"""
    if isinstance(exc, PermissionError):
        winerror = getattr(exc, "winerror", None)
        if winerror in (32, 33):  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
            return True
        errno_val = getattr(exc, "errno", None)
        if errno_val in (13, 16):  # EACCES, EBUSY
            return True
    return False


def _safe_folder_name(name: str) -> str:
    return validate_folder_component_name(name)


def _next_op_id(prefix: str = "org_") -> str:
    """生成密码学安全且不可预测的操作与撤销 ID。"""
    return f"{prefix}{secrets.token_hex(16)}"


def _system_internal_dir() -> Path:
    base = storage.data_root() / "system_internal"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def _organize_version_file() -> Path:
    return _system_internal_dir() / "organize_version.json"


def _atomic_write_version(version: int) -> None:
    """先写临时文件再原子替换，防止断电或并发读写导致损坏。"""
    vdir = _system_internal_dir()
    vfile = _organize_version_file()
    payload = {
        "version": version,
        "updated_at": storage.now_iso(),
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp_file = vdir / f"organize_version.json.tmp.{secrets.token_hex(8)}"
    try:
        tmp_file.write_text(content, encoding="utf-8")
        os.replace(tmp_file, vfile)
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError as exc:
                logger.warning("清理版本临时文件失败 %s: %s", tmp_file, exc)


def get_organize_version() -> int:
    """读取当前资产整理状态的 CAS 版本号。

    若版本文件缺失或内容损坏，直接抛出 500 VERSION_STATE_CORRUPTED，绝不静默回退默认值，禁止重置 CAS 历史。
    """
    vfile = _organize_version_file()
    if not vfile.exists():
        raise CleanroomException(500, "VERSION_STATE_CORRUPTED", "版本状态文件缺失，拒绝操作")
    try:
        data = json.loads(vfile.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "version" not in data:
            raise ValueError("Invalid version file format")
        return int(data["version"])
    except Exception as exc:
        logger.error("版本状态文件解析失败 %s: %s", vfile, exc)
        raise CleanroomException(500, "VERSION_STATE_CORRUPTED", "版本状态文件损坏，拒绝操作") from exc


def init_organize_version_file(initial_version: int = 1) -> int:
    """显式初始化版本文件（仅供系统冷启动或测试夹具初始化，禁止 GET 接口隐式重建）。"""
    with _ORGANIZE_LOCK:
        vfile = _organize_version_file()
        if not vfile.exists():
            _atomic_write_version(initial_version)
            return initial_version
        return get_organize_version()


def assert_version_matches(expected_version: int | None) -> int:
    """强制校验 expected_version（不豁免）。

    所有写操作必须携带 expected_version：
    若未传 expected_version，或与当前版本不一致，抛出 HTTP 409 VERSION_CONFLICT。
    返回当前的有效版本号。本函数绝不提前递增版本号。
    """
    if expected_version is None:
        raise CleanroomException(
            409,
            "VERSION_CONFLICT",
            "缺少 expected_version，拒绝执行写操作",
        )
    current_version = get_organize_version()
    if expected_version != current_version:
        raise CleanroomException(
            409,
            "VERSION_CONFLICT",
            f"版本冲突：当前版本为 {current_version}，期望版本为 {expected_version}",
        )
    return current_version


def bump_organize_version() -> int:
    """在操作全部成功后原子递增 CAS 版本号。

    返回递增后的新版本号。调用方须在 _ORGANIZE_LOCK 保护下调用。
    """
    current_version = get_organize_version()
    new_version = current_version + 1
    _atomic_write_version(new_version)
    return new_version


def _client_safe_undo(record: dict[str, Any]) -> dict[str, Any]:
    """返回安全的撤销描述结构体，绝不向客户端暴露绝对路径（如 abs_source/abs_target/stashed_path）。"""
    safe: dict[str, Any] = {
        "undo_id": record.get("undo_id"),
        "action": record.get("action"),
        "target": record.get("target"),
        "root_id": record.get("root_id"),
    }
    if "source" in record:
        safe["source"] = record["source"]
    return safe


def check_root_writable(root: Path, root_id: str | None = None, resolved_target: Path | None = None) -> None:
    """校验目标允许根是否为可写根；系统根或只读配置一律拒绝写入。

    resolved_target：实际目标文件/目录的绝对路径（可选）。同时检查目标是否位于只读库内，
    以及目标目录是否包含只读库，以阻止改动只读库的祖先目录。
    """
    resolved_root = root.resolve()
    resolved_check = (resolved_target or root).resolve()
    env_roots = [r.resolve() for r in storage.environment_allowed_roots()]
    if any(resolved_root == er or resolved_root.is_relative_to(er) for er in env_roots):
        raise CleanroomException(403, "SYSTEM_ROOT_READ_ONLY", "系统资源的根目录为只读，拒绝写入")

    snapshot = default_storage_settings_service.get_snapshot()
    source_policy = snapshot.local_library_read_only or {}
    rid = root_id or storage.root_identity_id(root)
    if source_policy.get(rid, False):
        raise CleanroomException(403, "SOURCE_READ_ONLY", "该素材源为只读，拒绝写入")

    # snapshot.local_libraries 为 list[dict]，安全按 dict 读取并恢复按库 id 的只读校验
    for lib in snapshot.local_libraries:
        if not isinstance(lib, dict):
            continue
        lib_id = str(lib.get("id") or lib.get("library_id") or "").strip()
        lib_rid = str(lib.get("root_identity_id") or "").strip()
        lib_path = str(lib.get("path") or lib.get("root_path") or "").strip()
        lib_read_only = bool(lib.get("read_only", False)) or bool(
            (lib_id and source_policy.get(lib_id, False))
            or (lib_rid and source_policy.get(lib_rid, False))
        )

        is_match = False
        if lib_rid and (lib_rid == rid or storage.coerce_root_id(lib_rid) == storage.coerce_root_id(rid)):
            is_match = True
        elif lib_id and (lib_id == rid or storage.coerce_root_id(lib_id) == storage.coerce_root_id(rid)):
            is_match = True
        elif lib_path:
            try:
                res_lib = Path(lib_path).resolve()
                # 同时保护库内路径、库本身和包含只读库的祖先目录。
                if (resolved_root == res_lib or resolved_root.is_relative_to(res_lib)
                        or resolved_check == res_lib or resolved_check.is_relative_to(res_lib)
                        or res_lib.is_relative_to(resolved_check)):
                    is_match = True
            except (ValueError, OSError) as exc:
                # 只读库无法解析时无法排除路径重叠，必须拒绝写入；可写库记录后跳过。
                logger.warning("check_root_writable: 解析库路径失败 %r: %s", lib_path, exc)
                if lib_read_only:
                    raise CleanroomException(403, "SOURCE_READ_ONLY", "只读素材源路径无法安全校验，拒绝写入") from None
                continue

        if is_match:
            # 校验库自身的 read_only 以及通过库 id 配置在 source_policy 中的只读策略
            if lib.get("read_only", False):
                raise CleanroomException(403, "SOURCE_READ_ONLY", "该素材源为只读，拒绝写入")
            if lib_id and source_policy.get(lib_id, False):
                raise CleanroomException(403, "SOURCE_READ_ONLY", "该素材源为只读，拒绝写入")
            if lib_rid and source_policy.get(lib_rid, False):
                raise CleanroomException(403, "SOURCE_READ_ONLY", "该素材源为只读，拒绝写入")


def resolve_writable_path(raw_path: str, root_id: str | None = None) -> tuple[Path, Path, str]:
    """解析并校验可写目标路径 (resolved_path, matched_root, matched_root_id)。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise CleanroomException(400, "INVALID_PATH", "路径不合法")

    if "\0" in raw_path:
        raise CleanroomException(400, "INVALID_PATH", "路径包含非法字符")

    raw_path_norm = raw_path.replace("\\", "/")
    parts = [p for p in raw_path_norm.split("/") if p]
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() and ".." in parts:
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "禁止越权访问上层目录")

    roots = storage.allowed_roots()
    if not roots:
        raise CleanroomException(403, "LOCAL_FILE_ACCESS_NOT_ADMITTED", "未配置允许访问的挂载目录")

    try:
        resolved = storage.resolve_request_path(raw_path, root_id=root_id)
    except OSError as exc:
        _handle_os_path_error(exc)
        raise

    # 显式拒绝 data_root 下路径，杜绝通过接口访问或删除系统内部账本状态
    dr = storage.data_root().resolve()
    resolved_res = resolved.resolve()
    if resolved_res == dr or resolved_res.is_relative_to(dr):
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "禁止操作系统内部数据目录")

    matched_root_id, _ = storage.location_identity(resolved)

    matched_root = None
    for r in roots:
        if storage.root_identity_id(r) == matched_root_id:
            matched_root = r
            break
    if matched_root is None:
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "目标路径不属于任何允许的挂载目录")

    check_root_writable(matched_root, matched_root_id, resolved_target=resolved)

    return resolved, matched_root, matched_root_id


def _recycler_dir() -> Path:
    recycler = _system_internal_dir() / "recycler"
    recycler.mkdir(parents=True, exist_ok=True)
    return recycler.resolve()


def _is_within_recycler(p: Path) -> bool:
    try:
        res = p.resolve()
        recycler = _recycler_dir().resolve()
        return res.is_relative_to(recycler) and res != recycler
    except (ValueError, OSError):
        return False


def _undo_records_dir() -> Path:
    undo_dir = _system_internal_dir() / "undo_records"
    undo_dir.mkdir(parents=True, exist_ok=True)
    return undo_dir.resolve()


def _stash_to_recycler(target: Path, root_id: str) -> dict[str, Any]:
    """将物理目标安全移入回收区，使用独立的随机目录隔绝所有同名冲突。"""
    recycler = _recycler_dir()
    staging_id = f"del_{secrets.token_hex(16)}"
    staging_dir = recycler / staging_id
    payload_dir = staging_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)

    dest = payload_dir / target.name

    manifest_file = staging_dir / "manifest.json"
    manifest_data = {
        "staging_id": staging_id,
        "original_name": target.name,
        "original_abs_path": str(target.resolve()),
        "root_id": root_id,
        "deleted_at": storage.now_iso(),
        "is_dir": target.is_dir(),
    }
    manifest_file.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    try:
        shutil.move(str(target), str(dest))
    except Exception as exc:
        if _is_file_locked_error(exc):
            raise CleanroomException(423, "FILE_LOCKED", "文件或目录被占用，无法移入回收区") from exc
        logger.error("移入回收站失败 %s -> %s: %s", target, dest, exc)
        raise CleanroomException(500, "RECYCLE_FAILED", "移入回收站失败") from exc

    return {
        "staging_id": staging_id,
        "staging_dir": str(staging_dir.resolve()),
        "stashed_path": str(dest.resolve()),
        "original_name": target.name,
        "original_abs_path": str(target.resolve()),
        "root_id": root_id,
        "is_dir": manifest_data["is_dir"],
    }


def _save_undo_record(undo_record: dict[str, Any]) -> None:
    undo_id = str(undo_record.get("undo_id") or "").strip()
    if not undo_id:
        return
    record_file = _undo_records_dir() / f"{undo_id}.json"
    temp_file = record_file.with_name(f"{record_file.name}.tmp.{secrets.token_hex(8)}")
    try:
        temp_file.write_text(
            json.dumps(undo_record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(record_file)
    except Exception:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            logger.warning("清理撤销账本临时文件失败 %s: %s", temp_file, cleanup_exc)
        raise


def _restore_from_recycler(stash_record: dict[str, Any], root_id: str | None = None) -> None:
    if not isinstance(stash_record, dict):
        raise CleanroomException(400, "INVALID_REQUEST", "回收暂存记录格式错误")

    stashed_path_raw = stash_record.get("stashed_path")
    if not stashed_path_raw:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少回收站暂存路径")

    stashed_path = Path(str(stashed_path_raw))
    # 严格校验 stashed_path 必须位于回收站内，禁止任何回收站外的路径逃逸
    if not _is_within_recycler(stashed_path):
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "回收暂存路径不在回收站受管目录内")

    staging_dir_raw = stash_record.get("staging_dir")
    if staging_dir_raw:
        staging_dir = Path(str(staging_dir_raw))
    elif stashed_path.parent.name == "payload":
        staging_dir = stashed_path.parent.parent
    else:
        staging_dir = stashed_path.parent

    # 严格校验 staging_dir 必须位于回收站内且非回收区根目录，禁止清空整个回收区
    if not _is_within_recycler(staging_dir):
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "暂存目录不在回收站受管目录内")

    if not stashed_path.exists():
        raise CleanroomException(404, "RECYCLE_PAYLOAD_NOT_FOUND", "回收暂存内容不存在")

    # 严格读取服务端落盘在 staging_dir 的 manifest.json（Fail-Closed，不吞异常，不回退客户端输入）
    manifest_file = staging_dir / "manifest.json"
    if not manifest_file.exists():
        raise CleanroomException(404, "RECYCLE_MANIFEST_NOT_FOUND", "回收暂存清单不存在，拒绝恢复")

    try:
        server_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("回收暂存清单解析失败 %s: %s", manifest_file, exc)
        raise CleanroomException(500, "RECYCLE_MANIFEST_CORRUPTED", "回收暂存清单已损坏，拒绝恢复") from exc

    if not isinstance(server_manifest, dict):
        raise CleanroomException(500, "RECYCLE_MANIFEST_CORRUPTED", "回收暂存清单已损坏（非字典结构），拒绝恢复")

    original_path_raw = server_manifest.get("original_abs_path")
    if not original_path_raw:
        raise CleanroomException(500, "RECYCLE_MANIFEST_INVALID", "回收暂存清单缺少目标路径，拒绝恢复")

    record_rid = server_manifest.get("root_id") or root_id

    # 目标恢复路径必须严格经过 resolve_writable_path 校验（限制在允许根内且非只读）
    target, matched_root, _ = resolve_writable_path(str(original_path_raw), record_rid)

    if target.resolve() == matched_root.resolve():
        raise CleanroomException(400, "CANNOT_DELETE_ROOT", "禁止恢复覆盖挂载根目录")

    if target.exists():
        raise CleanroomException(409, "TARGET_EXISTS", "恢复目标路径已存在")

    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(stashed_path), str(target))
    except Exception as exc:
        if _is_file_locked_error(exc):
            raise CleanroomException(423, "FILE_LOCKED", "恢复目标位置文件被占用") from exc
        logger.error("从回收区恢复失败 %s -> %s: %s", stashed_path, target, exc)
        raise CleanroomException(500, "RESTORE_FAILED", "从回收区恢复失败") from exc

    # 恢复成功后清理 staging_dir，记录日志
    try:
        shutil.rmtree(str(staging_dir))
    except Exception as exc:
        logger.warning("清理回收暂存目录失败 %s: %s", staging_dir, exc)


def _matches_root_id(entry: dict[str, Any], target_root_id: str) -> bool:
    """精确比较条目所属的根身份，缺根条目尝试按路径解析，绝不默认匹配全部根。"""
    entry_root = entry.get("_root_identity") or entry.get("root_id") or entry.get("storage_root_id")
    if entry_root:
        str_entry = str(entry_root)
        str_target = str(target_root_id)
        if str_entry == str_target:
            return True
        return storage.coerce_root_id(str_entry) == storage.coerce_root_id(str_target)

    file_path = entry.get("path") or entry.get("abs_path")
    if file_path:
        try:
            detected_rid, _ = storage.location_identity(Path(file_path))
            if detected_rid:
                return storage.coerce_root_id(detected_rid) == storage.coerce_root_id(target_root_id)
        except (CleanroomException, ValueError, OSError) as exc:
            logger.warning("解析条目所属根身份失败 %s: %s", file_path, exc)
    return False


# ---------------------------------------------------------------------------
# 索引增量更新协同
# ---------------------------------------------------------------------------


class OrganizationSyncListener:
    """文件夹整理索引同步监听器基类。"""

    def on_prefix_updated(
        self, root_id: str, old_path: Path, new_path: Path, is_dir: bool = False
    ) -> int:
        return 0

    def on_paths_removed(
        self, root_id: str, target: Path, is_dir: bool = False
    ) -> list[dict[str, Any]]:
        return []

    def on_paths_restored(
        self, root_id: str, stashed_data: list[dict[str, Any]]
    ) -> None:
        pass


_LISTENERS: list[OrganizationSyncListener] = []


def register_sync_listener(listener: OrganizationSyncListener) -> None:
    if listener not in _LISTENERS:
        _LISTENERS.append(listener)


def _incremental_update_prefix(
    root_id: str, old_path: Path, new_path: Path, is_dir: bool = False
) -> int:
    """增量更新受影响文件在 local_asset_index 与注册监听器中的索引路径。"""
    old_canonical = storage.location_identity(old_path)[1]
    new_canonical = storage.location_identity(new_path)[1]
    old_disp = storage.relative_display(old_path)
    new_disp = storage.relative_display(new_path)

    updated_count = 0

    # 1. 增量更新 local_asset_index.json
    def mutate_local(raw: dict[str, Any]) -> int:
        nonlocal updated_count
        entries = raw.get("entries", {})
        affected: list[tuple[str, dict[str, Any]]] = []
        for key, entry in entries.items():
            if not _matches_root_id(entry, root_id):
                continue
            canonical = entry.get("canonical_relpath") or ""
            if is_dir:
                if canonical == old_canonical or canonical.startswith(old_canonical + "/"):
                    affected.append((key, entry))
            else:
                if canonical == old_canonical:
                    affected.append((key, entry))

        for old_key, entry in affected:
            canonical = entry.get("canonical_relpath") or ""
            tail = canonical[len(old_canonical):] if is_dir else ""
            entry["canonical_relpath"] = new_canonical + tail

            disp = entry.get("display_path") or ""
            if is_dir and disp.startswith(old_disp):
                entry["display_path"] = new_disp + disp[len(old_disp):]
            elif not is_dir:
                entry["display_path"] = new_disp
                entry["name"] = new_path.name

            del entries[old_key]
            entry_root_identity = entry.get("_root_identity") or root_id
            new_key = asset_lib_repo._local_entry_key(entry_root_identity, entry["display_path"])
            entries[new_key] = entry
            updated_count += 1
        return len(affected)

    asset_lib_repo._local_state().mutate(mutate_local)

    # 2. 通知所有注册的监听器（如 asset_registry）
    for listener in _LISTENERS:
        try:
            listener.on_prefix_updated(root_id, old_path, new_path, is_dir=is_dir)
        except Exception as exc:
            logger.warning("组织整理更新监听器执行失败 %s: %s", listener, exc)

    return updated_count


def _incremental_remove_paths(
    root_id: str, target: Path, is_dir: bool = False
) -> dict[str, Any]:
    """增量从索引中移出指定路径或整树，并暂存条目用于撤销。"""
    target_canonical = storage.location_identity(target)[1]
    stashed_local_entries: list[dict[str, Any]] = []

    def mutate_local(raw: dict[str, Any]) -> None:
        entries = raw.get("entries", {})
        to_delete = []
        for key, entry in entries.items():
            if not _matches_root_id(entry, root_id):
                continue
            canonical = entry.get("canonical_relpath") or ""
            if is_dir:
                if canonical == target_canonical or canonical.startswith(target_canonical + "/"):
                    to_delete.append(key)
            else:
                if canonical == target_canonical:
                    to_delete.append(key)
        for key in to_delete:
            stashed_local_entries.append(entries.pop(key))

    asset_lib_repo._local_state().mutate(mutate_local)

    stashed_registry_entries: list[dict[str, Any]] = []
    for listener in _LISTENERS:
        try:
            res = listener.on_paths_removed(root_id, target, is_dir=is_dir)
            if isinstance(res, list):
                stashed_registry_entries.extend(res)
        except Exception as exc:
            logger.warning("组织整理移除监听器执行失败 %s: %s", listener, exc)

    return {
        "local": stashed_local_entries,
        "registry": stashed_registry_entries,
    }


def _incremental_restore_stashed_index(
    root_id: str, stashed_index: dict[str, Any]
) -> None:
    """撤销删除时，将暂存的条目增量写回 local_asset_index 与监听器索引。"""
    local_items = stashed_index.get("local") or []
    reg_items = stashed_index.get("registry") or []

    if local_items:
        def mutate_local(raw: dict[str, Any]) -> None:
            entries = raw.setdefault("entries", {})
            for item in local_items:
                disp = item.get("display_path") or ""
                key = asset_lib_repo._local_entry_key(root_id, disp)
                entries[key] = item

        asset_lib_repo._local_state().mutate(mutate_local)

    if reg_items:
        for listener in _LISTENERS:
            try:
                listener.on_paths_restored(root_id, reg_items)
            except Exception as exc:
                logger.warning("组织整理恢复监听器执行失败 %s: %s", listener, exc)


# ---------------------------------------------------------------------------
# 业务操作实现
# ---------------------------------------------------------------------------


def create_folder(
    raw_path: str | None = None,
    name: str | None = None,
    parent: str | None = None,
    root_id: str | None = None,
    operator: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """在用户挂载根下新建任意深度的子文件夹。"""
    with _ORGANIZE_LOCK:
        assert_version_matches(expected_version)

        if raw_path is None:
            if not name:
                raise CleanroomException(400, "INVALID_PATH", "缺少文件夹名称")
            folder_name = validate_folder_component_name(name)
            target_expr = f"{parent.rstrip('/')}/{folder_name}" if parent else folder_name
        else:
            target_expr = raw_path

        target, root, rid = resolve_writable_path(target_expr, root_id)
        if target.resolve() == root.resolve():
            raise CleanroomException(400, "CANNOT_DELETE_ROOT", "禁止覆盖挂载根目录")

        validate_target_path(target, root)

        try:
            if target.exists():
                raise CleanroomException(409, "TARGET_EXISTS", "目标文件夹已存在")
        except OSError as exc:
            _handle_os_path_error(exc)
            raise

        try:
            target.mkdir(parents=True, exist_ok=False)
        except CleanroomException:
            raise
        except OSError as exc:
            _handle_os_path_error(exc)
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件夹位置被占用") from exc
            logger.error("创建文件夹失败 %s: %s", target, exc)
            raise CleanroomException(500, "FOLDER_CREATE_FAILED", "创建文件夹失败") from exc
        except Exception as exc:
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件夹位置被占用") from exc
            logger.error("创建文件夹失败 %s: %s", target, exc)
            raise CleanroomException(500, "FOLDER_CREATE_FAILED", "创建文件夹失败") from exc

        undo_record = {
            "undo_id": _next_op_id("undo_"),
            "operator": operator,
            "action": "create_folder",
            "target": storage.relative_display(target),
            "abs_target": str(target.resolve()),
            "root_id": rid,
            "canonical_relpath": storage.location_identity(target)[1],
        }
        _save_undo_record(undo_record)

        new_version = bump_organize_version()

        return {
            "created": True,
            "name": target.name,
            "display_path": storage.relative_display(target),
            "canonical_relpath": storage.location_identity(target)[1],
            "root_id": rid,
            "version": new_version,
            "undo": _client_safe_undo(undo_record),
        }


def rename_folder(
    raw_path: str,
    new_name: str,
    root_id: str | None = None,
    operator: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """重命名文件夹，并增量更新受影响的子树路径索引。"""
    with _ORGANIZE_LOCK:
        assert_version_matches(expected_version)

        src, root, rid = resolve_writable_path(raw_path, root_id)
        if src.resolve() == root.resolve():
            raise CleanroomException(400, "CANNOT_RENAME_ROOT", "禁止重命名挂载根目录本身")
        if not src.exists():
            raise CleanroomException(404, "FOLDER_NOT_FOUND", "文件夹不存在")
        if not src.is_dir():
            raise CleanroomException(400, "INVALID_PATH", "目标路径不是文件夹")

        clean_name = validate_folder_component_name(new_name)
        dst = src.parent / clean_name
        validate_target_path(dst, root)

        try:
            if dst.exists():
                raise CleanroomException(409, "TARGET_EXISTS", "目标文件夹已存在")
        except OSError as exc:
            _handle_os_path_error(exc)
            raise

        # 确保 dst 仍在允许根内
        dst_resolved, _, dst_rid = resolve_writable_path(str(dst), root_id=rid)
        if dst_rid != rid:
            raise CleanroomException(403, "CROSS_ROOT_FORBIDDEN", "禁止跨根重命名文件夹")

        try:
            src.replace(dst_resolved)
        except OSError as exc:
            _handle_os_path_error(exc)
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件夹被占用，无法重命名") from exc
            logger.error("重命名文件夹失败 %s -> %s: %s", src, dst_resolved, exc)
            raise CleanroomException(500, "FOLDER_RENAME_FAILED", "重命名文件夹失败") from exc
        except Exception as exc:
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件夹被占用，无法重命名") from exc
            logger.error("重命名文件夹失败 %s -> %s: %s", src, dst_resolved, exc)
            raise CleanroomException(500, "FOLDER_RENAME_FAILED", "重命名文件夹失败") from exc

        try:
            affected = _incremental_update_prefix(rid, src, dst_resolved, is_dir=True)
        except Exception as exc:
            logger.error("重命名后增量更新索引失败，尝试回滚物理文件夹: %s", exc)
            try:
                dst_resolved.replace(src)
            except Exception as rollback_exc:
                logger.critical("回滚物理文件夹失败: %s", rollback_exc)
                undo_record = {
                    "undo_id": _next_op_id("undo_"),
                    "operator": operator,
                    "action": "rename_folder",
                    "source": storage.relative_display(dst_resolved),
                    "target": storage.relative_display(src),
                    "abs_source": str(dst_resolved.resolve()),
                    "abs_target": str(src.resolve()),
                    "root_id": rid,
                }
                _save_undo_record(undo_record)
            raise CleanroomException(500, "INDEX_SYNC_FAILED", "更新索引失败") from exc

        undo_record = {
            "undo_id": _next_op_id("undo_"),
            "operator": operator,
            "action": "rename_folder",
            "source": storage.relative_display(dst_resolved),
            "target": storage.relative_display(src),
            "abs_source": str(dst_resolved.resolve()),
            "abs_target": str(src.resolve()),
            "root_id": rid,
        }
        _save_undo_record(undo_record)

        new_version = bump_organize_version()

        return {
            "renamed": True,
            "from": storage.relative_display(src),
            "to": storage.relative_display(dst_resolved),
            "root_id": rid,
            "affected_assets": affected,
            "version": new_version,
            "undo": _client_safe_undo(undo_record),
        }


def move_folder(
    source: str,
    target: str,
    source_root_id: str | None = None,
    target_root_id: str | None = None,
    operator: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """移动文件夹（整棵子树跟随移动），并增量更新索引。"""
    with _ORGANIZE_LOCK:
        assert_version_matches(expected_version)

        src, src_root, src_rid = resolve_writable_path(source, source_root_id)
        if src.resolve() == src_root.resolve():
            raise CleanroomException(400, "CANNOT_MOVE_ROOT", "禁止移动挂载根目录本身")
        if not src.exists():
            raise CleanroomException(404, "FOLDER_NOT_FOUND", "源文件夹不存在")
        if not src.is_dir():
            raise CleanroomException(400, "INVALID_PATH", "源路径不是文件夹")

        dst_target_root = target_root_id or src_rid
        dst, dst_root, dst_rid = resolve_writable_path(target, dst_target_root)
        if src_rid != dst_rid:
            raise CleanroomException(403, "CROSS_ROOT_FORBIDDEN", "禁止跨挂载根移动文件夹")

        validate_target_path(dst, dst_root)

        src_res = src.resolve()
        dst_res = dst.resolve()
        if dst_res == src_res:
            raise CleanroomException(400, "INVALID_MOVE_DESTINATION", "目标与源相同")
        if dst_res.is_relative_to(src_res):
            raise CleanroomException(400, "INVALID_MOVE_DESTINATION", "禁止将文件夹移动到其子目录中")

        try:
            if dst.exists():
                raise CleanroomException(409, "TARGET_EXISTS", "目标路径已存在")
        except OSError as exc:
            _handle_os_path_error(exc)
            raise

        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except OSError as exc:
            _handle_os_path_error(exc)
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件夹被占用，无法移动") from exc
            logger.error("移动文件夹失败 %s -> %s: %s", src, dst, exc)
            raise CleanroomException(500, "FOLDER_MOVE_FAILED", "移动文件夹失败") from exc
        except Exception as exc:
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件夹被占用，无法移动") from exc
            logger.error("移动文件夹失败 %s -> %s: %s", src, dst, exc)
            raise CleanroomException(500, "FOLDER_MOVE_FAILED", "移动文件夹失败") from exc

        try:
            affected = _incremental_update_prefix(src_rid, src, dst, is_dir=True)
        except Exception as exc:
            logger.error("移动文件夹后增量更新索引失败，尝试回滚: %s", exc)
            try:
                shutil.move(str(dst), str(src))
            except Exception as rollback_exc:
                logger.critical("回滚移动文件夹失败: %s", rollback_exc)
                undo_record = {
                    "undo_id": _next_op_id("undo_"),
                    "operator": operator,
                    "action": "move_folder",
                    "source": storage.relative_display(dst),
                    "target": storage.relative_display(src),
                    "abs_source": str(dst.resolve()),
                    "abs_target": str(src.resolve()),
                    "root_id": src_rid,
                }
                _save_undo_record(undo_record)
            raise CleanroomException(500, "INDEX_SYNC_FAILED", "更新索引失败") from exc

        undo_record = {
            "undo_id": _next_op_id("undo_"),
            "operator": operator,
            "action": "move_folder",
            "source": storage.relative_display(dst),
            "target": storage.relative_display(src),
            "abs_source": str(dst.resolve()),
            "abs_target": str(src.resolve()),
            "root_id": src_rid,
        }
        _save_undo_record(undo_record)

        new_version = bump_organize_version()

        return {
            "moved": True,
            "from": storage.relative_display(src),
            "to": storage.relative_display(dst),
            "root_id": src_rid,
            "affected_assets": affected,
            "version": new_version,
            "undo": _client_safe_undo(undo_record),
        }


def move_file(
    source: str,
    target: str,
    source_root_id: str | None = None,
    target_root_id: str | None = None,
    conflict_resolution: str | None = None,
    operator: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """移动文件，支持跳过/改名/覆盖冲突处理，并增量更新索引。"""
    with _ORGANIZE_LOCK:
        current_version = assert_version_matches(expected_version)

        src, src_root, src_rid = resolve_writable_path(source, source_root_id)
        if src.resolve() == src_root.resolve():
            raise CleanroomException(400, "CANNOT_MOVE_ROOT", "禁止移动挂载根目录本身")
        if not src.exists():
            raise CleanroomException(404, "FILE_NOT_FOUND", "源文件不存在")
        if not src.is_file():
            raise CleanroomException(400, "INVALID_PATH", "源路径不是文件")

        dst_target_root = target_root_id or src_rid
        dst, dst_root, dst_rid = resolve_writable_path(target, dst_target_root)
        if src_rid != dst_rid:
            raise CleanroomException(403, "CROSS_ROOT_FORBIDDEN", "禁止跨挂载根移动文件")

        if dst.exists() and dst.is_dir():
            dst = dst / src.name

        validate_target_path(dst, dst_root)

        src_res = src.resolve()
        dst_res = dst.resolve()
        if dst_res == src_res:
            raise CleanroomException(400, "INVALID_MOVE_DESTINATION", "目标与源相同")

        overwritten_backup: dict[str, Any] | None = None
        overwritten_stashed_index: dict[str, Any] | None = None
        try:
            dst_exists = dst.exists()
        except OSError as exc:
            _handle_os_path_error(exc)
            raise

        if dst_exists:
            if conflict_resolution == "skip":
                # 跳过冲突：不发生物理或索引变更，不递增版本号
                return {
                    "moved": False,
                    "skipped": True,
                    "from": storage.relative_display(src),
                    "to": storage.relative_display(dst),
                    "root_id": src_rid,
                    "version": current_version,
                }
            if conflict_resolution == "rename":
                idx = 1
                while True:
                    cand = dst.with_name(f"{dst.stem} ({idx}){dst.suffix}")
                    try:
                        cand_exists = cand.exists()
                    except OSError as exc:
                        _handle_os_path_error(exc)
                        raise
                    if not cand_exists:
                        dst = cand
                        break
                    idx += 1
            elif conflict_resolution == "overwrite":
                overwritten_backup = _stash_to_recycler(dst, src_rid)
                try:
                    overwritten_stashed_index = _incremental_remove_paths(src_rid, dst, is_dir=False)
                except Exception as exc:
                    logger.warning("移除被覆盖文件索引异常: %s", exc)
                    try:
                        _restore_from_recycler(overwritten_backup, root_id=src_rid)
                    except Exception as rest_exc:
                        logger.error("回滚被覆盖文件失败: %s", rest_exc)
                    raise CleanroomException(500, "INDEX_SYNC_FAILED", "更新索引失败") from exc
            else:
                raise CleanroomException(409, "TARGET_EXISTS", "目标文件已存在")

        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except OSError as exc:
            _handle_os_path_error(exc)
            if overwritten_backup is not None:
                try:
                    _restore_from_recycler(overwritten_backup, root_id=src_rid)
                except Exception as rest_exc:
                    logger.error("回滚被覆盖文件失败: %s", rest_exc)
                if overwritten_stashed_index:
                    try:
                        _incremental_restore_stashed_index(src_rid, overwritten_stashed_index)
                    except Exception as idx_exc:
                        logger.error("恢复被覆盖文件索引失败: %s", idx_exc)
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件被占用，无法移动") from exc
            logger.error("移动文件失败 %s -> %s: %s", src, dst, exc)
            raise CleanroomException(500, "FILE_MOVE_FAILED", "移动文件失败") from exc
        except Exception as exc:
            if overwritten_backup is not None:
                try:
                    _restore_from_recycler(overwritten_backup, root_id=src_rid)
                except Exception as rest_exc:
                    logger.error("回滚被覆盖文件失败: %s", rest_exc)
                if overwritten_stashed_index:
                    try:
                        _incremental_restore_stashed_index(src_rid, overwritten_stashed_index)
                    except Exception as idx_exc:
                        logger.error("恢复被覆盖文件索引失败: %s", idx_exc)
            if _is_file_locked_error(exc):
                raise CleanroomException(423, "FILE_LOCKED", "文件被占用，无法移动") from exc
            logger.error("移动文件失败 %s -> %s: %s", src, dst, exc)
            raise CleanroomException(500, "FILE_MOVE_FAILED", "移动文件失败") from exc

        try:
            _incremental_update_prefix(src_rid, src, dst, is_dir=False)
        except Exception as exc:
            logger.error("移动文件后增量更新索引失败，尝试回滚: %s", exc)
            try:
                shutil.move(str(dst), str(src))
                if overwritten_backup is not None:
                    _restore_from_recycler(overwritten_backup, root_id=src_rid)
                if overwritten_stashed_index:
                    _incremental_restore_stashed_index(src_rid, overwritten_stashed_index)
            except Exception as rollback_exc:
                logger.critical("移动文件回滚失败: %s", rollback_exc)
                undo_record = {
                    "undo_id": _next_op_id("undo_"),
                    "operator": operator,
                    "action": "move_file",
                    "source": storage.relative_display(dst),
                    "target": storage.relative_display(src),
                    "abs_source": str(dst.resolve()),
                    "abs_target": str(src.resolve()),
                    "root_id": src_rid,
                    "overwritten_backup": overwritten_backup,
                    "overwritten_stashed_index": overwritten_stashed_index,
                }
                _save_undo_record(undo_record)
            raise CleanroomException(500, "INDEX_SYNC_FAILED", "更新索引失败") from exc

        undo_record = {
            "undo_id": _next_op_id("undo_"),
            "operator": operator,
            "action": "move_file",
            "source": storage.relative_display(dst),
            "target": storage.relative_display(src),
            "abs_source": str(dst.resolve()),
            "abs_target": str(src.resolve()),
            "root_id": src_rid,
            "overwritten_backup": overwritten_backup,
            "overwritten_stashed_index": overwritten_stashed_index,
        }
        _save_undo_record(undo_record)

        new_version = bump_organize_version()

        return {
            "moved": True,
            "from": storage.relative_display(src),
            "to": storage.relative_display(dst),
            "root_id": src_rid,
            "version": new_version,
            "undo": _client_safe_undo(undo_record),
        }


def delete_entry(
    raw_path: str,
    root_id: str | None = None,
    operator: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """删除文件夹或文件进应用回收站，不做永久删除，并增量更新索引。"""
    with _ORGANIZE_LOCK:
        assert_version_matches(expected_version)

        target, matched_root, rid = resolve_writable_path(raw_path, root_id)
        if target.resolve() == matched_root.resolve():
            raise CleanroomException(400, "CANNOT_DELETE_ROOT", "禁止删除挂载根目录本身")
        if not target.exists():
            raise CleanroomException(404, "NOT_FOUND", "目标文件或文件夹不存在")

        is_dir = target.is_dir()
        # 1. 先将物理文件/文件夹移入回收区（物理落盘优先，失败则索引完好）
        stashed_file = _stash_to_recycler(target, rid)

        # 2. 物理移走成功后再更新索引
        try:
            stashed_index = _incremental_remove_paths(rid, target, is_dir=is_dir)
        except Exception as exc:
            try:
                _restore_from_recycler(stashed_file, root_id=rid)
            except Exception as rest_exc:
                logger.error("回滚回收区文件失败: %s", rest_exc)
            raise CleanroomException(500, "INDEX_SYNC_FAILED", "更新索引失败") from exc

        undo_record = {
            "undo_id": _next_op_id("undo_"),
            "operator": operator,
            "action": "delete",
            "stashed_file": stashed_file,
            "stashed_index": stashed_index,
            "root_id": rid,
            "is_dir": is_dir,
            "target": storage.relative_display(target),
            "abs_target": str(target.resolve()),
        }
        _save_undo_record(undo_record)

        new_version = bump_organize_version()

        return {
            "deleted": True,
            "path": storage.relative_display(target),
            "is_dir": is_dir,
            "root_id": rid,
            "version": new_version,
            "undo": _client_safe_undo(undo_record),
        }


def undo_operation(
    undo_descriptor: dict[str, Any],
    operator: str | None = None,
    is_admin: bool = False,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """执行反向撤销操作。"""
    with _ORGANIZE_LOCK:
        assert_version_matches(expected_version)

        if not isinstance(undo_descriptor, dict):
            raise CleanroomException(400, "INVALID_REQUEST", "撤销参数格式必须为字典对象")

        undo_id = str(undo_descriptor.get("undo_id") or "").strip()
        if not undo_id:
            raise CleanroomException(400, "INVALID_REQUEST", "缺少撤销凭据 ID (undo_id)")

        if not undo_id.startswith("undo_") or ".." in undo_id or "/" in undo_id or "\\" in undo_id:
            raise CleanroomException(400, "INVALID_REQUEST", "非法撤销凭据 ID")

        record_file = _undo_records_dir() / f"{undo_id}.json"
        if not record_file.exists():
            raise CleanroomException(404, "UNDO_NOT_FOUND", "撤销凭据不存在或已失效")

        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("撤销凭据解析失败 %s: %s", record_file, exc)
            raise CleanroomException(500, "UNDO_RECORD_CORRUPTED", "撤销凭据文件损坏") from exc

        if not isinstance(record, dict):
            raise CleanroomException(500, "UNDO_RECORD_CORRUPTED", "撤销凭据格式错误（非字典结构）")

        # 校验操作者绑定与权限（跨用户撤销拦截）
        record_operator = record.get("operator")
        if record_operator and not is_admin and operator != record_operator:
            raise CleanroomException(403, "PERMISSION_DENIED", "只能撤销自己发起的操作或由管理员撤销")

        action = str(record.get("action") or "").strip()
        rid = record.get("root_id")

        if action == "create_folder":
            target_str = str(record.get("abs_target") or record.get("target") or "")
            target, matched_root, _ = resolve_writable_path(target_str, rid)
            if target.resolve() == matched_root.resolve():
                raise CleanroomException(400, "CANNOT_DELETE_ROOT", "禁止操作挂载根目录本身")
            if target.exists() and target.is_dir():
                # 严格校验：仅当该文件夹为空时才予以清理删除；若非空，拒绝删除以保护已有文件
                try:
                    target.rmdir()
                except OSError as exc:
                    raise CleanroomException(409, "FOLDER_NOT_EMPTY", "文件夹非空，无法撤销新建操作") from exc
            record_file.unlink(missing_ok=True)
            new_version = bump_organize_version()
            return {
                "undone": True,
                "action": action,
                "target": storage.relative_display(target),
                "version": new_version,
            }

        if action in {"rename_folder", "move_folder"}:
            curr_str = str(record.get("abs_source") or record.get("source") or "")
            orig_str = str(record.get("abs_target") or record.get("target") or "")
            curr, curr_root, _ = resolve_writable_path(curr_str, rid)
            orig, orig_root, _ = resolve_writable_path(orig_str, rid)
            if curr.resolve() == curr_root.resolve() or orig.resolve() == orig_root.resolve():
                raise CleanroomException(400, "CANNOT_MOVE_ROOT", "禁止操作挂载根目录本身")

            folder_restored = bool(record.get("folder_restored", False))
            if not folder_restored:
                if not curr.exists():
                    if orig.exists():
                        folder_restored = True
                        record["folder_restored"] = True
                        _save_undo_record(record)
                    else:
                        raise CleanroomException(404, "FOLDER_NOT_FOUND", "撤销源文件夹不存在")
                else:
                    if orig.exists():
                        raise CleanroomException(409, "TARGET_EXISTS", "撤销目标文件夹已存在")
                    orig.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(curr), str(orig))
                        folder_restored = True
                        record["folder_restored"] = True
                        _save_undo_record(record)
                    except Exception as exc:
                        if _is_file_locked_error(exc):
                            raise CleanroomException(423, "FILE_LOCKED", "文件夹被占用，无法撤销") from exc
                        logger.error("撤销文件夹操作失败 %s -> %s: %s", curr, orig, exc)
                        raise CleanroomException(500, "UNDO_FAILED", "撤销文件夹操作失败") from exc

            # 无论本次还是此前已完成物理移动，必须完成增量索引更新；失败则保留账本供重试
            try:
                _incremental_update_prefix(rid, curr, orig, is_dir=True)
            except Exception as exc:
                logger.error("撤销文件夹更新索引失败（账本保留以供重试）: %s", exc)
                raise CleanroomException(500, "UNDO_FAILED", "更新索引失败，撤销账本已保留供重试") from exc

            record_file.unlink(missing_ok=True)
            new_version = bump_organize_version()
            return {
                "undone": True,
                "action": action,
                "from": storage.relative_display(curr),
                "to": storage.relative_display(orig),
                "version": new_version,
            }

        if action == "move_file":
            curr_str = str(record.get("abs_source") or record.get("source") or "")
            orig_str = str(record.get("abs_target") or record.get("target") or "")
            curr, curr_root, _ = resolve_writable_path(curr_str, rid)
            orig, orig_root, _ = resolve_writable_path(orig_str, rid)
            if curr.resolve() == curr_root.resolve() or orig.resolve() == orig_root.resolve():
                raise CleanroomException(400, "CANNOT_MOVE_ROOT", "禁止操作挂载根目录本身")

            overwritten_backup = record.get("overwritten_backup")
            overwritten_stashed_index = record.get("overwritten_stashed_index")
            a_restored = bool(record.get("a_restored", False))
            b_restored = bool(record.get("b_restored", False))

            # 前置预检必须先确认 B 的暂存文件存在；curr 此时通常仍是 A，不能据此推断 B 已恢复。
            if overwritten_backup and not b_restored:
                if not isinstance(overwritten_backup, dict):
                    raise CleanroomException(500, "UNDO_FAILED", "覆盖备份记录损坏")
                stashed_raw = overwritten_backup.get("stashed_path")
                if not stashed_raw or not Path(str(stashed_raw)).is_file():
                    pending_target = str(overwritten_backup.get("original_abs_path") or "")
                    try:
                        restored_target_matches = (
                            bool(record.get("b_restore_pending"))
                            and a_restored
                            and curr.is_file()
                            and bool(pending_target)
                            and Path(pending_target).resolve() == curr.resolve()
                        )
                    except (OSError, RuntimeError, ValueError):
                        restored_target_matches = False
                    if not restored_target_matches:
                        raise CleanroomException(500, "UNDO_FAILED", "覆盖暂存文件丢失，无法安全撤销")
                    b_restored = True
                    record["b_restored"] = True
                    record.pop("b_restore_pending", None)

            # 第一阶段：将 A 迁回原位置 orig 并更新 A 的索引
            if not a_restored:
                a_file_moved = bool(record.get("a_file_moved", False))
                if not a_file_moved:
                    if not curr.exists():
                        # 检查是否此前重试已将 A 迁回 orig
                        if orig.exists():
                            a_file_moved = True
                            record["a_file_moved"] = True
                            _save_undo_record(record)
                        else:
                            raise CleanroomException(404, "FILE_NOT_FOUND", "撤销源文件不存在")
                    else:
                        if orig.exists():
                            raise CleanroomException(409, "TARGET_EXISTS", "撤销原目标位置已存在文件")
                        orig.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.move(str(curr), str(orig))
                            a_file_moved = True
                            record["a_file_moved"] = True
                            _save_undo_record(record)
                        except Exception as exc:
                            if _is_file_locked_error(exc):
                                raise CleanroomException(423, "FILE_LOCKED", "文件被占用，无法撤销") from exc
                            logger.error("撤销文件移动失败 %s -> %s: %s", curr, orig, exc)
                            raise CleanroomException(500, "UNDO_FAILED", "撤销文件移动失败") from exc

                # 物理移回 A 成功后更新索引；若失败保留账本供重试，绝不跳过索引
                try:
                    _incremental_update_prefix(rid, curr, orig, is_dir=False)
                    a_restored = True
                    record["a_restored"] = True
                    _save_undo_record(record)
                except Exception as exc:
                    logger.error("撤销文件移动更新索引失败（账本保留以供重试）: %s", exc)
                    raise CleanroomException(500, "UNDO_FAILED", "恢复索引失败，撤销账本已保留供重试") from exc

            # 第二阶段：将原本被覆盖的 B 恢复到 curr（支持重试幂等）
            if overwritten_backup:
                if not b_restored:
                    try:
                        # 先将恢复待续状态写入账本；若物理恢复后最终状态写失败，重试仍能识别已恢复的 B。
                        record["b_restore_pending"] = True
                        _save_undo_record(record)
                        _restore_from_recycler(overwritten_backup, root_id=rid)
                        b_restored = True
                        record["b_restored"] = True
                        record.pop("b_restore_pending", None)
                        _save_undo_record(record)
                    except Exception as exc:
                        logger.error("撤销恢复被覆盖文件失败（账本保留以供重试）: %s", exc)
                        raise CleanroomException(500, "UNDO_FAILED", "恢复被覆盖文件失败，撤销账本已保留供重试") from exc

                if overwritten_stashed_index:
                    try:
                        _incremental_restore_stashed_index(rid, overwritten_stashed_index)
                    except Exception as exc:
                        logger.error("恢复被覆盖文件索引失败（账本保留以供重试）: %s", exc)
                        raise CleanroomException(500, "UNDO_FAILED", "恢复被覆盖文件索引失败，撤销账本已保留供重试") from exc

            # 全部阶段成功后方可销毁撤销账本并递增版本号
            record_file.unlink(missing_ok=True)
            new_version = bump_organize_version()
            return {
                "undone": True,
                "action": action,
                "from": storage.relative_display(curr),
                "to": storage.relative_display(orig),
                "version": new_version,
            }

        if action == "delete":
            stashed_file = record.get("stashed_file")
            stashed_index = record.get("stashed_index")
            if not stashed_file:
                raise CleanroomException(400, "INVALID_REQUEST", "缺少回收站暂存描述")

            target_str = str(record.get("abs_target") or record.get("target") or "")
            target, _, _ = resolve_writable_path(target_str, rid)

            file_restored = bool(record.get("file_restored", False))
            if not file_restored:
                # 检查若物理文件已在 target 存在且暂存文件已不存在，判定物理已恢复
                stashed_raw = stashed_file.get("stashed_path")
                if target.exists() and (not stashed_raw or not Path(str(stashed_raw)).exists()):
                    file_restored = True
                    record["file_restored"] = True
                    _save_undo_record(record)
                else:
                    _restore_from_recycler(stashed_file, root_id=rid)
                    file_restored = True
                    record["file_restored"] = True
                    _save_undo_record(record)

            if stashed_index:
                try:
                    _incremental_restore_stashed_index(rid, stashed_index)
                except Exception as exc:
                    logger.error("撤销删除恢复索引失败（账本保留以供重试）: %s", exc)
                    raise CleanroomException(500, "UNDO_FAILED", "恢复索引失败，撤销账本已保留供重试") from exc

            record_file.unlink(missing_ok=True)
            new_version = bump_organize_version()
            return {
                "undone": True,
                "action": action,
                "target": record.get("target"),
                "version": new_version,
            }

        raise CleanroomException(400, "INVALID_REQUEST", f"不支持的撤销类型：{action}")
