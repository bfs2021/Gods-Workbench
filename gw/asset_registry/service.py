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

"""Phase 11 B3 素材注册表的洁净室服务。

本块先建立可审计的注册表边界，而不是伪造文件/媒体/远程索引能力：
- 仅返回进程内可证明的空注册表、空回收站、空关系和空版本集合；
- 所有会读取或写入真实文件、媒体、远程资源、索引、压缩包的操作显式 503；
- 不创建演示素材，不扫描本机路径，不执行外部程序或网络请求；
- 所有响应携带 data_status/data_gaps，便于前端保留显式降级。
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gw.asset_registry.models import ProducedAssetProvenance
from gw.core.errors import CleanroomException

DATA_STATUS_OK = "ok"
DATA_STATUS_NOT_INTEGRATED = "not_integrated"
ASSET_REGISTRY_NOT_INTEGRATED = "ASSET_REGISTRY_NOT_INTEGRATED"
ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
JOB_NOT_FOUND = "WORKSPACE_JOB_NOT_FOUND"
ASSET_ALREADY_EXISTS = "ASSET_ALREADY_EXISTS"
RECYCLE_ENTRY_NOT_FOUND = "RECYCLE_ENTRY_NOT_FOUND"
ASSET_RESTORE_CONFLICT = "ASSET_RESTORE_CONFLICT"
ASSET_RESTORE_PARTIAL_FAILURE = "ASSET_RESTORE_PARTIAL_FAILURE"
# MVP2-T2：生产链产物回写的来源链相关错误码。
PROVENANCE_REQUIRED = "PROVENANCE_REQUIRED"
PROVENANCE_CONFLICT = "PROVENANCE_CONFLICT"
PROVENANCE_NOT_FOUND = "PROVENANCE_NOT_FOUND"
UNKNOWN_TARGET_ROOT = "UNKNOWN_TARGET_ROOT"
# QIN-440-followup（Issue #408）：sidecar 落盘失败统一成受控异常，
# 避免裸 OSError 把本机绝对路径带回前端。
PROVENANCE_SIDECAR_WRITE_FAILED = "PROVENANCE_SIDECAR_WRITE_FAILED"

_GAP_REGISTRY_SOURCE = "asset_registry_source_not_connected"
_GAP_MEDIA_SOURCE = "media_storage_not_connected"
_GAP_REMOTE_SOURCE = "remote_asset_source_not_connected"
_GAP_INDEX_SOURCE = "asset_index_not_connected"

# 产物回写只允许出现在已配置产出根下的相对目录；sidecar 文件名由 asset_id 生成。
_PROVENANCE_SIDECAR_DIR = "provenance"
# asset_id 参与 sidecar 文件名，必须排除路径分隔符与遍历写法。
_SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_STRUCTURED_KINDS = {"image", "video", "audio", "document", "model", "font", "lut", "other"}
_STRUCTURED_FILTER_FIELDS = {"kind", "project_id", "tag_id", "created_at", "status"}
_STRUCTURED_SORTS = {"newest", "oldest", "name", "asset_id"}
_STRUCTURED_INCLUDES = {"facets", "preview"}


def _structured_error(code: str, message: str) -> None:
    raise CleanroomException(400, code, message)


def _json_or_csv(value: Any, *, field: str) -> Any:
    """读取 GET 参数里的 JSON 数组/对象，同时兼容简短逗号分隔写法。"""
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            _structured_error("UNSUPPORTED_FILTER_VALUE", f"{field} 不是有效 JSON")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _string_list(
    value: Any, *, field: str, allowed: set[str] | None = None
) -> list[str]:
    values = _json_or_csv(value, field=field)
    if values is None:
        return []
    if not isinstance(values, list):
        _structured_error("UNSUPPORTED_FILTER_VALUE", f"{field} 必须是字符串数组")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            _structured_error("UNSUPPORTED_FILTER_VALUE", f"{field} 包含无效值")
        clean = item.strip()
        if allowed is not None and clean not in allowed:
            _structured_error("UNSUPPORTED_FILTER_VALUE", f"{field} 不支持值 {clean}")
        if clean not in result:
            result.append(clean)
    return sorted(result)


def parse_utc_datetime(value: Any, *, field: str = "created_at") -> datetime:
    """解析带时区的 UTC RFC3339 时间；拒绝本地时间和无效时间。"""
    if not isinstance(value, str) or not value.strip():
        _structured_error("INVALID_TIME_RANGE", f"{field} 必须是带时区的 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _structured_error("INVALID_TIME_RANGE", f"{field} 不是有效的 RFC3339 时间")
    if parsed.tzinfo is None:
        _structured_error("INVALID_TIME_RANGE", f"{field} 必须包含时区")
    return parsed.astimezone(UTC)


def _canonical_datetime(value: Any, *, field: str) -> str:
    return parse_utc_datetime(value, field=field).isoformat().replace("+00:00", "Z")


def normalize_asset_query(
    *,
    scope: Any = None,
    text: Any = None,
    filters: Any = None,
    sort: str | None = None,
    limit: Any = 50,
    include: Any = None,
) -> dict[str, Any]:
    """规范化 C11 资产查询，未知字段和值一律显式失败。"""
    scopes = _string_list(scope or ["asset"], field="scope")
    if not scopes:
        _structured_error("UNSUPPORTED_FILTER_VALUE", "scope 不能为空")
    if any(value != "asset" for value in scopes):
        _structured_error("UNSUPPORTED_FILTER_VALUE", "当前资产入口只支持 asset scope")

    if text is None:
        clean_text = ""
    elif isinstance(text, str):
        clean_text = text.strip()
    else:
        _structured_error("UNSUPPORTED_FILTER_VALUE", "text 必须是字符串")

    raw_filters = _json_or_csv(filters or {}, field="filters")
    if raw_filters is None:
        raw_filters = {}
    if not isinstance(raw_filters, dict):
        _structured_error("UNSUPPORTED_FILTER_VALUE", "filters 必须是对象")
    unknown = set(raw_filters) - _STRUCTURED_FILTER_FIELDS
    if unknown:
        _structured_error(
            "UNSUPPORTED_FILTER_VALUE", f"不支持过滤字段: {sorted(unknown)[0]}"
        )

    normalized: dict[str, Any] = {}
    if "kind" in raw_filters:
        normalized["kind"] = _string_list(
            raw_filters.get("kind"), field="kind", allowed=_STRUCTURED_KINDS
        )
    if "project_id" in raw_filters:
        normalized["project_id"] = _string_list(
            raw_filters.get("project_id"), field="project_id"
        )
    if "tag_id" in raw_filters:
        normalized["tag_id"] = _string_list(raw_filters.get("tag_id"), field="tag_id")
    if "created_at" in raw_filters:
        created = raw_filters.get("created_at")
        if not isinstance(created, dict) or set(created) - {"from", "to"}:
            _structured_error("INVALID_TIME_RANGE", "created_at 只接受 from/to")
        if not created.get("from") and not created.get("to"):
            _structured_error("INVALID_TIME_RANGE", "created_at 至少需要 from 或 to")
        window: dict[str, str] = {}
        if created.get("from") is not None:
            window["from"] = _canonical_datetime(
                created["from"], field="created_at.from"
            )
        if created.get("to") is not None:
            window["to"] = _canonical_datetime(created["to"], field="created_at.to")
        if "from" in window and "to" in window and window["from"] >= window["to"]:
            _structured_error(
                "INVALID_TIME_RANGE", "created_at.from 必须早于 created_at.to"
            )
        normalized["created_at"] = window
    if "status" in raw_filters:
        status = raw_filters.get("status")
        if not isinstance(status, dict) or set(status) - {"asset"}:
            _structured_error(
                "UNSUPPORTED_FILTER_VALUE", "status 必须明确指定 asset 域"
            )
        normalized["status"] = {
            "asset": _string_list(
                status.get("asset"),
                field="status.asset",
                allowed={"active", "archived"},
            )
        }

    clean_sort = str(sort or "newest").strip()
    if clean_sort not in _STRUCTURED_SORTS:
        _structured_error("UNSUPPORTED_SORT", f"不支持的资产排序 {clean_sort}")
    if type(limit) is not int or not 1 <= limit <= 200:
        _structured_error("INVALID_LIMIT", "结构化查询 limit 必须在 1 到 200 之间")
    includes = _string_list(
        include or [], field="include", allowed=_STRUCTURED_INCLUDES
    )
    return {
        "scope": scopes,
        "text": clean_text,
        "filters": normalized,
        "sort": clean_sort,
        "limit": limit,
        "include": includes,
    }


def _not_integrated(
    endpoint: str,
    *,
    code: str = ASSET_REGISTRY_NOT_INTEGRATED,
    gap: str = _GAP_REGISTRY_SOURCE,
) -> None:
    """对未获准入的副作用统一失败关闭。"""
    raise CleanroomException(
        status_code=503,
        code=code,
        message="素材注册表该能力尚未接入真实数据源，已拒绝执行；未读取或写入外部资源。",
        extra={
            "endpoint": endpoint,
            "unavailable": True,
            "data_status": DATA_STATUS_NOT_INTEGRATED,
            "data_gaps": [gap],
        },
        expose_extra_fields={"endpoint", "unavailable", "data_status"},
    )


def _not_found(code: str, message: str) -> None:
    """返回可区分的资源不存在错误。"""
    raise CleanroomException(status_code=404, code=code, message=message)


def _truthful_meta(*, gaps: list[str] | None = None) -> dict[str, Any]:
    """构造统一的可用性元数据，不把空集合误报为成功抓取。"""
    return {
        "data_status": DATA_STATUS_OK if not gaps else DATA_STATUS_NOT_INTEGRATED,
        "data_gaps": list(gaps or []),
    }


def _now_iso() -> str:
    """返回可回读的 UTC 事实时间，不依赖外部时钟服务。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalized_roots(roots: Mapping[str, str | Path]) -> dict[str, Path]:
    """把调用方注入的根映射规范化为 root_id -> 绝对路径。

    空/空白 root_id 与非绝对路径（含空串、纯空白、相对路径）一律失败关闭：
    相对路径会在解析时落到进程工作目录，等于把产物写进不可控位置，
    因此这里拒绝而不是静默接受。产出根与源素材根共用同一套严格度。
    """
    normalized: dict[str, Path] = {}
    for raw_root_id, raw_root_path in roots.items():
        root_id = str(raw_root_id).strip()
        if not root_id:
            raise CleanroomException(
                400, "INVALID_REQUEST", "产出根/源素材根的 root_id 不能为空或空白"
            )
        candidate = Path(str(raw_root_path).strip())
        if not candidate.is_absolute():
            raise CleanroomException(
                400,
                UNKNOWN_TARGET_ROOT,
                "产出根/源素材根必须是绝对路径；空路径与相对路径一律拒绝，"
                "避免解析后落到进程工作目录",
                extra={"root_id": root_id},
                expose_extra_fields={"root_id"},
            )
        normalized[root_id] = candidate
    return normalized


def _is_within(candidate: Path, parent: Path) -> bool:
    """判断候选路径是否位于父目录内（含父目录自身）。"""
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _normalize_produced_provenance(provenance: Any) -> dict[str, Any]:
    """校验并规范化产物来源链；缺字段、None、非法取值或多余字段都失败关闭。"""
    if not isinstance(provenance, dict):
        raise CleanroomException(
            400,
            PROVENANCE_REQUIRED,
            "产物回写素材注册表必须携带 provenance 来源链对象",
        )
    try:
        model = ProducedAssetProvenance.model_validate(provenance)
    except ValidationError as exc:
        invalid_fields = sorted(
            {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
        )
        raise CleanroomException(
            400,
            PROVENANCE_REQUIRED,
            "provenance 来源链不完整或不合法，已拒绝回写素材注册表",
            extra={"invalid_fields": invalid_fields},
            expose_extra_fields={"invalid_fields"},
        ) from None
    return model.model_dump()


class AssetRegistryService:
    """素材注册表的最小进程内真值。

    当前不从旧仓、数据库、本地目录或远程服务读取资产，因此初始集合严格为空。
    这样可以支持列表/状态/治理页面的确定性空态，同时让真实资源操作保持 503。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revision = 1
        # 仅保存调用方明确登记的内存快照；不扫描文件系统，也不把空态伪装成真实来源。
        self._assets: dict[str, dict[str, Any]] = {}
        self._recycle_entries: dict[str, dict[str, Any]] = {}
        self._recycle_sequence = 0
        # 只缓存显式幂等键对应的本地事实；不以键推断或重放外部副作用。
        self._idempotent_results: dict[str, dict[str, Any]] = {}
        # MVP2-T2：产出根与源素材根只由调用方配置注入，本模块不固化任何默认路径。
        self._output_roots: dict[str, Path] = {}
        self._source_roots: dict[str, Path] = {}
        self._team_preferences: dict[str, Any] = {
            "team_id": None,
            "default_library_id": None,
            "default_folder_id": None,
            "view": "grid",
            "sort": "newest",
            "version": 1,
        }

    def _meta(self) -> dict[str, Any]:
        return {"revision": self._revision, **_truthful_meta()}

    def root_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "registry": "asset-registry",
                "revision": self._revision,
                "assets": [copy.deepcopy(item) for item in self._assets.values()],
                "features": self._features(),
                "backend": "cleanroom-memory",
                **_truthful_meta(),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": True,
                "backend": "cleanroom-memory",
                "revision": self._revision,
                "assets_count": len(self._assets),
                "jobs": [],
                "overview": {"assets": len(self._assets), "projects": 0, "canvases": 0},
                "features": self._features(),
                **_truthful_meta(),
            }

    @staticmethod
    def _features() -> list[dict[str, Any]]:
        return [
            {
                "id": "asset_registry",
                "name": "素材登记",
                "enabled": False,
                "data_status": DATA_STATUS_NOT_INTEGRATED,
            },
            {
                "id": "media_preview",
                "name": "媒体预览",
                "enabled": False,
                "data_status": DATA_STATUS_NOT_INTEGRATED,
            },
            {
                "id": "remote_assets",
                "name": "远程素材",
                "enabled": False,
                "data_status": DATA_STATUS_NOT_INTEGRATED,
            },
            {
                "id": "index_automation",
                "name": "索引自动化",
                "enabled": False,
                "data_status": DATA_STATUS_NOT_INTEGRATED,
            },
        ]

    def list_assets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
        structured: bool = False,
        scope: Any = None,
        text: Any = None,
        filters: Any = None,
        sort: str | None = None,
        include: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        # 集合只包含调用方显式登记的内存快照，不生成演示数据。
        if structured:
            normalized = normalize_asset_query(
                scope=scope,
                text=text,
                filters=filters,
                sort=sort,
                limit=limit,
                include=include,
            )
            if offset < 0:
                raise CleanroomException(
                    status_code=400, code="INVALID_OFFSET", message="offset 不能为负数"
                )
            if cursor is not None:
                try:
                    cursor_offset = int(cursor)
                except (TypeError, ValueError):
                    raise CleanroomException(
                        400, "INVALID_CURSOR", "分页游标无效或不属于当前查询"
                    ) from None
                if cursor_offset < 0 or (offset and offset != cursor_offset):
                    raise CleanroomException(
                        400, "INVALID_CURSOR", "分页游标无效或不属于当前查询"
                    )
                offset = cursor_offset
            with self._lock:
                values = [copy.deepcopy(item) for item in self._assets.values()]
                needle = normalized["text"].casefold()
                if needle:
                    values = [
                        item
                        for item in values
                        if needle in str(item.get("name") or "").casefold()
                        or needle in str(item.get("asset_id") or "").casefold()
                    ]
                filters_map = normalized["filters"]
                if filters_map.get("kind"):
                    values = [
                        item
                        for item in values
                        if str(item.get("kind") or "other") in filters_map["kind"]
                    ]
                if filters_map.get("project_id"):
                    projects = set(filters_map["project_id"])
                    values = [
                        item
                        for item in values
                        if projects.intersection(
                            set(item.get("project_ids") or [])
                            | (
                                {item["project_id"]}
                                if item.get("project_id")
                                else set()
                            )
                        )
                    ]
                if filters_map.get("tag_id"):
                    tags = set(filters_map["tag_id"])
                    values = [
                        item
                        for item in values
                        if tags.intersection(set(item.get("tags") or []))
                    ]
                if filters_map.get("status", {}).get("asset"):
                    allowed_status = set(filters_map["status"]["asset"])
                    values = [
                        item
                        for item in values
                        if ("archived" if item.get("archived") else "active")
                        in allowed_status
                    ]
                if "created_at" in filters_map:
                    window = filters_map["created_at"]
                    start = (
                        parse_utc_datetime(window["from"])
                        if window.get("from")
                        else None
                    )
                    end = parse_utc_datetime(window["to"]) if window.get("to") else None
                    selected = []
                    for item in values:
                        try:
                            created = parse_utc_datetime(item.get("created_at"))
                        except CleanroomException:
                            continue
                        if start is not None and created < start:
                            continue
                        if end is not None and created >= end:
                            continue
                        selected.append(item)
                    values = selected
                values.sort(key=lambda item: str(item.get("asset_id") or ""))
                if normalized["sort"] in {"newest", "oldest"}:
                    values.sort(
                        key=lambda item: str(item.get("created_at") or ""),
                        reverse=normalized["sort"] == "newest",
                    )
                elif normalized["sort"] == "name":
                    values.sort(key=lambda item: str(item.get("name") or "").casefold())
                total = len(values)
                page = values[offset : offset + limit]
                has_more = offset + len(page) < total
                next_cursor = str(offset + len(page)) if has_more else None
                projected = []
                for item in page:
                    projected_item = copy.deepcopy(item)
                    projected_item.pop("id", None)
                    projected_item["entity_type"] = "asset"
                    projected_item["tag_id"] = list(item.get("tags") or [])
                    projected_item["status"] = (
                        "archived" if item.get("archived") else "active"
                    )
                    projected.append(projected_item)
                return {
                    "assets": projected,
                    "items": projected,
                    "total": total,
                    "total_known": True,
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                    "revision": self._revision,
                    "page": {
                        "limit": limit,
                        "has_more": has_more,
                        "next_cursor": next_cursor,
                        "collection_revision": self._revision,
                    },
                    "applied_filters": {
                        "scope": normalized["scope"],
                        "sort": normalized["sort"],
                        **({"text": normalized["text"]} if normalized["text"] else {}),
                        **filters_map,
                    },
                    "facets": None,
                    "data_status": "complete",
                    "data_gaps": [],
                }
        if limit < 1 or limit > 500:
            raise CleanroomException(
                status_code=400,
                code="INVALID_LIMIT",
                message="limit 必须在 1 到 500 之间",
            )
        if offset < 0:
            raise CleanroomException(
                status_code=400, code="INVALID_OFFSET", message="offset 不能为负数"
            )
        with self._lock:
            values = [copy.deepcopy(item) for item in self._assets.values()]
            page = values[offset : offset + limit]
            return {
                "assets": page,
                "total": len(values),
                "limit": limit,
                "offset": offset,
                "next_cursor": str(offset + limit)
                if offset + limit < len(values)
                else None,
                "revision": self._revision,
                **_truthful_meta(),
            }

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        # 没有登记的 asset_id 必须明确 404，不伪造一个空详情。
        with self._lock:
            item = self._assets.get(asset_id)
            if item is None:
                _not_found(ASSET_NOT_FOUND, f"素材 {asset_id} 不存在")
            return copy.deepcopy(item)

    def register_asset(
        self,
        asset_id: str,
        *,
        source_ref: dict[str, Any] | None = None,
        references: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一个由真实上游提交的稳定资产快照。

        该入口只接收调用方已确认的 ID 与来源，不读取本机文件或远程服务；它为
        C8 的统一回收契约提供可回放的最小资产真源。
        """
        asset_id = str(asset_id or "").strip()
        if not asset_id:
            raise CleanroomException(400, "INVALID_REQUEST", "asset_id 不能为空")
        with self._lock:
            if asset_id in self._assets:
                raise CleanroomException(
                    409, ASSET_ALREADY_EXISTS, f"素材 {asset_id} 已登记"
                )
            item: dict[str, Any] = {
                "id": asset_id,
                "asset_id": asset_id,
                "version": 1,
                "source_ref": copy.deepcopy(source_ref)
                if isinstance(source_ref, dict)
                else {},
                "references": copy.deepcopy(references)
                if isinstance(references, dict)
                else {},
                "metadata": copy.deepcopy(metadata)
                if isinstance(metadata, dict)
                else {},
            }
            if isinstance(payload, dict):
                item.update(copy.deepcopy(payload))
            # 资产 ID 是唯一真源，调用方 payload 不能覆盖它。
            item["id"] = asset_id
            item["asset_id"] = asset_id
            item["version"] = int(item.get("version") or 1)
            self._assets[asset_id] = item
            self._revision += 1
            return {
                "asset": copy.deepcopy(item),
                "revision": self._revision,
                **_truthful_meta(),
            }

    # 便于上层按领域语言调用；两者共享同一实现，不生成第二套状态。
    register = register_asset
    upsert_asset = register_asset

    def _assert_expected_version(self, expected_version: int | None) -> None:
        if expected_version is None:
            return
        if type(expected_version) is not int or expected_version < 1:
            raise CleanroomException(
                400,
                "EXPECTED_VERSION_REQUIRED",
                "资产回收操作必须携带正整数 expected_version",
            )
        if expected_version != self._revision:
            raise CleanroomException(
                409,
                "VERSION_CONFLICT",
                "注册表版本冲突，请重新读取后重试",
                extra={
                    "expected_version": expected_version,
                    "current_version": self._revision,
                },
                expose_extra_fields={"expected_version", "current_version"},
            )

    def _next_recycle_id(self) -> str:
        self._recycle_sequence += 1
        return f"rcy_{self._recycle_sequence:04d}"

    @staticmethod
    def _preserved_subjects(asset: dict[str, Any]) -> list[dict[str, str]]:
        """从资产快照提取已存在的稳定引用，不按数组位置生成 ID。"""
        subject: dict[str, str] = {
            "asset_id": str(asset.get("asset_id") or asset.get("id") or "")
        }
        version_id = asset.get("version_id")
        if version_id:
            subject["version_id"] = str(version_id)
        return [subject] if subject["asset_id"] else []

    @staticmethod
    def _recovery_projection(
        *,
        operation: str,
        entry_id: str,
        state: str,
        preserved_subjects: list[dict[str, str]],
        failed_scopes: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "recycle_entry_id": entry_id,
            "state": state,
            "preserved_subjects": copy.deepcopy(preserved_subjects),
            "failed_scopes": list(failed_scopes or []),
            "idempotency_key": idempotency_key or f"{operation}:{entry_id}",
            "occurred_at": _now_iso(),
        }

    def recycle_entry(self, entry_id: str) -> dict[str, Any]:
        """读取单个回收条目；未知状态明确 404，不返回空成功。"""
        with self._lock:
            entry = self._recycle_entries.get(entry_id)
            if entry is None:
                raise CleanroomException(
                    404, RECYCLE_ENTRY_NOT_FOUND, "回收站条目不存在"
                )
            return copy.deepcopy(entry)

    def assert_recycle_entry_restorable(self, entry_id: str) -> dict[str, Any]:
        """恢复前置检查，保证跨服务恢复失败时不先改动另一侧。"""
        with self._lock:
            entry = self._recycle_entries.get(entry_id)
            if entry is None:
                raise CleanroomException(
                    404, RECYCLE_ENTRY_NOT_FOUND, "回收站条目不存在"
                )
            asset_id = str(entry.get("asset_id") or "")
            if not asset_id or not isinstance(entry.get("payload"), dict):
                raise CleanroomException(
                    409,
                    ASSET_RESTORE_PARTIAL_FAILURE,
                    "回收条目快照不完整，无法安全恢复",
                    extra={
                        "partial_failures": [
                            {
                                "entry_id": entry_id,
                                "code": ASSET_RESTORE_PARTIAL_FAILURE,
                            }
                        ]
                    },
                )
            if asset_id in self._assets:
                raise CleanroomException(
                    409, ASSET_RESTORE_CONFLICT, f"恢复目标资产 {asset_id} 已存在"
                )
            return copy.deepcopy(entry)

    def delete_asset(
        self,
        asset_id: str,
        expected_version: int | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """删除登记快照并进入可恢复回收站，保留来源与引用。"""
        with self._lock:
            if idempotency_key and idempotency_key in self._idempotent_results:
                return copy.deepcopy(self._idempotent_results[idempotency_key])
            self._assert_expected_version(expected_version)
            if asset_id not in self._assets:
                _not_found(ASSET_NOT_FOUND, f"素材 {asset_id} 不存在")
            item = copy.deepcopy(self._assets[asset_id])
            entry_id = self._next_recycle_id()
            entry = {
                "entry_id": entry_id,
                "kind": "asset",
                "asset_id": asset_id,
                "payload": item,
                "source_ref": copy.deepcopy(item.get("source_ref") or {}),
                "references": copy.deepcopy(item.get("references") or {}),
                "version_id": item.get("version_id"),
                "idempotency_key": idempotency_key or f"recycle:{entry_id}",
                "occurred_at": _now_iso(),
            }
            del self._assets[asset_id]
            self._recycle_entries[entry_id] = entry
            self._revision += 1
            result = {
                "entry_id": entry_id,
                "asset_id": asset_id,
                "deleted": True,
                "recycle_entry_id": entry_id,
                "references": copy.deepcopy(entry["references"]),
                "source_ref": copy.deepcopy(entry["source_ref"]),
                "revision": self._revision,
                "closure_status": "recovery_required",
                "blocker": None,
                "recovery": self._recovery_projection(
                    operation="recycle",
                    entry_id=entry_id,
                    state="recovery_required",
                    preserved_subjects=self._preserved_subjects(item),
                    idempotency_key=idempotency_key or f"recycle:{entry_id}",
                ),
                "next_action": "restore_reference",
                **_truthful_meta(),
            }
            if idempotency_key:
                self._idempotent_results[idempotency_key] = copy.deepcopy(result)
            return result

    def restore_from_recycle(
        self,
        entry_id: str,
        expected_version: int | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """恢复资产快照；同一条目重复恢复不重复登记。"""
        with self._lock:
            if idempotency_key and idempotency_key in self._idempotent_results:
                return copy.deepcopy(self._idempotent_results[idempotency_key])
            self._assert_expected_version(expected_version)
            entry = self.assert_recycle_entry_restorable(entry_id)
            asset_id = str(entry["asset_id"])
            self._assets[asset_id] = copy.deepcopy(entry["payload"])
            del self._recycle_entries[entry_id]
            self._revision += 1
            result = {
                "entry_id": entry_id,
                "asset_id": asset_id,
                "restored": True,
                "asset": copy.deepcopy(self._assets[asset_id]),
                "references": copy.deepcopy(
                    self._assets[asset_id].get("references") or {}
                ),
                "source_ref": copy.deepcopy(
                    self._assets[asset_id].get("source_ref") or {}
                ),
                "revision": self._revision,
                "closure_status": "closed",
                "blocker": None,
                "recovery": self._recovery_projection(
                    operation="restore",
                    entry_id=entry_id,
                    state="completed",
                    preserved_subjects=self._preserved_subjects(self._assets[asset_id]),
                    idempotency_key=idempotency_key or f"restore:{entry_id}",
                ),
                "next_action": None,
                **_truthful_meta(),
            }
            if idempotency_key:
                self._idempotent_results[idempotency_key] = copy.deepcopy(result)
            return result

    restore_recycle_entry = restore_from_recycle

    def image_versions(self, asset_id: str, *, limit: int = 500) -> dict[str, Any]:
        if limit < 1 or limit > 500:
            raise CleanroomException(
                status_code=400,
                code="INVALID_LIMIT",
                message="limit 必须在 1 到 500 之间",
            )
        self.get_asset(asset_id)
        return {"versions": [], "asset_id": asset_id, **_truthful_meta()}

    def facets(self) -> dict[str, Any]:
        return {
            "kinds": [],
            "categories": [],
            "tags": [],
            "collections": [],
            "roots": [],
            **_truthful_meta(),
        }

    def folders(self, root_id: str | None = None) -> dict[str, Any]:
        return {
            "folders": [],
            "root_id": root_id,
            "revision": self._revision,
            **_truthful_meta(),
        }

    def preferences(self) -> dict[str, Any]:
        with self._lock:
            return {
                "preferences": copy.deepcopy(self._team_preferences),
                **self._meta(),
            }

    def recycle_bin(self) -> dict[str, Any]:
        with self._lock:
            items = [copy.deepcopy(item) for item in self._recycle_entries.values()]
            return {
                "items": items,
                "assets": [item for item in items if item.get("kind") == "asset"],
                "projects": [],
                "canvases": [],
                "revision": self._revision,
                **_truthful_meta(),
            }

    def governance_overview(self) -> dict[str, Any]:
        return {
            "assets": [],
            "projects": [],
            "canvases": [],
            "outbox": {"pending": 0, "failed": 0, "remaining": 0, "replayed": 0},
            "revision": self._revision,
            **_truthful_meta(),
        }

    def templates(self, project_type: str | None = None) -> dict[str, Any]:
        return {
            "templates": [],
            "project_type": project_type,
            "revision": self._revision,
            **_truthful_meta(),
        }

    def workspace_job(self, job_id: str) -> dict[str, Any]:
        _not_found(JOB_NOT_FOUND, f"工作区任务 {job_id} 不存在")

    def assert_feature_id(self, feature_id: str) -> None:
        known = {item["id"] for item in self._features()}
        if feature_id not in known:
            _not_found("FEATURE_NOT_FOUND", f"素材注册表能力 {feature_id} 不存在")

    # ------------------------------------------------------------------
    # MVP2-T2：生产链成片/中间产物回写的来源链（provenance）
    # ------------------------------------------------------------------

    def configure_produced_roots(
        self,
        output_roots: Mapping[str, str | Path],
        *,
        source_roots: Mapping[str, str | Path] | None = None,
    ) -> None:
        """注入产出根与源素材根。

        本票不固化任何默认路径：产出根只能由调用方（部署配置或测试）注入，
        未配置时产物回写一律失败关闭，绝不落到源素材目录。
        空/空白/相对路径同样拒绝，避免解析时落到进程工作目录。
        """
        # 先整体规范化再落库：任一条目非法都不会留下半套根配置。
        normalized_output = _normalized_roots(output_roots)
        normalized_source = _normalized_roots(source_roots or {})
        with self._lock:
            self._output_roots = normalized_output
            self._source_roots = normalized_source

    def _resolve_produced_root(self, target_root_id: str) -> tuple[str, Path]:
        """解析产出根；未配置或位于源素材目录内都失败关闭。"""
        root_id = str(target_root_id or "").strip()
        root_path = self._output_roots.get(root_id)
        if root_path is None:
            raise CleanroomException(
                400,
                UNKNOWN_TARGET_ROOT,
                f"产出根 {root_id or '（空）'} 未配置；产物只能回写到已配置的产出根",
                extra={"target_root_id": root_id},
                expose_extra_fields={"target_root_id"},
            )
        resolved = root_path.resolve()
        if any(
            _is_within(resolved, source.resolve())
            for source in self._source_roots.values()
        ):
            raise CleanroomException(
                400,
                UNKNOWN_TARGET_ROOT,
                "产出根位于源素材目录内；产物回写不得写入源素材目录",
                extra={"target_root_id": root_id},
                expose_extra_fields={"target_root_id"},
            )
        return root_id, resolved

    @staticmethod
    def _write_provenance_sidecar(
        target: Path,
        payload: dict[str, Any],
        *,
        target_root_id: str,
        sidecar_path: str,
    ) -> None:
        """把来源链标记写入产出根内的相对路径；返回值只暴露 root_id 与相对路径。

        产出根可能是已存在的文件、只读目录或权限不足，裸 OSError 的 message 会
        带上本机绝对路径；这里统一包成受控异常，只回显 root_id 与相对 sidecar
        路径，并用 `from None` 断掉原始异常链。
        """
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            raise CleanroomException(
                503,
                PROVENANCE_SIDECAR_WRITE_FAILED,
                "来源链标记写入产出根失败；已拒绝回写，素材注册表未写入该产物",
                extra={"target_root_id": target_root_id, "sidecar_path": sidecar_path},
                expose_extra_fields={"target_root_id", "sidecar_path"},
            ) from None

    def register_produced_asset(
        self,
        asset_id: str,
        *,
        provenance: dict[str, Any],
        target_root_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记生产链产物：来源链必填、产出根必须是已配置的产出根。

        同 asset_id 且同来源链重复回写幂等；来源链或产出根不一致返回 409，
        绝不静默覆盖已有登记。落盘标记只写在产出根下，并在登记前完成，
        落盘失败时注册表不留半成品条目。
        """
        asset_id = str(asset_id or "").strip()
        if not asset_id:
            raise CleanroomException(400, "INVALID_REQUEST", "asset_id 不能为空")
        if not _SAFE_ASSET_ID.fullmatch(asset_id):
            raise CleanroomException(
                400,
                "INVALID_REQUEST",
                "asset_id 只允许字母、数字、点、下划线与连字符",
            )
        normalized = _normalize_produced_provenance(provenance)
        root_id, resolved_root = self._resolve_produced_root(target_root_id)
        with self._lock:
            existing = self._assets.get(asset_id)
            if existing is None:
                item: dict[str, Any] = {
                    "id": asset_id,
                    "asset_id": asset_id,
                    "version": 1,
                    "source_ref": {},
                    "references": {},
                    "metadata": copy.deepcopy(metadata)
                    if isinstance(metadata, dict)
                    else {},
                    "provenance": copy.deepcopy(normalized),
                    "target_root_id": root_id,
                }
            else:
                if existing.get("provenance") != normalized or existing.get(
                    "target_root_id"
                ) != root_id:
                    raise CleanroomException(
                        409,
                        PROVENANCE_CONFLICT,
                        f"素材 {asset_id} 已登记且来源链或产出根不一致，已拒绝覆盖",
                    )
                item = existing
            sidecar_path = f"{_PROVENANCE_SIDECAR_DIR}/{asset_id}.json"
            self._write_provenance_sidecar(
                resolved_root / _PROVENANCE_SIDECAR_DIR / f"{asset_id}.json",
                {
                    "asset_id": asset_id,
                    "target_root_id": root_id,
                    "provenance": normalized,
                },
                target_root_id=root_id,
                sidecar_path=sidecar_path,
            )
            if existing is None:
                self._assets[asset_id] = item
                self._revision += 1
            return {
                "asset_id": asset_id,
                "asset": copy.deepcopy(item),
                "provenance": copy.deepcopy(normalized),
                "target_root_id": root_id,
                "sidecar_path": sidecar_path,
                "revision": self._revision,
                **_truthful_meta(),
            }

    def provenance_chain(self, asset_id: str) -> dict[str, Any]:
        """按 asset_id 反查来源链，并标注每个参考素材当前是否已登记。"""
        with self._lock:
            item = self._assets.get(str(asset_id or "").strip())
            if item is None:
                _not_found(ASSET_NOT_FOUND, f"素材 {asset_id} 不存在")
            provenance = item.get("provenance")
            if not isinstance(provenance, dict) or not provenance:
                raise CleanroomException(
                    404,
                    PROVENANCE_NOT_FOUND,
                    f"素材 {asset_id} 没有来源链登记，无法反查产出来源",
                )
            provenance = copy.deepcopy(provenance)
            references = [
                {"asset_id": reference, "exists": reference in self._assets}
                for reference in provenance.get("reference_asset_ids", [])
            ]
            return {
                "asset_id": item["asset_id"],
                "provenance": provenance,
                "references": references,
                "target_root_id": item.get("target_root_id"),
                "revision": self._revision,
                **_truthful_meta(),
            }


# 单例只保存非敏感、进程内可重建状态。
default_asset_registry_service = AssetRegistryService()


__all__ = [
    "ASSET_ALREADY_EXISTS",
    "ASSET_REGISTRY_NOT_INTEGRATED",
    "ASSET_NOT_FOUND",
    "ASSET_RESTORE_CONFLICT",
    "ASSET_RESTORE_PARTIAL_FAILURE",
    "DATA_STATUS_NOT_INTEGRATED",
    "DATA_STATUS_OK",
    "JOB_NOT_FOUND",
    "PROVENANCE_CONFLICT",
    "PROVENANCE_NOT_FOUND",
    "PROVENANCE_REQUIRED",
    "PROVENANCE_SIDECAR_WRITE_FAILED",
    "UNKNOWN_TARGET_ROOT",
    "normalize_asset_query",
    "parse_utc_datetime",
    "RECYCLE_ENTRY_NOT_FOUND",
    "AssetRegistryService",
    "default_asset_registry_service",
    "_not_integrated",
]
