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

"""官方 libjxl 命令探测与旧 JPEG 无损转码任务。"""

from __future__ import annotations

import contextvars
import hashlib
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from gw.core import storage
from gw.core.errors import CleanroomException

JOBS_NAMESPACE = "jxl_lossless_transcode_jobs"
OFFICIAL_RELEASES_URL = "https://github.com/libjxl/libjxl/releases"
PROBE_TIMEOUT_SECONDS = 5
TRANSCODE_TIMEOUT_SECONDS = 7200
MAX_ASSETS_PER_JOB = 100_000
MAX_ACTIVE_JOBS = 2
MAX_STORED_JOBS = 1_000
MAX_RESULT_ITEMS = 200
PROGRESS_BATCH_SIZE = 25
PROGRESS_MIN_INTERVAL_SECONDS = 0.5
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
JXL_SIGNATURES = (b"\x00\x00\x00\x0cJXL \r\n\x87\n", b"\xff\x0a")


def _blank_jobs() -> dict[str, Any]:
    return {"schema_version": 1, "sequence": 0, "jobs": {}}


def _store() -> storage.JsonState:
    return storage.JsonState(JOBS_NAMESPACE, _blank_jobs)


def _root_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _tool_names(name: str) -> tuple[str, ...]:
    if os.name == "nt":
        return (name + ".exe", name)
    return (name,)


def _find_tool(name: str) -> tuple[str | None, str | None]:
    for executable in _tool_names(name):
        found = shutil.which(executable)
        if found:
            return found, "PATH"
    tool_dir = _root_dir() / "bin"
    for executable in _tool_names(name):
        candidate = tool_dir / executable
        if candidate.is_file():
            return str(candidate), "bin"
    return None, None


def _run_command(command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanroomException(
            503, "JXL_TOOL_TIMEOUT", "JPEG XL 工具执行超时"
        ) from exc
    except OSError as exc:
        raise CleanroomException(
            503, "JXL_TOOL_UNAVAILABLE", "JPEG XL 工具无法启动"
        ) from exc


def _decode_output(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _probe_one(name: str) -> tuple[dict[str, Any], str | None]:
    executable, source = _find_tool(name)
    code = name.upper() + "_NOT_FOUND"
    if not executable:
        return (
            {
                "available": False,
                "status": "missing",
                "code": code,
                "version": None,
                "source": None,
            },
            None,
        )
    try:
        result = _run_command([executable, "--version"], timeout=PROBE_TIMEOUT_SECONDS)
    except CleanroomException:
        return (
            {
                "available": False,
                "status": "error",
                "code": name.upper() + "_PROBE_FAILED",
                "version": None,
                "source": source,
            },
            executable,
        )
    version_lines = [
        line.strip()
        for line in (_decode_output(result.stdout) + "\n" + _decode_output(result.stderr)).splitlines()
        if line.strip()
    ]
    if result.returncode != 0 or not version_lines:
        return (
            {
                "available": False,
                "status": "error",
                "code": name.upper() + "_PROBE_FAILED",
                "version": None,
                "source": source,
            },
            executable,
        )
    return (
        {
            "available": True,
            "status": "ready",
            "code": None,
            "version": version_lines[0][:160],
            "source": source,
        },
        executable,
    )


def detect_tools() -> dict[str, Any]:
    """探测 PATH / 仓库 bin 中的 cjxl 与 djxl，不暴露本机绝对路径。"""
    encoder, cjxl = _probe_one("cjxl")
    decoder, djxl = _probe_one("djxl")
    status = "ready" if encoder["available"] and decoder["available"] else "missing"
    if any(item["status"] == "error" for item in (encoder, decoder)):
        status = "error"
    return {
        "status": status,
        "cjxl": encoder,
        "djxl": decoder,
        "download_url": OFFICIAL_RELEASES_URL,
        "guidance": (
            "安装官方 libjxl 工具，并确保 cjxl 与 djxl 位于 PATH 或应用 bin/ 目录。"
        ),
        "data_status": "ok",
        "data_gaps": [],
    }


def _require_tools() -> tuple[str, str]:
    encoder, cjxl = _probe_one("cjxl")
    decoder, djxl = _probe_one("djxl")
    if not encoder["available"]:
        code = str(encoder["code"] or "CJXL_NOT_FOUND")
        raise CleanroomException(
            503,
            code,
            "未检测到可用的 cjxl；请安装官方 libjxl 并配置 PATH 或应用 bin/ 目录",
        )
    if not decoder["available"]:
        code = str(decoder["code"] or "DJXL_NOT_FOUND")
        raise CleanroomException(
            503,
            code,
            "未检测到可用的 djxl；验证无损转码需要同时安装官方解码工具",
        )
    assert cjxl is not None and djxl is not None
    return cjxl, djxl


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CleanroomException(409, "SOURCE_READ_FAILED", "JPEG 源文件读取失败") from exc
    return digest.hexdigest()


def _run_roundtrip(
    *,
    source: Path,
    jxl_path: Path,
    restored_path: Path,
    cjxl: str,
    djxl: str,
) -> str:
    encode = _run_command(
        [cjxl, "--lossless_jpeg=1", str(source), str(jxl_path)],
        timeout=TRANSCODE_TIMEOUT_SECONDS,
    )
    if encode.returncode != 0 or not jxl_path.is_file() or jxl_path.stat().st_size <= 0:
        raise CleanroomException(422, "CJXL_FAILED", "cjxl 未生成有效 JXL 文件")
    try:
        with jxl_path.open("rb") as encoded:
            signature = encoded.read(12)
    except OSError as exc:
        raise CleanroomException(422, "JXL_OUTPUT_UNREADABLE", "JXL 产物无法读取") from exc
    if not signature.startswith(JXL_SIGNATURES):
        raise CleanroomException(422, "JXL_OUTPUT_INVALID", "cjxl 输出不是有效的 JXL 文件")

    decode = _run_command(
        [djxl, str(jxl_path), str(restored_path)],
        timeout=TRANSCODE_TIMEOUT_SECONDS,
    )
    if decode.returncode != 0 or not restored_path.is_file():
        raise CleanroomException(422, "DJXL_FAILED", "djxl 无法还原 JXL 产物")
    return _sha256_file(restored_path)


def _output_relative(asset_id: str, source_sha256: str) -> str:
    if not asset_id or not all(
        char.isascii() and (char.isalnum() or char in "_-") for char in asset_id
    ):
        raise CleanroomException(400, "INVALID_ASSET_ID", "素材标识不是安全单段")
    return f"media_output/jxl-lossless/{asset_id}-{source_sha256[:16]}.jxl"


def _transcode_one(
    item: dict[str, Any],
    *,
    job_id: str,
    source_lookup: Callable[[str], Path | None],
    cjxl: str,
    djxl: str,
) -> dict[str, Any]:
    asset_id = str(item["asset_id"])
    source = source_lookup(asset_id)
    if source is None or not source.is_file():
        raise CleanroomException(404, "JPEG_SOURCE_NOT_FOUND", "登记的 JPEG 源文件不可用")
    if source.suffix.lower() not in {".jpg", ".jpeg"}:
        raise CleanroomException(400, "NOT_A_JPEG", "仅支持登记为 JPEG 的素材")
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise CleanroomException(409, "SOURCE_READ_FAILED", "JPEG 源文件状态不可读取") from exc
    if (
        int(item.get("source_size_bytes", -1)) != source_stat.st_size
        or int(item.get("source_mtime_ns", -1)) != source_stat.st_mtime_ns
    ):
        raise CleanroomException(409, "SOURCE_CHANGED", "扫描后源 JPEG 已变化，请重新扫描")

    source_sha256 = _sha256_file(source)
    output_relative = _output_relative(asset_id, source_sha256)
    storage.resolve_output_directory(
        "media_output/jxl-lossless", must_exist=False
    ).mkdir(parents=True, exist_ok=True)
    storage.resolve_output_directory("media_output/jxl-lossless", must_exist=True)
    output_path = storage.resolve_output_path(output_relative, must_exist=False)
    temp_prefix = f".{asset_id}.{job_id}"
    encoded_relative = f"media_output/jxl-lossless/{temp_prefix}.part.jxl"
    restored_relative = f"media_output/jxl-lossless/{temp_prefix}.restored.jpg"
    encoded_path = storage.resolve_output_path(encoded_relative, must_exist=False)
    restored_path = storage.resolve_output_path(restored_relative, must_exist=False)
    created_output = False
    try:
        if output_path.exists():
            verified_sha256 = _run_roundtrip(
                source=source,
                jxl_path=output_path,
                restored_path=restored_path,
                cjxl=cjxl,
                djxl=djxl,
            )
            status = "cached"
        else:
            verified_sha256 = _run_roundtrip(
                source=source,
                jxl_path=encoded_path,
                restored_path=restored_path,
                cjxl=cjxl,
                djxl=djxl,
            )
            if verified_sha256 != source_sha256:
                raise CleanroomException(
                    422, "LOSSLESS_VERIFICATION_FAILED", "JXL 往返后 JPEG SHA256 不一致"
                )
            try:
                after_stat = source.stat()
            except OSError as exc:
                raise CleanroomException(409, "SOURCE_CHANGED", "转码期间源 JPEG 不可读取") from exc
            if (
                after_stat.st_size != source_stat.st_size
                or after_stat.st_mtime_ns != source_stat.st_mtime_ns
                or _sha256_file(source) != source_sha256
            ):
                raise CleanroomException(409, "SOURCE_CHANGED", "转码期间源 JPEG 已变化")
            if output_path.exists():
                raise CleanroomException(409, "OUTPUT_EXISTS", "同名 JXL 产物已存在，请重新扫描")
            os.replace(encoded_path, output_path)
            created_output = True
            status = "generated"
        if verified_sha256 != source_sha256:
            raise CleanroomException(
                422, "LOSSLESS_VERIFICATION_FAILED", "JXL 往返后 JPEG SHA256 不一致"
            )
        output_size = output_path.stat().st_size
        return {
            "asset_id": asset_id,
            "expected_version": int(item["expected_version"]),
            "status": status,
            "output": output_relative,
            "source_sha256": source_sha256,
            "verified_sha256": verified_sha256,
            "source_size_bytes": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "output_size_bytes": output_size,
            "saved_bytes": source_stat.st_size - output_size,
            "verified": True,
            "created_output": created_output,
        }
    except CleanroomException:
        if created_output:
            output_path.unlink(missing_ok=True)
        raise
    finally:
        encoded_path.unlink(missing_ok=True)
        restored_path.unlink(missing_ok=True)


class JxlLosslessTranscodeService:
    """独立落盘的单实例任务服务；源 JPEG 永不由该服务改写或删除。"""

    def __init__(
        self,
        source_lookup: Callable[[str], Path | None],
        record_transcodes: Callable[[list[dict[str, Any]]], dict[str, Any]],
    ) -> None:
        self._store = _store()
        self._source_lookup = source_lookup
        self._record_transcodes = record_transcodes
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="jxl-lossless"
        )
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        def mutate(raw: dict[str, Any]) -> None:
            for job in raw.setdefault("jobs", {}).values():
                if job.get("status") in {"queued", "running"}:
                    job.update(
                        {
                            "status": "interrupted",
                            "phase": "interrupted",
                            "error_code": "JOB_INTERRUPTED_BY_RESTART",
                            "updated_at": storage.now_iso(),
                            "finished_at": storage.now_iso(),
                            "version": int(job.get("version", 1)) + 1,
                        }
                    )

        self._store.mutate(mutate)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        progress = dict(job.get("progress") or {})
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "phase": job.get("phase", job["status"]),
            "version": int(job.get("version", 1)),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "finished_at": job.get("finished_at"),
            "progress": progress,
            "result": job.get("result"),
            "error_code": job.get("error_code"),
            "cancel_requested": bool(job.get("cancel_requested")),
            "data_status": "ok",
            "data_gaps": [],
        }

    def _owner_job(self, job_id: str, owner: str) -> dict[str, Any]:
        job = self._store.read().get("jobs", {}).get(job_id)
        if job is None or job.get("owner") != owner:
            raise CleanroomException(404, "JOB_NOT_FOUND", "转码任务不存在")
        return job

    def _update(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        def mutate(raw: dict[str, Any]) -> dict[str, Any] | None:
            job = raw.setdefault("jobs", {}).get(job_id)
            if job is None:
                return None
            job.update(changes)
            job["version"] = int(job.get("version", 1)) + 1
            job["updated_at"] = storage.now_iso()
            return dict(job)

        return self._store.mutate(mutate)

    def submit(self, items: list[dict[str, Any]], owner: str) -> dict[str, Any]:
        if not items:
            raise CleanroomException(409, "NO_PENDING_JPEG", "没有待转码的 JPEG 素材")
        if len(items) > MAX_ASSETS_PER_JOB:
            raise CleanroomException(413, "TRANSCODE_BATCH_TOO_LARGE", "单个转码任务最多处理 100000 项")
        clean_items = []
        seen = set()
        for item in items:
            asset_id = str(item.get("asset_id") or "").strip()
            expected_version = item.get("expected_version")
            if (
                not asset_id
                or asset_id in seen
                or type(expected_version) is not int
                or expected_version < 1
            ):
                continue
            seen.add(asset_id)
            clean_items.append(
                {
                    "asset_id": asset_id,
                    "expected_version": expected_version,
                    "source_size_bytes": int(item["source_size_bytes"]),
                    "source_mtime_ns": int(item["source_mtime_ns"]),
                }
            )
        if not clean_items:
            raise CleanroomException(400, "INVALID_REQUEST", "缺少有效 JPEG 素材")

        with self._lock:
            raw = self._store.read()
            jobs = raw.setdefault("jobs", {})
            active = [job for job in jobs.values() if job.get("status") not in TERMINAL_STATUSES]
            if len(active) >= MAX_ACTIVE_JOBS:
                raise CleanroomException(429, "TRANSCODE_QUEUE_FULL", "转码队列已满，请稍后重试")
            selected_ids = {item["asset_id"] for item in clean_items}
            active_ids = {
                item["asset_id"]
                for job in active
                for item in job.get("_items", [])
            }
            if selected_ids & active_ids:
                raise CleanroomException(409, "ASSET_ALREADY_TRANSCODING", "所选素材已有转码任务运行")
            if len(jobs) >= MAX_STORED_JOBS:
                raise CleanroomException(429, "TRANSCODE_HISTORY_FULL", "转码任务记录已满，请重启服务后再试")
            raw["sequence"] = int(raw.get("sequence", 0)) + 1
            job_id = f"job_jxl_lossless_{raw['sequence']:08d}"
            now = storage.now_iso()
            job = {
                "job_id": job_id,
                "job_type": "asset.jpeg.lossless_transcode",
                "status": "queued",
                "phase": "queued",
                "version": 1,
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
                "owner": owner,
                "_items": clean_items,
                "cancel_requested": False,
                "error_code": None,
                "progress": {
                    "total": len(clean_items),
                    "completed": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "cached": 0,
                    "saved_bytes": 0,
                    "current_asset_id": "",
                },
                "result": None,
            }
            jobs[job_id] = job
            self._store.write(raw)
            try:
                request_context = contextvars.copy_context()
                self._executor.submit(request_context.run, self._run, job_id)
            except RuntimeError:
                self._update(
                    job_id,
                    {
                        "status": "failed",
                        "phase": "failed",
                        "error_code": "TRANSCODE_EXECUTOR_UNAVAILABLE",
                        "finished_at": storage.now_iso(),
                    },
                )
            public = self._public(job)
        return {
            "job_id": job_id,
            "job": public,
            "poll_hint": {
                "url": f"/api/assets/transcode-jpeg-lossless/jobs/{job_id}",
                "interval_ms": 500,
            },
            "data_status": "ok",
            "data_gaps": [],
        }

    def get(self, job_id: str, owner: str) -> dict[str, Any]:
        return self._public(self._owner_job(job_id, owner))

    def active_for_owner(self, owner: str) -> dict[str, Any] | None:
        jobs = self._store.read().get("jobs", {}).values()
        active = [
            job for job in jobs
            if job.get("owner") == owner and job.get("status") not in TERMINAL_STATUSES
        ]
        if not active:
            return None
        latest = max(active, key=lambda job: str(job.get("created_at") or ""))
        return self._public(latest)

    def cancel(self, job_id: str, owner: str) -> dict[str, Any]:
        with self._lock:
            job = self._owner_job(job_id, owner)
            if job["status"] == "queued":
                updated = self._update(
                    job_id,
                    {
                        "status": "cancelled",
                        "phase": "cancelled",
                        "cancel_requested": True,
                        "finished_at": storage.now_iso(),
                    },
                )
                return self._public(updated or job)
            if job["status"] == "running":
                updated = self._update(job_id, {"cancel_requested": True})
                return self._public(updated or job)
            raise CleanroomException(409, "JOB_NOT_CANCELLABLE", "当前转码任务已结束，无法取消")

    def _cancel_requested(self, job_id: str) -> bool:
        job = self._store.read().get("jobs", {}).get(job_id, {})
        return bool(job.get("cancel_requested")) or job.get("status") in TERMINAL_STATUSES

    def _persist_progress(
        self, job_id: str, progress: dict[str, Any], *, current_asset_id: str
    ) -> None:
        job = self._store.read().get("jobs", {}).get(job_id)
        if not job or job.get("status") in TERMINAL_STATUSES:
            return
        value = dict(progress)
        value["current_asset_id"] = current_asset_id
        self._update(job_id, {"progress": value, "phase": "transcoding"})

    def _flush_batch(
        self,
        job_id: str,
        outcomes: list[dict[str, Any]],
        progress: dict[str, Any],
    ) -> None:
        candidates = [
            item["metadata_record"]
            for item in outcomes
            if item.get("metadata_record") is not None
        ]
        record_status: dict[str, dict[str, Any]] = {}
        if candidates:
            try:
                result = self._record_transcodes(candidates)
                record_status = {
                    item["asset_id"]: item
                    for item in result.get("items", [])
                    if isinstance(item, dict) and item.get("asset_id")
                }
            except Exception:
                record_status = {}
        public_items = []
        for outcome in outcomes:
            record = outcome.get("metadata_record")
            if record is not None:
                saved = record_status.get(outcome["asset_id"], {})
                if not saved.get("recorded"):
                    if outcome.get("created_output"):
                        self._remove_output(str(outcome.get("output") or ""))
                    outcome = {
                        "asset_id": outcome["asset_id"],
                        "status": "failed",
                        "error_code": saved.get("error_code") or "ASSET_INDEX_UPDATE_FAILED",
                    }
            public_items.append(
                {
                    key: value
                    for key, value in outcome.items()
                    if key not in {"metadata_record", "created_output"}
                }
            )
        progress["completed"] += len(public_items)
        for item in public_items:
            if item["status"] == "generated":
                progress["succeeded"] += 1
                progress["saved_bytes"] += int(item.get("saved_bytes", 0))
            elif item["status"] == "cached":
                progress["cached"] += 1
            else:
                progress["failed"] += 1
        current = self._store.read().get("jobs", {}).get(job_id)
        if not current or current.get("status") in TERMINAL_STATUSES:
            return
        retained = list((current.get("result") or {}).get("items", []))
        retained.extend(public_items)
        result = {
            "verified": progress["succeeded"] + progress["cached"],
            "succeeded": progress["succeeded"],
            "failed": progress["failed"],
            "cached": progress["cached"],
            "saved_bytes": progress["saved_bytes"],
            "items": retained[:MAX_RESULT_ITEMS],
        }
        updated_progress = {**progress, "current_asset_id": ""}
        self._update(job_id, {"progress": updated_progress, "result": result})

    @staticmethod
    def _remove_output(relative: str) -> None:
        if not relative:
            return
        try:
            output = storage.resolve_output_path(relative, must_exist=False)
            output.unlink(missing_ok=True)
        except (CleanroomException, OSError):
            return

    def _run(self, job_id: str) -> None:
        job = self._store.read().get("jobs", {}).get(job_id)
        if not job or job.get("status") != "queued":
            return
        self._update(
            job_id,
            {"status": "running", "phase": "transcoding", "started_at": storage.now_iso()},
        )
        try:
            cjxl, djxl = _require_tools()
            job = self._store.read()["jobs"][job_id]
            items = list(job.get("_items", []))
            progress = dict(job["progress"])
            pending: list[dict[str, Any]] = []
            last_progress_write = time.monotonic()
            for item in items:
                if self._cancel_requested(job_id):
                    break
                asset_id = item["asset_id"]
                try:
                    converted = _transcode_one(
                        item,
                        job_id=job_id,
                        source_lookup=self._source_lookup,
                        cjxl=cjxl,
                        djxl=djxl,
                    )
                    metadata_record = {
                        key: converted[key]
                        for key in (
                            "asset_id",
                            "expected_version",
                            "output",
                            "source_sha256",
                            "verified_sha256",
                            "source_size_bytes",
                            "source_mtime_ns",
                            "output_size_bytes",
                            "saved_bytes",
                            "verified",
                        )
                    }
                    metadata_record["verified_at"] = storage.now_iso()
                    pending.append(
                        {
                            "asset_id": asset_id,
                            "status": converted["status"],
                            "output": converted["output"],
                            "source_size_bytes": converted["source_size_bytes"],
                            "output_size_bytes": converted["output_size_bytes"],
                            "saved_bytes": converted["saved_bytes"],
                            "verified": True,
                            "created_output": converted["created_output"],
                            "metadata_record": metadata_record,
                        }
                    )
                except CleanroomException as exc:
                    pending.append(
                        {
                            "asset_id": asset_id,
                            "status": "failed",
                            "error_code": exc.code,
                        }
                    )
                progress["current_asset_id"] = asset_id
                elapsed = time.monotonic() - last_progress_write
                if len(pending) >= PROGRESS_BATCH_SIZE:
                    self._flush_batch(job_id, pending, progress)
                    pending = []
                    progress = dict(self._store.read()["jobs"][job_id]["progress"])
                    last_progress_write = time.monotonic()
                elif elapsed >= PROGRESS_MIN_INTERVAL_SECONDS:
                    self._persist_progress(
                        job_id, progress, current_asset_id=asset_id
                    )
                    last_progress_write = time.monotonic()
            if pending:
                self._flush_batch(job_id, pending, progress)
                progress = dict(self._store.read()["jobs"][job_id]["progress"])
            if self._cancel_requested(job_id):
                status = "cancelled"
                phase = "cancelled"
                error_code = None
            elif progress["failed"]:
                status = "failed"
                phase = "failed"
                error_code = "TRANSCODE_ITEMS_FAILED"
            else:
                status = "succeeded"
                phase = "succeeded"
                error_code = None
            self._update(
                job_id,
                {
                    "status": status,
                    "phase": phase,
                    "error_code": error_code,
                    "finished_at": storage.now_iso(),
                    "progress": {**progress, "current_asset_id": ""},
                },
            )
        except CleanroomException as exc:
            self._update(
                job_id,
                {
                    "status": "failed",
                    "phase": "failed",
                    "error_code": exc.code,
                    "finished_at": storage.now_iso(),
                },
            )
        except Exception:
            self._update(
                job_id,
                {
                    "status": "failed",
                    "phase": "failed",
                    "error_code": "TRANSCODE_JOB_FAILED",
                    "finished_at": storage.now_iso(),
                },
            )
