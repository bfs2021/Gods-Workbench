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

"""真实素材索引后台任务；单实例、有限队列、持续授权及提交收据。"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

from gw.core import storage
from gw.core.auth import EDIT_ROLES
from gw.core.config import load_runtime_auth_config
from gw.core.errors import CleanroomException
from gw.core.runtime_paths import (
    DATA_DIR_ENV,
    LOCAL_AUTH_DB_ENV,
    RUNTIME_MODE_ENV,
    VIDEO_DATA_DIR_ENV,
)

MAX_ACTIVE = 16
MAX_JOBS = 1000
MAX_FILES = 250000
MAX_ENTRIES = 500000
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}
)
# 扫描进度落盘节流：除每 25 项外，距上次落盘超过该秒数也落盘。小样本里单个
# 视频探针/哈希可能耗时数秒，只按批次落盘会让运行中的 scanned 恒为 0。
PROGRESS_MIN_INTERVAL_SECONDS = 0.2
PROGRESS_BATCH_SIZE = 25
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
ACTIONS = {
    "pause": {"queued", "running"},
    "resume": {"paused"},
    "cancel": {"queued", "running", "paused"},
    "retry": {"failed", "cancelled", "interrupted"},
}


def _error(status, code, message, *, data_status=None, unavailable=False):
    """抛出受控错误；来源不可用时同时返回明确的降级标记。"""
    extra = {}
    exposed = set()
    if data_status is not None:
        extra["data_status"] = data_status
        exposed.add("data_status")
    if unavailable:
        extra["unavailable"] = True
        exposed.add("unavailable")
    raise CleanroomException(
        status, code, message, extra=extra, expose_extra_fields=exposed or None
    )


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _root_configuration_fingerprint(aliases):
    """只对来源身份配置做指纹；挂载可达性属于运行时状态。"""
    volatile = {"availability", "availability_reason", "availability_message", "alias_candidates"}
    stable = {
        source_id: str(
            {key: value for key, value in alias.items() if key not in volatile}
        )
        for source_id, alias in sorted(aliases.items())
    }
    return _digest(stable)


def _legacy_root_configuration_fingerprint(aliases):
    """读取升级前已入队任务使用的指纹格式。"""
    return _digest({key: str(value) for key, value in sorted(aliases.items())})


def _actor(auth):
    return {
        "subject": auth.subject,
        "mode": auth.mode,
        "domain": auth.identity_domain,
        "role": auth.role,
    }


def _owner(actor):
    return _digest([actor["mode"], actor["domain"], actor["subject"]])


def _runtime_path_configuration():
    return tuple(
        (name, os.environ.get(name))
        for name in (
            RUNTIME_MODE_ENV,
            DATA_DIR_ENV,
            LOCAL_AUTH_DB_ENV,
            VIDEO_DATA_DIR_ENV,
            "LOCALAPPDATA",
        )
    )


def _video_index_data(path: Path) -> tuple[dict, float | None]:
    """为本地视频登记镜头与技术元数据，关键帧逐张解码后立即释放。"""
    decode_keyframe_to_memory = importlib.import_module(
        "gw.media.keyframe_extractor"
    ).decode_keyframe_to_memory
    detect_scenes = importlib.import_module("gw.media.scene_detector").detect_scenes

    scenes = []
    try:
        scenes = detect_scenes(path)
    except CleanroomException as exc:
        return (
            {
                "video_segments": [],
                "video_metadata": {
                    "scene_detection_status": "unavailable",
                    "keyframe_status": "unavailable",
                    "reason_code": exc.code,
                    "source_url": None,
                },
            },
            None,
        )

    if not scenes:
        return (
            {
                "video_segments": [],
                "video_metadata": {
                    "scene_detection_status": "unavailable",
                    "keyframe_status": "unavailable",
                    "reason_code": "SCENE_DETECTION_FAILED",
                    "source_url": None,
                },
            },
            None,
        )

    first = scenes[0]
    duration = scenes[-1]["end_sec"]
    video_metadata = {
        "width": first["width"],
        "height": first["height"],
        "duration_sec": duration,
        "codec": first["codec"],
        "bit_rate": first.get("bit_rate"),
        "container": first.get("container") or path.suffix.lower().lstrip("."),
        "source_url": first.get("source_url"),
        "scene_detection_status": "complete",
        "keyframe_status": "pending",
    }
    try:
        for scene in scenes:
            midpoint = scene["start_sec"] + (
                scene["end_sec"] - scene["start_sec"]
            ) / 2
            keyframe = decode_keyframe_to_memory(path, midpoint)
            del keyframe
    except CleanroomException as exc:
        video_metadata["keyframe_status"] = "unavailable"
        video_metadata["reason_code"] = exc.code
        return (
            {
                "video_segments": scenes,
                "video_metadata": video_metadata,
            },
            duration,
        )

    video_metadata["keyframe_status"] = "decoded_in_memory"
    return {"video_segments": scenes, "video_metadata": video_metadata}, duration


def _roots():
    """解析允许根与来源别名；每个别名保存其根 ID、相对前缀和筛选 ID。"""
    from gw.settings.service import default_storage_settings_service

    roots = storage.allowed_roots()
    canonical = {}
    root_statuses = {}
    root_resolutions = {}
    for root in roots:
        root_id = storage.root_identity_id(root)
        status = storage.probe_mount(str(root))
        resolved_root = storage.resolve_mount_path(str(root)) if status["available"] else None
        if status["available"] and resolved_root is None:
            status = {
                "available": False,
                "reason": "MOUNT_OFFLINE",
                "message": "允许根路径解析检查超时，目录暂时离线",
            }
        path_key = storage.directory_path_key(str(root))
        root_statuses[path_key] = status
        root_resolutions[path_key] = resolved_root
        if root_id not in canonical or (
            not root_statuses[
                storage.directory_path_key(str(canonical[root_id]))
            ]["available"]
            and status["available"]
        ):
            canonical[root_id] = root
    aliases = {}
    for root_id, root in canonical.items():
        status = root_statuses[storage.directory_path_key(str(root))]
        aliases[root_id] = {
            "root_id": root_id,
            "root": root,
            "prefix": "",
            "storage_root_id": root_id,
            "name": root.name or root_id,
            "availability": "available" if status["available"] else "offline",
            "availability_reason": str(status["reason"] or ""),
            "availability_message": str(status["message"] or ""),
        }
    registered_locations = {}
    local_libraries = default_storage_settings_service.get_snapshot().local_libraries
    removed_roots = [
        record
        for record in storage._allowed_roots_state().get("roots", [])
        if not record.get("active") and record.get("path")
    ]

    def removed_root_for_path(path):
        for record in removed_roots:
            try:
                relative = path.relative_to(Path(record["path"]))
            except (OSError, RuntimeError, ValueError):
                continue
            return record, relative
        return None

    def record_unavailable_location(source_id, item, path, status):
        matches = []
        for allowed_root in roots:
            try:
                relative = path.relative_to(allowed_root)
            except ValueError:
                continue
            matches.append((allowed_root, relative))
        if matches:
            root_path, relative = max(matches, key=lambda match: len(match[0].parts))
            root_id = storage.root_identity_id(root_path)
            root = canonical[root_id]
            prefix = "" if str(relative) == "." else relative.as_posix()
            aliases[source_id] = {
                "root_id": root_id,
                "root": root,
                "prefix": prefix,
                "storage_root_id": source_id,
                "name": str(item.get("name") or source_id),
                "availability": "offline",
                "availability_reason": str(status["reason"] or "MOUNT_OFFLINE"),
                "availability_message": str(status["message"] or "目录暂时不可访问"),
            }
            registered_locations[source_id] = {
                "root_id": root_id,
                "prefix": prefix,
                "available": False,
            }
            return
        aliases[source_id] = {
            "root_id": source_id,
            "root": None,
            "prefix": "",
            "storage_root_id": source_id,
            "name": str(item.get("name") or source_id),
            "availability": "offline",
            "availability_reason": str(status["reason"] or "MOUNT_OFFLINE"),
            "availability_message": str(status["message"] or "目录暂时不可访问"),
        }

    for item in local_libraries:
        source_id = str(item.get("id") or "")
        raw_path = str(item.get("path") or "")
        path = Path(raw_path)
        if not source_id:
            continue
        if not raw_path or not path.is_absolute():
            aliases[source_id] = {
                "root_id": source_id,
                "root": None,
                "prefix": "",
                "storage_root_id": source_id,
                "name": str(item.get("name") or source_id),
                "availability": "invalid",
                "availability_reason": "MOUNT_NOT_FOUND",
                "availability_message": "素材来源没有有效的绝对目录路径",
            }
            continue
        removed = removed_root_for_path(path)
        if removed is not None:
            record, relative = removed
            root_id = str(record.get("id") or source_id)
            prefix = "" if str(relative) == "." else relative.as_posix()
            aliases[source_id] = {
                "root_id": root_id,
                "root": None,
                "prefix": prefix,
                "storage_root_id": source_id,
                "name": str(item.get("name") or source_id),
                "availability": "removed",
                "availability_reason": "MOUNT_ROOT_REMOVED",
                "availability_message": "允许根已移除；已有索引与源文件保留",
            }
            registered_locations[source_id] = {
                "root_id": root_id,
                "prefix": prefix,
                "available": False,
            }
            continue
        status = storage.probe_mount(raw_path)
        if not status["available"]:
            record_unavailable_location(source_id, item, path, status)
            continue
        # 路径解析也有界；探测或解析超时均保留离线 facet，不让网络盘卡住索引请求。
        resolved = storage.resolve_mount_path(raw_path)
        if resolved is None:
            record_unavailable_location(
                source_id,
                item,
                path,
                {
                    "reason": "MOUNT_OFFLINE",
                    "message": "素材目录路径解析检查超时，目录暂时离线",
                },
            )
            continue
        matches = []
        for allowed_root in roots:
            root_resolved = root_resolutions.get(
                storage.directory_path_key(str(allowed_root))
            )
            if root_resolved is None:
                continue
            try:
                relative = resolved.relative_to(root_resolved)
            except ValueError:
                continue
            matches.append((allowed_root, relative))
        if not matches:
            suggestions = storage.mount_alias_candidates(raw_path, roots)
            code = "MOUNT_ALIAS_UNCONFIRMED" if suggestions else "MOUNT_OUTSIDE_ALLOWED_ROOTS"
            message = (
                "该路径可能是允许根的映射盘或 UNC 别名，尚未确认"
                if suggestions
                else "该素材来源不在已授权的允许根内"
            )
            aliases[source_id] = {
                "root_id": source_id,
                "root": None,
                "prefix": "",
                "storage_root_id": source_id,
                "name": str(item.get("name") or source_id),
                "availability": "invalid",
                "availability_reason": code,
                "availability_message": message,
                "alias_candidates": suggestions,
            }
            continue
        root_path, relative = max(matches, key=lambda match: len(match[0].parts))
        root_id = storage.root_identity_id(root_path)
        root = canonical[root_id]
        prefix = "" if str(relative) == "." else relative.as_posix()
        alias_record = {
            "root_id": root_id,
            "root": root,
            "prefix": prefix,
            "storage_root_id": source_id,
            "name": str(item.get("name") or source_id),
            "availability": "available" if status["available"] else "offline",
            "availability_reason": str(status["reason"] or ""),
            "availability_message": str(status["message"] or ""),
        }
        if storage.directory_path_key(str(root_path)) != storage.directory_path_key(str(root)):
            alias_record["scan_path"] = resolved
        aliases[source_id] = alias_record
        registered_locations[source_id] = {
            "root_id": root_id,
            "prefix": prefix,
            "available": bool(status["available"]),
        }
    if not roots and not local_libraries:
        _error(
            503,
            "INDEX_SOURCE_NOT_AVAILABLE",
            "未配置允许根目录",
            data_status="not_integrated",
            unavailable=True,
        )
    # 已确认的路径别名仅作为同一登记来源的离线恢复通道；未确认候选不参与索引。
    from gw.asset_registry import repository as repo

    bound_aliases = repo.state().read().get("root_aliases", {})
    for item in local_libraries:
        source_id = str(item.get("id") or "")
        if not source_id:
            continue
        registered = registered_locations.get(source_id)
        # 登记目录仍可访问时只使用登记路径；已离线时才考虑确认过的别名。
        if source_id in aliases and (
            registered is None or registered["available"]
        ):
            continue
        bound = bound_aliases.get(source_id)
        bound_root_id = str(bound.get("root_id") or "") if isinstance(bound, dict) else ""
        if not bound_root_id:
            continue
        paths = bound.get("aliases", [])
        if not isinstance(paths, list):
            continue
        for raw_alias_path in sorted(str(value) for value in paths if value):
            try:
                alias_path = Path(raw_alias_path).resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if not storage.probe_mount(str(alias_path))["available"]:
                continue

            if registered is not None:
                # 离线来源的身份与 canonical 前缀必须仍与持久绑定完全一致。
                if bound_root_id != registered["root_id"]:
                    continue
                bound_prefix = bound.get("canonical_relpath")
                if not isinstance(bound_prefix, str):
                    continue
                try:
                    expected_prefix = (
                        storage.canonical_relpath(registered["prefix"])
                        if registered["prefix"]
                        else ""
                    )
                    confirmed_prefix = (
                        storage.canonical_relpath(bound_prefix) if bound_prefix else ""
                    )
                except CleanroomException:
                    continue
                root = canonical.get(bound_root_id)
                if (
                    confirmed_prefix != expected_prefix
                    or root is None
                ):
                    continue
                aliases[source_id] = {
                    "root_id": bound_root_id,
                    "root": root,
                    "prefix": registered["prefix"],
                    "scan_path": alias_path,
                    "storage_root_id": source_id,
                    "name": str(item.get("name") or source_id),
                }
                break

            # 旧绑定路径位于 allowed root 内时保留原有支持，不把外部路径升格为 allowed root。
            matches = []
            for allowed_root in roots:
                try:
                    relative = alias_path.relative_to(allowed_root.resolve())
                except (OSError, RuntimeError, ValueError):
                    continue
                matches.append((allowed_root, relative))
            if not matches:
                continue
            root, relative = max(matches, key=lambda match: len(match[0].parts))
            existing_root = canonical.get(bound_root_id)
            if existing_root is None or not existing_root.is_dir():
                canonical[bound_root_id] = root
            aliases[source_id] = {
                "root_id": bound_root_id,
                "root": root,
                "prefix": "" if str(relative) == "." else relative.as_posix(),
                "storage_root_id": source_id,
                "name": str(item.get("name") or source_id),
            }
            break
    return canonical, aliases


def root_facets():
    """返回可筛选来源及资产数，不暴露服务器绝对路径。"""
    from gw.asset_registry import repository as repo

    try:
        _, aliases = _roots()
    except CleanroomException:
        return []
    raw = repo.state().read()
    counts = {}
    for asset in raw["assets"].values():
        if not repo._visible(asset):
            continue
        source_id = str(asset.get("storage_root_id") or asset.get("root_id") or "")
        if source_id:
            counts[source_id] = counts.get(source_id, 0) + 1
    facets = {}
    for source_id, alias in sorted(aliases.items()):
        availability = str(alias.get("availability") or "available")
        reason = str(alias.get("availability_reason") or "")
        message = str(alias.get("availability_message") or "")
        if availability != "invalid" and alias.get("root") is not None:
            path = (
                Path(alias["scan_path"])
                if alias.get("scan_path") is not None
                else alias["root"].joinpath(*Path(alias.get("prefix") or ".").parts)
            )
            status = storage.probe_mount(str(path))
            availability = "available" if status["available"] else "offline"
            reason = str(status["reason"] or "")
            message = str(status["message"] or "")
        facets[source_id] = {
            "id": source_id,
            "name": alias["name"],
            "count": counts.get(source_id, 0),
            "availability": availability,
            "availability_reason": reason,
            "availability_message": message,
        }
    removed_root_ids = {
        str(record.get("id") or "")
        for record in storage._allowed_roots_state().get("roots", [])
        if not record.get("active")
    }
    for source_id, count in counts.items():
        if source_id in removed_root_ids and source_id not in facets:
            facets[source_id] = {
                "id": source_id,
                "name": source_id,
                "count": count,
                "availability": "removed",
                "availability_reason": "MOUNT_ROOT_REMOVED",
                "availability_message": "允许根已移除；已有索引与源文件保留",
            }
    return sorted(facets.values(), key=lambda item: item["id"])


def _admission(actor):
    """只检查服务器本地可撤销授权；不把会话有效冒称远端IdP实时授权。"""
    runtime = load_runtime_auth_config()
    if runtime.mode != actor["mode"]:
        _error(403, "INDEX_AUTH_CHANGED", "认证配置已改变，任务停止")
    if actor["mode"] == "local_account":
        from gw.core.local_accounts import database

        with database() as db:
            row = db.execute(
                "SELECT role FROM local_users WHERE user_id=?", (actor["subject"],)
            ).fetchone()
            live = db.execute(
                "SELECT 1 FROM local_sessions WHERE user_id=? "
                "AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
                (actor["subject"], time.time()),
            ).fetchone()
        if not row or row["role"] not in EDIT_ROLES or not live:
            _error(403, "INDEX_AUTH_REVOKED", "发起账户写权限或会话已失效")
    elif actor["mode"] == "oidc":
        from gw.core import session

        if actor["domain"] != "oidc:" + runtime.oidc.issuer:
            _error(403, "INDEX_AUTH_CHANGED", "身份域已改变")
        if not any(
            principal.get("role") in EDIT_ROLES
            for principal in session.get_sessions_for_subject(actor["subject"])
        ):
            _error(
                403, "INDEX_SESSION_REQUIRED", "后台索引需要持续有效的服务端登录会话"
            )
    elif actor["role"] not in EDIT_ROLES:
        _error(403, "INDEX_AUTH_REVOKED", "发起主体无写权限")


class _JobState(storage.JsonState):
    """任务真源损坏时失败关闭，不能回退空表复用稳定ID。"""

    def read(self):
        path = storage.data_root() / "registry_index_jobs.json"
        if not path.exists():
            return {"sequence": 0, "jobs": {}, "keys": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if (
                type(raw["sequence"]) is not int
                or not isinstance(raw["jobs"], dict)
                or not isinstance(raw["keys"], dict)
            ):
                raise ValueError()
            return raw
        except (OSError, ValueError, KeyError, TypeError):
            _error(503, "INDEX_STORE_INVALID", "索引任务持久状态不可读取")


class IndexJobs:
    """进度独立持久化；取消/暂停和注册表提交在同一线程锁内线性化。"""

    def __init__(self):
        self.root = storage.data_root().resolve()
        self.store = _JobState(
            "registry_index_jobs", lambda: {"sequence": 0, "jobs": {}, "keys": {}}
        )
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gw-index")
        self._root_snapshot_cache = {}
        self._runtime_path_configuration = _runtime_path_configuration()
        self.closed = False
        self._recover()

    def _recover(self):
        from gw.asset_registry import repository as repo

        receipts = repo.state().read().get("index_job_receipts", {})

        def restore(raw):
            for job in raw["jobs"].values():
                receipt = receipts.get(f"{job['job_id']}:{job['attempt']}")
                if receipt:
                    job.update(
                        status="succeeded",
                        phase="completed",
                        result=receipt,
                        error_code=None,
                    )
                elif job["status"] not in TERMINAL:
                    job.update(
                        status="interrupted",
                        phase="interrupted",
                        error_code="INDEX_PROCESS_INTERRUPTED",
                    )
                else:
                    continue
                job["version"] += 1
                job["updated_at"] = storage.now_iso()

        self.store.mutate(restore)

    def shutdown(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _root_snapshot_key(self):
        """廉价探测根配置快照是否改变，避免每个 NAS 文件都重新 resolve 全部根。"""
        try:
            state_files = []
            for namespace in ("storage_settings", "asset_registry", "allowed_roots"):
                path = self.root / f"{namespace}.json"
                try:
                    info = path.stat()
                except FileNotFoundError:
                    state_files.append(None)
                else:
                    state_files.append((info.st_mtime_ns, info.st_size))
            return _digest(
                {
                    "allowed_roots": os.environ.get(storage.ALLOWED_ROOTS_ENV, ""),
                    "state_files": state_files,
                }
            )
        except (CleanroomException, OSError, ValueError):
            return None

    def _check(self, job):
        if (
            self.closed
            or _runtime_path_configuration() != self._runtime_path_configuration
        ):
            _error(409, "INDEX_PROCESS_INTERRUPTED", "任务执行环境已停止")
        _admission(job["_actor"])
        cache_key = self._root_snapshot_key()
        cached = self._root_snapshot_cache.get(cache_key) if cache_key else None
        if cached is None:
            canonical, aliases = _roots()
            fingerprint = _root_configuration_fingerprint(aliases)
            legacy_fingerprint = _legacy_root_configuration_fingerprint(aliases)
            if cache_key:
                self._root_snapshot_cache[cache_key] = (
                    fingerprint,
                    legacy_fingerprint,
                    canonical,
                )
                if len(self._root_snapshot_cache) > MAX_ACTIVE:
                    self._root_snapshot_cache.pop(next(iter(self._root_snapshot_cache)))
        else:
            fingerprint, legacy_fingerprint, canonical = cached
        if job["_fingerprint"] not in {fingerprint, legacy_fingerprint}:
            _error(403, "INDEX_ROOTS_CHANGED", "允许根或来源配置已改变")
        return canonical

    def _job(self, job_id, auth):
        job = self.store.read()["jobs"].get(job_id)
        if not job or job["_owner"] != _owner(_actor(auth)):
            _error(404, "WORKSPACE_JOB_NOT_FOUND", "工作区任务不存在")
        return self._committed_truth(job)

    def _committed_truth(self, job):
        """业务提交收据高于任务快照；写状态失败也绝不开放取消或重试。"""
        from gw.asset_registry import repository as repo

        receipt = (
            repo.state()
            .read()
            .get("index_job_receipts", {})
            .get(f"{job['job_id']}:{job['attempt']}")
        )
        if receipt is not None and job["status"] != "succeeded":
            job.update(
                status="succeeded",
                phase="completed",
                result=receipt,
                error_code=None,
            )
            job["version"] += 1
            # 状态盘仍故障时按已持久化收据投影成功；动作判断使用同一投影。
            with suppress(OSError):
                self._save(job)
        return job

    def _save(self, job):
        job["updated_at"] = storage.now_iso()
        self.store.mutate(lambda raw: raw["jobs"].__setitem__(job["job_id"], job))

    @staticmethod
    def public(job):
        public = {k: v for k, v in job.items() if not k.startswith("_")}
        status = str(job.get("status") or "unknown")
        error_code = str(job.get("error_code") or "")
        if status == "succeeded":
            projection = {
                "data_status": "ok",
                "data_gaps": [],
                "next_action": "review_result",
                "result_unknown": False,
            }
        elif status in {"queued", "running"}:
            projection = {
                "data_status": "degraded",
                "data_gaps": ["INDEX_PENDING"],
                "next_action": "poll_job",
                "result_unknown": False,
            }
        elif status == "paused":
            projection = {
                "data_status": "degraded",
                "data_gaps": ["INDEX_PAUSED"],
                "next_action": "resume_index",
                "result_unknown": False,
            }
        elif status == "failed":
            projection = {
                "data_status": "degraded",
                "data_gaps": [error_code or "INDEX_EXECUTION_FAILED"],
                "next_action": "retry_index",
                "result_unknown": False,
            }
        elif status == "cancelled":
            projection = {
                "data_status": "degraded",
                "data_gaps": [error_code or "INDEX_CANCELLED"],
                "next_action": "retry_index",
                "result_unknown": False,
            }
        elif status == "interrupted":
            projection = {
                "data_status": "degraded",
                "data_gaps": [error_code or "INDEX_PROCESS_INTERRUPTED"],
                "next_action": "reconcile_index_job",
                "result_unknown": True,
            }
        else:
            projection = {
                "data_status": "degraded",
                "data_gaps": ["INDEX_STATUS_UNKNOWN"],
                "next_action": "reconcile_index_job",
                "result_unknown": True,
            }
        public.update(projection)
        public["has_error"] = bool(job.get("error_code"))
        public["progress"] = {
            "scanned": job["scanned"],
            "error_count": job["error_count"],
            "phase": job.get("phase")
            or {
                "queued": "queued",
                "running": "scanning",
                "succeeded": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "interrupted": "interrupted",
            }.get(status, status),
        }
        return public

    def detail(self, job_id, auth):
        with self.lock:
            job = self._job(job_id, auth)
            writable = auth.role in EDIT_ROLES
            actions = {
                a: writable
                and job["status"] in states
                and (a != "retry" or job["attempt"] < 5)
                for a, states in ACTIONS.items()
            }
            view = self.public(job)
            return {
                "job": view,
                "actions": actions,
                "action_reasons": {
                    a: "当前状态或权限不允许此操作"
                    for a, ok in actions.items()
                    if not ok
                },
                "data_status": view["data_status"],
                "data_gaps": view["data_gaps"],
                "next_action": view["next_action"],
                "result_unknown": view["result_unknown"],
            }

    def listing(self, auth):
        with self.lock:
            owner = _owner(_actor(auth))
            jobs = [
                self.public(self._committed_truth(j))
                for j in self.store.read()["jobs"].values()
                if j["_owner"] == owner
            ]
            return sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:100]

    def submit(self, kind, payload, auth, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 160:
            _error(
                400,
                "INDEX_IDEMPOTENCY_KEY_REQUIRED",
                "后台索引需要不超过160字符的幂等键",
            )
        actor = _actor(auth)
        _admission(actor)
        canonical, aliases = _roots()
        selected = payload.get("roots")
        if selected is not None and (
            not isinstance(selected, list)
            or not selected
            or len(selected) > 64
            or any(not isinstance(x, str) or x not in aliases for x in selected)
        ):
            _error(400, "INDEX_ROOT_INVALID", "来源必须为已准入的根标识")
        invalid_aliases = [
            aliases[source_id]
            for source_id in (selected or aliases.keys())
            if source_id in aliases and aliases[source_id].get("availability") == "invalid"
        ]
        if invalid_aliases:
            invalid = invalid_aliases[0]
            _error(
                409,
                str(invalid.get("availability_reason") or "MOUNT_INVALID"),
                str(invalid.get("availability_message") or "索引来源路径无效"),
            )
        selected_aliases = (
            [
                alias
                for alias in aliases.values()
                if alias["storage_root_id"] == alias["root_id"]
                and alias.get("availability") != "invalid"
            ]
            if selected is None
            else [aliases[source_id] for source_id in selected]
        )
        if not selected_aliases:
            _error(503, "INDEX_SOURCE_NOT_AVAILABLE", "没有可用的已登记索引来源", unavailable=True)
        scopes = [
            {
                "root_id": alias["root_id"],
                "prefix": alias["prefix"],
                "storage_root_id": alias["storage_root_id"],
                **(
                    {"scan_path": str(alias["scan_path"])}
                    if alias.get("scan_path") is not None
                    else {}
                ),
            }
            for alias in selected_aliases
        ]
        # 同一来源可能通过多个别名被重复选中，合并完全相同的扫描范围。
        scopes = list(
            {
                (scope["root_id"], scope["prefix"], scope["storage_root_id"]): scope
                for scope in scopes
            }.values()
        )
        root_ids = sorted({scope["root_id"] for scope in scopes})
        max_files = payload.get("max_files", MAX_FILES)
        if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
            _error(400, "INDEX_LIMIT_INVALID", "扫描文件数上限无效")
        hash_files = payload.get("hash_files", False)
        if not isinstance(hash_files, bool):
            _error(400, "INDEX_HASH_INVALID", "hash_files必须为布尔值")
        request = {
            "kind": kind,
            "roots": root_ids,
            "scopes": scopes,
            "legacy_identity_scan_complete": selected is None,
            "max_files": max_files,
            "hash_files": hash_files,
        }
        fingerprint = _root_configuration_fingerprint(aliases)
        request_digest = _digest([request, fingerprint])
        key_digest = _digest([_owner(actor), key])
        with self.condition:
            raw = self.store.read()
            previous = raw["keys"].get(key_digest)
            if previous:
                job = self._committed_truth(raw["jobs"][previous])
                if job["_request_digest"] != request_digest:
                    _error(
                        409, "INDEX_IDEMPOTENCY_CONFLICT", "幂等键已用于不同索引请求"
                    )
                return self._accepted(job)
            if (
                len(raw["jobs"]) >= MAX_JOBS
                or sum(j["status"] not in TERMINAL for j in raw["jobs"].values())
                >= MAX_ACTIVE
            ):
                _error(429, "INDEX_QUEUE_FULL", "索引任务容量已满")
            raw["sequence"] += 1
            job_id = f"job_index_{raw['sequence']:08d}"
            job = {
                "job_id": job_id,
                "job_type": "asset.index" if kind == "reindex" else "asset.index.sync",
                "name": "重建素材索引" if kind == "reindex" else "同步索引差集",
                "status": "queued",
                "phase": "queued",
                "version": 1,
                "attempt": 1,
                "max_attempts": 5,
                "created_at": storage.now_iso(),
                "updated_at": storage.now_iso(),
                "scanned": 0,
                "error_count": 0,
                "result": None,
                "error_code": None,
                "_actor": actor,
                "_owner": _owner(actor),
                "_fingerprint": fingerprint,
                "_request": request,
                "_request_digest": request_digest,
            }
            raw["jobs"][job_id] = job
            raw["keys"][key_digest] = job_id
            self.store.write(raw)
            self._dispatch(job)
            return self._accepted(job)

    def _dispatch(self, job):
        try:
            self.executor.submit(self._run, job["job_id"], job["attempt"])
        except RuntimeError:
            job.update(
                status="failed",
                phase="failed",
                error_code="INDEX_EXECUTOR_UNAVAILABLE",
            )
            job["version"] += 1
            self._save(job)

    def _accepted(self, job):
        view = self.public(job)
        return {
            "job_id": job["job_id"],
            "job": view,
            "poll_hint": {
                "url": "/api/asset-registry/workspace-jobs/" + job["job_id"],
                "interval_ms": 500,
            },
            "data_status": view["data_status"],
            "data_gaps": view["data_gaps"],
            "next_action": view["next_action"],
            "result_unknown": view["result_unknown"],
        }

    def operate(self, job_id, action, payload, auth):
        if action not in ACTIONS:
            _error(400, "INVALID_ACTION", "不支持的任务操作")
        with self.condition:
            job = self._job(job_id, auth)
            expected = payload.get("expected_version")
            if type(expected) is not int or expected < 1:
                _error(
                    400, "EXPECTED_VERSION_REQUIRED", "任务操作必须携带expected_version"
                )
            if expected != job["version"]:
                _error(409, "VERSION_CONFLICT", "任务版本已改变，请重新读取")
            self._check(job)
            if job["status"] not in ACTIONS[action] or (
                action == "retry" and job["attempt"] >= 5
            ):
                _error(409, "INDEX_ACTION_CONFLICT", "当前任务状态不允许此操作")
            if action == "retry":
                if (
                    sum(
                        j["status"] not in TERMINAL
                        for j in self.store.read()["jobs"].values()
                    )
                    >= MAX_ACTIVE
                ):
                    _error(429, "INDEX_QUEUE_FULL", "索引任务容量已满")
                job.update(
                    status="queued",
                    phase="queued",
                    attempt=job["attempt"] + 1,
                    scanned=0,
                    error_count=0,
                    result=None,
                    error_code=None,
                )
            else:
                job["status"] = {
                    "pause": "paused",
                    "resume": "running",
                    "cancel": "cancelled",
                }[action]
                if action == "cancel":
                    job["phase"] = "cancelled"
            job["version"] += 1
            self._save(job)
            self.condition.notify_all()
            if action == "retry":
                self._dispatch(job)
            return self.detail(job_id, auth)

    def _checkpoint(self, job_id, attempt):
        with self.condition:
            while True:
                job = self.store.read()["jobs"][job_id]
                if job["attempt"] != attempt or job["status"] in TERMINAL:
                    raise _Stopped()
                self._check(job)
                if job["status"] != "paused":
                    return job
                self.condition.wait(0.2)

    def _run(self, job_id, attempt):
        try:
            with self.condition:
                job = self.store.read()["jobs"][job_id]
                if job["attempt"] != attempt or job["status"] in TERMINAL:
                    return
                if job["status"] == "queued":
                    job["status"] = "running"
                    job["phase"] = "scanning"
                    job["version"] += 1
                    self._save(job)
            job = self._checkpoint(job_id, attempt)
            roots = self._check(job)
            files = self._scan(job_id, attempt, roots, job["_request"])
            with self.condition:
                job = self._checkpoint(job_id, attempt)
                job.update(phase="indexing", scanned=len(files))
                self._save(job)
                if job["error_count"]:
                    _error(
                        503,
                        "INDEX_FILE_ERRORS",
                        "扫描存在不可读取文件，未提交不完整索引",
                    )
                # 提交必须与取消/暂停在同一线程锁内线性化：
                # test_commit_wins_cancel_never_reports_cancelled 覆盖该不变量，
                # 不能在提交期间放开任务锁。
                result = self._commit(job, files)
                job.update(
                    status="succeeded",
                    phase="completed",
                    result=result,
                    scanned=len(files),
                    error_code=None,
                )
                job["version"] += 1
                self._save(job)
        except _Stopped:
            return
        except Exception as exc:
            with self.condition:
                if storage.data_root().resolve() != self.root:
                    return
                job = self.store.read()["jobs"].get(job_id)
                if not job or job["attempt"] != attempt or job["status"] in TERMINAL:
                    return
                # 注册表已提交而任务状态落盘失败时，以同事务收据为准。
                from gw.asset_registry import repository as repo

                receipt = (
                    repo.state()
                    .read()
                    .get("index_job_receipts", {})
                    .get(f"{job_id}:{attempt}")
                )
                job.update(
                    status="succeeded"
                    if receipt
                    else ("interrupted" if self.closed else "failed"),
                    phase="completed"
                    if receipt
                    else ("interrupted" if self.closed else "failed"),
                    result=receipt,
                    error_code=None
                    if receipt
                    else (
                        exc.code
                        if isinstance(exc, CleanroomException)
                        else "INDEX_EXECUTION_FAILED"
                    ),
                )
                job["version"] += 1
                # 恢复时只相信注册表收据，禁止重新提交。
                with suppress(Exception):
                    self._save(job)

    def _children(self, job_id, attempt, folder, root, *, hash_files=False, resolved_root=None):
        # 目录项枚举与元数据读取共享一次检查点，paused 后不继续读取新条目。
        with self.condition:
            self._checkpoint(job_id, attempt)
            resolved_folder = folder.resolve()
            if resolved_root is None:
                resolved_root = root.resolve()
            mapped_drive_path = storage._is_network_path(str(folder))
            inside_resolved_root = (
                resolved_folder == resolved_root
                or resolved_folder.is_relative_to(resolved_root)
            )
            if (
                (resolved_folder != folder.absolute() and not mapped_drive_path)
                or not inside_resolved_root
            ):
                _error(403, "INDEX_PATH_CHANGED", "扫描目录已改变")
            iterator = os.scandir(folder)
        try:
            while True:
                with self.condition:
                    self._checkpoint(job_id, attempt)
                    try:
                        child = next(iterator)
                    except StopIteration:
                        break
                    try:
                        # 哈希流程需要真实 inode/dev 与打开句柄比对；普通扫描使用
                        # DirEntry 的枚举元数据，避免每个 NAS 文件再单独 lstat。
                        info = (
                            Path(child.path).lstat()
                            if hash_files
                            else child.stat(follow_symlinks=False)
                        )
                        is_symlink = child.is_symlink()
                        stat_error = None
                    except OSError as exc:
                        info = None
                        is_symlink = False
                        stat_error = exc
                yield child, info, is_symlink, stat_error
        finally:
            iterator.close()

    def _hash(self, job_id, attempt, path, expected):
        with self.condition:
            self._checkpoint(job_id, attempt)
            # 与 scandir 分支一致：映射盘上 resolve() 返回 UNC 而 absolute() 保留
            # 盘符，形态差本身不代表路径被篡改，故网络路径不按形态判失败。
            # 真实变更仍由下方打开句柄后的 inode/dev/size 比对失败关闭。
            if (
                path.resolve() != path.absolute()
                and not storage._is_network_path(str(path))
            ):
                _error(403, "INDEX_PATH_CHANGED", "摘要源路径已改变")
            handle = path.open("rb")
            actual = os.fstat(handle.fileno())
            if (actual.st_ino, actual.st_dev, actual.st_size) != (
                expected.st_ino,
                expected.st_dev,
                expected.st_size,
            ):
                handle.close()
                _error(409, "INDEX_FILE_CHANGED", "摘要源文件已改变")
        digest = hashlib.sha256()
        read_bytes = 0
        try:
            while True:
                with self.condition:
                    self._checkpoint(job_id, attempt)
                    chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > 512 * 1024 * 1024:
                    _error(413, "INDEX_HASH_LIMIT", "单个文件超过摘要计算上限")
                digest.update(chunk)
            final = os.fstat(handle.fileno())
            if (final.st_size, final.st_mtime_ns) != (
                expected.st_size,
                expected.st_mtime_ns,
            ):
                _error(409, "INDEX_FILE_CHANGED", "摘要计算期间文件已改变")
        finally:
            handle.close()
        return digest.hexdigest()

    def _scan(self, job_id, attempt, roots, request):
        files = []
        entries = 0
        progress_saved_at = time.monotonic()
        scopes = request.get("scopes") or [
            {"root_id": root_id, "prefix": "", "storage_root_id": root_id}
            for root_id in request["roots"]
        ]
        for scope in scopes:
            root_id = scope["root_id"]
            root = roots[root_id]
            prefix = scope.get("prefix") or ""
            scan_path = scope.get("scan_path")
            scan_root = (
                Path(scan_path)
                if scan_path is not None
                else root.joinpath(*Path(prefix or ".").parts)
            )
            boundary = scan_root if scan_path is not None else root
            with self.condition:
                self._checkpoint(job_id, attempt)
                try:
                    scan_root_resolved = scan_root.resolve(strict=True)
                    root_resolved = root.resolve(strict=True) if scan_path is None else None
                except (OSError, RuntimeError, ValueError):
                    scan_root_resolved = None
                    root_resolved = None
                mapped_drive_path = storage._is_network_path(str(scan_root))
                if (
                    not scan_root.is_dir()
                    or scan_root_resolved is None
                    or (
                        scan_path is not None
                        and scan_root_resolved != scan_root.absolute()
                    )
                    or (
                        scan_path is None
                        and (
                            root_resolved is None
                            or not (
                                scan_root_resolved == root_resolved
                                or scan_root_resolved.is_relative_to(root_resolved)
                            )
                            or (
                                scan_root_resolved != scan_root.absolute()
                                and not mapped_drive_path
                            )
                        )
                    )
                ):
                    _error(
                        503,
                        "INDEX_ROOT_UNAVAILABLE",
                        "索引来源目录不可用",
                        data_status="not_integrated",
                        unavailable=True,
                    )
            pending = [scan_root]
            while pending:
                folder = pending.pop()
                for child, info, is_symlink, stat_error in self._children(
                    job_id,
                    attempt,
                    folder,
                    boundary,
                    hash_files=request["hash_files"],
                    resolved_root=(
                        scan_root_resolved if scan_path is not None else root_resolved
                    ),
                ):
                    entries += 1
                    if entries > MAX_ENTRIES:
                        _error(413, "INDEX_SCAN_LIMIT", "扫描目录项数量超过上限")
                    try:
                        if stat_error is not None:
                            raise stat_error
                        if info is None:
                            continue
                        if is_symlink or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        path = Path(child.path)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(path)
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        if len(files) >= request["max_files"]:
                            _error(413, "INDEX_SCAN_LIMIT", "扫描文件数量超过上限")
                        relative_path = path.relative_to(boundary).as_posix()
                        display_path = (
                            "/".join(
                                part
                                for part in (prefix, relative_path)
                                if part and part != "."
                            )
                            if scan_path is not None
                            else relative_path
                        )
                        item = {
                            "root_id": root_id,
                            "storage_root_id": scope.get("storage_root_id")
                            or root_id,
                            "display_path": display_path,
                            "canonical_relpath": storage.canonical_relpath(display_path),
                            "name": path.name,
                            "size_bytes": info.st_size,
                        }
                        if path.suffix.lower() in VIDEO_SUFFIXES:
                            video_metadata, duration = _video_index_data(path)
                            item["metadata"] = video_metadata
                            if duration is not None:
                                item["duration"] = duration
                        if request["hash_files"]:
                            if info.st_size > 512 * 1024 * 1024:
                                _error(
                                    413, "INDEX_HASH_LIMIT", "单个文件超过摘要计算上限"
                                )
                            item["sha256"] = self._hash(job_id, attempt, path, info)
                        files.append(item)
                        now = time.monotonic()
                        if (
                            len(files) % PROGRESS_BATCH_SIZE == 0
                            or now - progress_saved_at >= PROGRESS_MIN_INTERVAL_SECONDS
                        ):
                            # 进度落盘是尽力而为：状态盘用 tmp + os.replace 原子写入，
                            # Windows 上若恰有并发读取（轮询进度）正在打开目标文件，
                            # 替换会抛 PermissionError。这里跳过本个节流点、下一批继续，
                            # 绝不把状态盘抖动算成素材文件不可读而整单失败（INDEX_FILE_ERRORS）。
                            with suppress(OSError):
                                with self.condition:
                                    job = self._checkpoint(job_id, attempt)
                                    job["scanned"] = len(files)
                                    self._save(job)
                            progress_saved_at = now
                    except OSError:
                        with self.condition:
                            job = self._checkpoint(job_id, attempt)
                            job["error_count"] += 1
                            self._save(job)
                            if job["error_count"] >= 100:
                                _error(
                                    503, "INDEX_FILE_ERRORS", "不可读取文件数量超过上限"
                                )
        return files

    def _commit(self, job, files):
        from gw.asset_registry import repository as repo

        self._check(job)
        receipt_id = f"{job['job_id']}:{job['attempt']}"

        def commit(raw):
            self._check(job)
            receipts = raw.setdefault("index_job_receipts", {})
            if receipt_id in receipts:
                return receipts[receipt_id]
            by_relpath = {}
            for item in files:
                by_relpath.setdefault(item["canonical_relpath"], []).append(item)
            repair_queue = raw.setdefault("identity_repair_queue", [])
            if not isinstance(repair_queue, list):
                repair_queue = raw["identity_repair_queue"] = []
            known = {}
            for asset in raw["assets"].values():
                display_path = asset.get("display_path")
                asset_root_id = asset.get("root_id")
                canonical_relpath = asset.get("canonical_relpath")
                if display_path and not canonical_relpath:
                    try:
                        canonical_relpath = storage.canonical_relpath(str(display_path))
                    except CleanroomException:
                        canonical_relpath = None
                if not asset_root_id:
                    matches = by_relpath.get(canonical_relpath or "", [])
                    if (
                        job["_request"].get("legacy_identity_scan_complete")
                        and len(matches) == 1
                    ):
                        asset_root_id = matches[0]["root_id"]
                        asset["root_id"] = asset_root_id
                        asset["storage_root_id"] = matches[0]["storage_root_id"]
                    else:
                        asset["identity_status"] = "ambiguous"
                        asset_id = str(asset.get("asset_id") or asset.get("id") or "")
                        if asset_id and asset_id not in repair_queue:
                            repair_queue.append(asset_id)
                        continue
                if canonical_relpath:
                    asset["canonical_relpath"] = canonical_relpath
                    known[(asset_root_id, canonical_relpath)] = asset
            disk = {(x["root_id"], x["canonical_relpath"]) for x in files}
            added = 0
            updated = 0
            if job["_request"]["kind"] == "reindex":
                search_documents = {}
                next_asset_number = repo._asset_sequence_start(raw)
                for item in files:
                    identity = (item["root_id"], item["canonical_relpath"])
                    existing = known.get(identity)
                    if existing is not None:
                        changed = False
                        for field in (
                            "storage_root_id",
                            "canonical_relpath",
                            "name",
                            "size_bytes",
                            "sha256",
                        ):
                            if field in item and existing.get(field) != item[field]:
                                existing[field] = item.get(field)
                                changed = True
                        if "metadata" in item:
                            current_metadata = existing.get("metadata")
                            if not isinstance(current_metadata, dict):
                                current_metadata = existing["metadata"] = {}
                            scanned_metadata = item["metadata"]
                            scanned_video = scanned_metadata.get("video_metadata", {})
                            current_video = current_metadata.get("video_metadata", {})
                            if (
                                scanned_video.get("scene_detection_status") == "complete"
                                or not current_video
                            ):
                                for field in ("video_segments", "video_metadata"):
                                    if current_metadata.get(field) != scanned_metadata.get(
                                        field
                                    ):
                                        current_metadata[field] = scanned_metadata.get(
                                            field
                                        )
                                        changed = True
                        if "duration" in item and existing.get("duration") != item[
                            "duration"
                        ]:
                            existing["duration"] = item["duration"]
                            changed = True
                        if changed:
                            existing["updated_at"] = storage.now_iso()
                            updated += 1
                        asset_id = str(existing.get("asset_id") or existing.get("id") or "")
                        if asset_id:
                            search_documents[asset_id] = existing
                        continue
                    asset_id = f"ast_{next_asset_number:04d}"
                    next_asset_number += 1
                    record = repo._plain_asset(
                        asset_id,
                        item["name"],
                        "indexed",
                        **{k: v for k, v in item.items() if k != "name"},
                    )
                    raw["assets"][asset_id] = record
                    known[identity] = record
                    search_documents[asset_id] = record
                    added += 1
                if added or updated:
                    repo._bump(raw)
                repo._upsert_asset_search_documents(search_documents.values())
                result = {
                    "indexed": added,
                    "updated": updated,
                    "scanned": len(files),
                    "revision": raw["revision"],
                }
            else:
                scoped = {x for x in known if x[0] in job["_request"]["roots"]}
                result = {
                    "on_disk": len(disk),
                    "registered": len(scoped),
                    "missing_from_registry": len(disk - scoped),
                    "missing_from_disk": len(scoped - disk),
                    "revision": raw["revision"],
                }
            receipts[receipt_id] = result
            return result

        return repo.state().mutate(commit)


class _Stopped(Exception):
    """取消或新尝试已接管，不落失败状态覆盖新任务。"""


_default = None
_default_lock = threading.Lock()


def service():
    global _default
    with _default_lock:
        root = storage.data_root().resolve()
        if _default is None or _default.root != root or _default.closed:
            if _default is not None:
                _default.shutdown()
            _default = IndexJobs()
        return _default


def shutdown():
    """应用关闭只停止自身索引线程，不清理任务、资产和用户文件。"""
    if _default is not None:
        _default.shutdown()
