# -*- coding: utf-8 -*-
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

"""Phase 12 素材注册表 API（真实落盘数据源）。

真实数据源：`gw.asset_registry.repository`（core.storage.JsonState 命名空间
`asset_registry`）+ 允许根目录内的真实本机文件 + ffmpeg/ffprobe 真实媒体处理。

安全口径：
- 认证先于参数校验，未认证一律 401；
- 本机文件访问仅限 `GW_ALLOWED_ROOTS`，未配置即 403 失败关闭，不回显原始绝对路径；
- 外部依赖（ffmpeg / 允许根目录 / 可读取媒体）缺失一律 503，禁止伪造数据。
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, Response, status, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from gw.asset_registry import governance, media_probe, repository as repo, index_jobs
from gw.asset_registry.service import (
    ASSET_REGISTRY_NOT_INTEGRATED,
    DATA_STATUS_NOT_INTEGRATED,
    DATA_STATUS_OK,
    default_asset_registry_service,
)
from gw.core import jxl_tool, storage
from gw.core.auth import require_authenticated, require_edit_access, require_governance_access
from gw.core.errors import CleanroomException
from gw.projects_hub.service import (
    default_projects_service,
    owner_key_for_context,
    require_project_write_context,
)
from gw.settings.models import StorageSettingsPatchRequest
from gw.settings.service import default_storage_settings_service

from gw.core.asset_visibility import asset_owner_key, asset_request_scope

router = APIRouter(prefix="/api/asset-registry", tags=["asset-registry"], dependencies=[Depends(asset_request_scope)])

MAX_IMPORT_BYTES = 64 * 1024 * 1024


def _read_auth(authorization: Optional[str], x_user_role: str):
    """统一读取权限检查。"""
    return require_authenticated(authorization, x_user_role)


def _edit_auth(authorization: Optional[str], x_user_role: str):
    """统一编辑权限检查。"""
    return require_edit_access(authorization, x_user_role)


def _governance_auth(authorization: Optional[str], x_user_role: str):
    """统一治理权限检查。"""
    return require_governance_access(authorization, x_user_role)


def _payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _ids(payload: Dict[str, Any], key: str = "asset_ids") -> List[str]:
    value = payload.get(key) or []
    return [str(item) for item in value] if isinstance(value, list) else []


def _version(payload: Dict[str, Any], *, required: bool = False) -> Optional[int]:
    raw = payload.get("expected_version")
    if raw is None and not required:
        return None
    if type(raw) is not int or raw < 1:
        raise CleanroomException(400, "INVALID_REQUEST", "expected_version 必须为正整数")
    return raw


def _closure_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _asset_closure_projection(
    result: Dict[str, Any],
    *,
    asset_id: Optional[str] = None,
    operation: str,
    next_action: Optional[str] = None,
    closure_status: str,
    version_id: Optional[str] = None,
    failed_scopes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """把既有注册表结果补成 C9 收口投影，不改变原有字段。"""
    entry_id = str(result.get("recycle_entry_id") or result.get("entry_id") or "")
    subject: Dict[str, str] = {}
    if asset_id:
        subject["asset_id"] = str(asset_id)
    if version_id:
        subject["version_id"] = str(version_id)
    recovery = {
        "operation": operation,
        "recycle_entry_id": entry_id,
        "state": "completed" if closure_status == "closed" else closure_status,
        "preserved_subjects": [subject] if subject else [],
        "failed_scopes": list(failed_scopes or []),
        "idempotency_key": f"{operation}:{entry_id}" if entry_id else None,
        "occurred_at": _closure_now(),
    }
    return {
        **result,
        "closure_status": closure_status,
        "blocker": None,
        "recovery": recovery,
        "next_action": next_action,
    }


def _recycle_entry_projection(entry: Dict[str, Any]) -> Dict[str, Any]:
    """为回收站读模型补充稳定资产引用和恢复动作。"""
    if entry.get("closure_status") or entry.get("kind") != "asset":
        return entry
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    asset_id = entry.get("asset_id") or payload.get("asset_id")
    return _asset_closure_projection(
        entry,
        asset_id=str(asset_id) if asset_id else None,
        version_id=payload.get("version_id"),
        operation="recycle",
        closure_status="recovery_required",
        next_action="restore_reference",
    )


def _recycle_asset_subject(entry_id: str) -> tuple[Optional[str], Optional[str]]:
    """在删除回收条目真正消费前读取其中的稳定资产/版本引用。"""
    entry = repo.state().read().get("recycle_bin", {}).get(entry_id) or {}
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    asset_id = entry.get("asset_id") or payload.get("asset_id")
    return (str(asset_id) if asset_id else None, payload.get("version_id"))


async def _json_body(request: Request) -> Dict[str, Any]:
    """宽容读取 JSON 请求体；空体或非对象等价于空字典。"""
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _ascii_filename(name: str, fallback: str = "export") -> str:
    """HTTP 头只能承载 latin-1；中文名降级为 ASCII 回退名，原始名走 filename*。"""
    clean = "".join(ch for ch in (name or "") if ch not in '\\/:*?"<>|\r\n').strip()
    ascii_only = clean.encode("ascii", "ignore").decode("ascii").strip(" ._-")
    return ascii_only or fallback


def _content_disposition(name: str, fallback: str, suffix: str) -> str:
    """RFC 5987 编码附件名，避免中文名触发 latin-1 头编码错误。"""
    from urllib.parse import quote
    clean = "".join(ch for ch in (name or "") if ch not in '\\/:*?"<>|\r\n').strip() or fallback
    return "attachment; filename=\"%s%s\"; filename*=UTF-8''%s%s" % (
        _ascii_filename(clean, fallback), suffix, quote(clean, safe=""), suffix)


def _pdf_response(asset_ids: List[str], name: str) -> Response:
    """把素材清单导出为合法 PDF 字节流。"""
    content, exported = repo.export_pdf_bundle(asset_ids)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(name, "export", ".pdf"),
            "X-PDF-Exported": str(exported),
            "X-PDF-Skipped": "0",
        },
    )


# ---------------------------------------------------------------------------
# 只读真值接口
# ---------------------------------------------------------------------------


@router.get("", summary="读取素材注册表总览", status_code=status.HTTP_200_OK)
def get_registry_root(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    raw = repo.state().read()
    from gw.core.asset_visibility import asset_visible
    return {
        "registry": "asset-registry",
        "revision": raw["revision"],
        "assets": repo.annotate_asset_provenance(
            [a for a in raw["assets"].values() if asset_visible(a)], raw=raw
        ),
        "features": repo.features_snapshot()["features"],
        "backend": "json-state",
        "data_status": DATA_STATUS_OK,
        "data_gaps": [],
    }


@router.get("/status", summary="读取素材注册表状态", status_code=status.HTTP_200_OK)
def get_registry_status(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    actor = _read_auth(authorization, x_user_role)
    raw = repo.state().read()
    from gw.core.asset_visibility import asset_visible
    visible_count = sum(asset_visible(asset) for asset in raw['assets'].values())
    snapshot = repo.features_snapshot()
    return {
        "ready": True,
        "backend": "json-state",
        "revision": raw["revision"],
        "assets_count": visible_count,
        "jobs": index_jobs.service().listing(actor),
        "overview": {
            "assets": visible_count,
            "projects": len({e.get("project_id") for e in raw["project_entities"].values()}),
            "canvases": 0,
            "roots": [],
            "features": [
                {"id": key, "name": FEATURE_NAMES.get(key, key), "enabled": bool(value),
                 "data_status": DATA_STATUS_OK}
                for key, value in snapshot["features"].items()
            ],
            "index_automation": snapshot["index_automation"],
        },
        "features": [
            {"id": key, "name": FEATURE_NAMES.get(key, key), "enabled": bool(value)}
            for key, value in snapshot["features"].items()
        ],
        "data_status": DATA_STATUS_OK,
        "data_gaps": [],
    }


FEATURE_NAMES = {
    "thumbnail": "自动缩略图",
    "video_storyboard": "视频九宫格缩略图",
    "metadata": "元数据解析",
    "fulltext": "全文检索",
    "font": "字体识别",
    "model": "模型识别",
    "index_automation": "索引自动化",
}


@router.get("/assets", summary="分页读取素材登记", status_code=status.HTTP_200_OK)
def list_registry_assets(
    limit: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    dynamic_limit: Optional[int] = Query(None, ge=1, le=1500),
    cursor: Optional[str] = Query(None),
    sort: Optional[str] = Query(None),
    view: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    include_tags: Optional[str] = Query(None),
    exclude_tags: Optional[str] = Query(None),
    root_id: Optional[str] = Query(None),
    folder_path: Optional[str] = Query(None),
    board_sources: Optional[str] = Query(None),
    collection_id: Optional[str] = Query(None),
    recent_days: Optional[int] = Query(None),
    archived: Optional[bool] = Query(None),
    scope: Optional[str] = Query(None),
    text: Optional[str] = Query(None),
    filters: Optional[str] = Query(None),
    include: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    shot_size: Optional[str] = Query(None),
    camera_movement: Optional[str] = Query(None),
    focal_length: Optional[str] = Query(None),
    lighting_phase: Optional[str] = Query(None),
    aspect_ratio: Optional[str] = Query(None),
    include_removed: bool = Query(False),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    structured = any(value is not None for value in (scope, text, filters, include))
    if structured and any(
        value is not None
        for value in (
            entity_type,
            entity_id,
            shot_size,
            camera_movement,
            focal_length,
            lighting_phase,
            aspect_ratio,
        )
    ):
        raise CleanroomException(400, "UNSUPPORTED_FILTER_VALUE", "结构化查询不能混用旧版过滤参数")
    if limit is None:
        effective_limit = 50 if structured else 100
    else:
        try:
            effective_limit = int(limit)
        except (TypeError, ValueError):
            raise CleanroomException(400, "INVALID_LIMIT", "limit 必须是整数") from None
    if type(effective_limit) is not int or effective_limit < 1 or effective_limit > (200 if structured else 1500):
        raise CleanroomException(400, "INVALID_LIMIT", "limit 超出当前查询允许范围")
    structured_text = text
    legacy_text = query or search
    if structured and legacy_text:
        if structured_text and structured_text != legacy_text:
            raise CleanroomException(400, "UNSUPPORTED_FILTER_VALUE", "结构化查询不能混用 query/search")
        structured_text = legacy_text
    if project_id:
        if "?" in project_id:
            project_id = project_id.split("?")[0]
        owner_key = None
        role = str(getattr(context, "role", "") or "")
        try:
            owner_key = owner_key_for_context(context)
        except CleanroomException:
            owner_key = None
        from gw.projects_hub import service as projects_service_module
        if not projects_service_module.default_projects_service.can_read_project(project_id, owner_key=owner_key, role=role):
            raise CleanroomException(404, "PROJECT_NOT_FOUND", "项目不存在")
    return repo.list_assets(effective_limit, offset, query or search, kind, archived, cursor=cursor, sort=sort,
                            view=view, category=category, tag=tag, project_id=project_id,
                            include_tags=include_tags, exclude_tags=exclude_tags, root_id=root_id,
                            collection_id=collection_id, recent_days=recent_days,
                            structured=structured, scope=scope, text=structured_text,
                            filters=filters, include=include,
                            entity_type=entity_type, entity_id=entity_id,
                            shot_size=shot_size, camera_movement=camera_movement,
                            focal_length=focal_length, lighting_phase=lighting_phase,
                            aspect_ratio=aspect_ratio,
                            include_removed=include_removed, dynamic_limit=dynamic_limit,
                            folder_path=folder_path, board_sources=board_sources)


#: 镜头级检索单次返回上限，防止一次下钻把整库镜头全部返回。
MAX_SHOT_SEARCH_LIMIT = 50
DEFAULT_SHOT_SEARCH_LIMIT = 20


def _shot_search_limit(raw: Optional[str]) -> int:
    """按字符串解析镜头检索 limit，缺省 20，越界或非整数返回 400。"""
    if raw is None:
        return DEFAULT_SHOT_SEARCH_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CleanroomException(400, "INVALID_LIMIT", "limit 必须是整数") from None
    if type(value) is not int or value < 1 or value > MAX_SHOT_SEARCH_LIMIT:
        raise CleanroomException(
            400,
            "INVALID_LIMIT",
            "limit 超出当前查询允许范围（1 到 %d）" % MAX_SHOT_SEARCH_LIMIT,
        )
    return value


def _keyword_recalled_assets(query: str, limit: int) -> List[str]:
    """补齐「镜头文本命中、但资产检索文档未命中」的候选资产（MVP1-AC4）。

    检索文档只收录资产级标签与路径，镜头标签与摘要由 ``list_assets`` 既有的
    关键词匹配覆盖；这里只取候选 id，词面加权仍由镜头下钻完成，因此不改变
    既有资产检索的召回口径与排序。
    """
    candidate_limit = min(max(limit * 3, limit), 100)
    try:
        page = repo.list_assets(candidate_limit, 0, query, None, None)
    except CleanroomException:
        return []
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        return []
    asset_ids: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or item.get("id") or "").strip()
        if asset_id and asset_id not in asset_ids:
            asset_ids.append(asset_id)
    return asset_ids


@router.get("/search/shots", summary="镜头级素材检索", status_code=status.HTTP_200_OK)
def search_registry_shots(
    q: Optional[str] = Query(None),
    limit: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """把候选资产下钻到镜头，返回可直接跳转的起止时间戳（MVP1-AC4）。

    候选资产复用 ``HybridAssetSearcher`` 的双轨召回（关键词/向量、限流与降级
    口径与资产检索完全一致），再按登记镜头做标签与摘要的词面加权。索引文件
    不存在时如实返回 503，不伪造空结果或假成功。
    """
    _read_auth(authorization, x_user_role)
    query = (q or "").strip()
    if not query:
        raise CleanroomException(400, "INVALID_QUERY", "检索词不能为空")
    bounded_limit = _shot_search_limit(limit)

    from gw.asset_registry.hybrid_search import (
        HybridAssetSearcher,
        normalize_shot_records,
    )
    from gw.core.sqlite_vec_engine import MAX_QUERY_LENGTH, SQLiteVecEngine

    if len(query) > MAX_QUERY_LENGTH:
        raise CleanroomException(
            400, "INVALID_QUERY", "检索词长度不得超过 %d 个字符" % MAX_QUERY_LENGTH
        )
    db_path = storage.data_root() / repo.ASSET_SEARCH_INDEX_FILENAME
    if not db_path.is_file():
        raise CleanroomException(
            503,
            "SEARCH_INDEX_NOT_READY",
            "本地检索索引尚未建立，请先重建素材索引",
            extra={"data_status": DATA_STATUS_NOT_INTEGRATED},
            expose_extra_fields={"data_status"},
        )

    located: Dict[str, Dict[str, Any]] = {}
    with SQLiteVecEngine(db_path) as engine:

        def _shot_provider(asset_id: str) -> List[Dict[str, Any]]:
            """返回该资产已登记镜头；顺带补齐索引中的内容哈希。"""
            try:
                record = repo.get_asset(asset_id)
            except CleanroomException:
                return []
            located[asset_id] = record
            metadata = record.get("metadata")
            segments = metadata.get("video_segments") if isinstance(metadata, dict) else None
            records = normalize_shot_records(segments)
            if not records:
                return []
            hashes = {
                row.shot_index: row.content_hash
                for row in engine.list_shots(asset_id=asset_id)
            }
            return [
                {**record_row, "content_hash": hashes.get(record_row["shot_index"])}
                for record_row in records
            ]

        results = HybridAssetSearcher(engine).search_shots(
            query,
            limit=bounded_limit,
            shot_provider=_shot_provider,
            extra_asset_ids=_keyword_recalled_assets(query, bounded_limit),
        )
        # 本次请求未携带查询向量，引擎侧的降级标记恒为 false；此处按索引实况
        # 补正（MVP1-T7/F2）：没有登记嵌入模型或引擎未启用向量轨时如实报告降级，
        # 不让「向量轨从未写入」被伪装成双轨正常。
        index_metadata = engine.index_metadata()
        vector_track_ready = bool(
            engine.vector_enabled
            and index_metadata is not None
            and index_metadata.model_id
        )

    items = []
    for hit in results.hits:
        record = located.get(hit.asset_id) or {}
        items.append(
            {
                "asset_id": hit.asset_id,
                "granularity": hit.granularity,
                "name": str(record.get("name") or ""),
                "kind": str(record.get("kind") or ""),
                "display_path": record.get("display_path"),
                "shot_index": hit.shot_index,
                "start_sec": hit.start_sec,
                "end_sec": hit.end_sec,
                "score": round(hit.score, 6),
                "asset_score": round(hit.asset_score, 6),
                "lexical_hits": hit.lexical_hits,
                "tags": list(hit.tags),
                "summary": hit.summary,
                "content_hash": hit.content_hash,
            }
        )
    return {
        "query": query,
        "granularity": "shot",
        "limit": bounded_limit,
        "items": items,
        "shot_count": sum(1 for item in items if item["granularity"] == "shot"),
        "vector_track_degraded": bool(
            results.vector_track_degraded or not vector_track_ready
        ),
        "data_status": DATA_STATUS_OK,
        "data_gaps": [],
    }


@router.post(
    "/search-index/rebuild",
    summary="从注册表重建本地检索索引（文档 + 镜头 + 元数据）",
    status_code=status.HTTP_200_OK,
)
def rebuild_asset_search_index(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """产品化的索引重建入口（MVP1-AC10 / T7-F2）。

    入库本身已带索引写入（``repository._upsert_asset_search_documents``），本端点
    用于索引文件丢失、schema 调整或人工要求全量重建：只读 JSON 注册表真源，
    先写临时文件再原子替换，失败保留旧索引并如实返回 500
    ``SEARCH_INDEX_REBUILD_FAILED``。响应只给统计与索引文件名，不回显绝对路径。
    """
    _edit_auth(authorization, x_user_role)
    try:
        result = repo.rebuild_search_index()
    except CleanroomException:
        raise
    except Exception as exc:
        raise CleanroomException(
            500,
            "SEARCH_INDEX_REBUILD_FAILED",
            "本地检索索引重建失败，已保留原有索引",
        ) from exc
    return {
        "rebuilt": True,
        "documents": result["documents"],
        "shots": result["shots"],
        "skipped_segments": result["skipped_segments"],
        "index_file": repo.ASSET_SEARCH_INDEX_FILENAME,
        "data_status": DATA_STATUS_OK,
        "data_gaps": [],
    }


@router.get("/assets/{asset_id}", summary="读取素材登记详情", status_code=status.HTTP_200_OK)
def get_registry_asset(
    asset_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return {"asset": repo.get_asset(asset_id), "data_status": DATA_STATUS_OK}


@router.get("/assets/{asset_id}/image-versions", summary="读取素材图片版本", status_code=status.HTTP_200_OK)
def list_image_versions(
    asset_id: str,
    limit: int = Query(500, ge=1, le=500),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return repo.list_image_versions(asset_id)


@router.get("/assets/{asset_id}/image-versions/{version_id}/media", summary="读取图片版本媒体", status_code=status.HTTP_200_OK)
def get_image_version_media(
    asset_id: str,
    version_id: str,
    range_header: Optional[str] = Header(None, alias="Range"),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    path = repo.media_file(asset_id, version_id)
    if path is None:
        record = repo.get_asset(asset_id)
        if record.get("metadata", {}).get("workflow_owner"):
            from gw.god_workflow import registry as workflow_registry
            resolved = workflow_registry.resolve_asset_file(context, asset_id)
            if resolved:
                private_path, asset = resolved
                return _media_file_response(
                    private_path, asset["content_type"], range_header
                )
        repo._unavailable(
            "/api/asset-registry/assets/{asset_id}/image-versions/{version_id}/media",
            "MEDIA_NOT_AVAILABLE", "该版本没有可读取的媒体文件")
    return _media_file_response(
        path,
        _media_type(path),
        range_header,
        {"X-Media-Display": _header_safe(repo.storage.relative_display(path))},
    )


@router.get("/assets/{asset_id}/media", summary="读取素材媒体", status_code=status.HTTP_200_OK)
def get_asset_media(
    asset_id: str,
    range_header: Optional[str] = Header(None, alias="Range"),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    path = repo.media_file(asset_id)
    if path is None:
        record = repo.get_asset(asset_id)
        if record.get("metadata", {}).get("workflow_owner"):
            from gw.god_workflow import registry as workflow_registry
            resolved = workflow_registry.resolve_asset_file(context, asset_id)
            if resolved:
                private_path, asset = resolved
                return _media_file_response(
                    private_path, asset["content_type"], range_header
                )
        repo._unavailable("/api/asset-registry/assets/{asset_id}/media",
                          "MEDIA_NOT_AVAILABLE", "该素材没有可读取的媒体文件")
    return _media_file_response(
        path,
        _media_type(path),
        range_header,
        {"X-Media-Display": _header_safe(repo.storage.relative_display(path))},
    )


def _header_safe(value: str) -> str:
    """把展示串降级为 HTTP 头可承载的 latin-1 内容。"""
    return (value or "").encode("ascii", "replace").decode("ascii")


def _media_file_response(path, media_type: str, range_header: Optional[str], extra_headers=None):
    """流式返回本地媒体，支持单段 Range 且不把完整文件载入内存。"""
    size = path.stat().st_size
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private",
        "Content-Length": str(size),
        **(extra_headers or {}),
    }
    if range_header is None:
        return FileResponse(path, media_type=media_type, headers=base_headers)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip(), re.IGNORECASE)
    if (
        not match
        or size == 0
        or (not match.group(1) and not match.group(2))
        or len(match.group(1)) > 20
        or len(match.group(2)) > 20
    ):
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private",
                "Content-Range": f"bytes */{size}",
                **(extra_headers or {}),
            },
        )

    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            start = end = -1
    else:
        suffix_length = int(last)
        if suffix_length <= 0:
            start = end = -1
        else:
            start = max(0, size - suffix_length)
            end = size - 1
    if start < 0:
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private",
                "Content-Range": f"bytes */{size}",
                **(extra_headers or {}),
            },
        )

    length = end - start + 1

    def stream_range():
        with path.open("rb") as media_file:
            media_file.seek(start)
            remaining = length
            while remaining:
                chunk = media_file.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        stream_range(),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private",
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
            **(extra_headers or {}),
        },
    )


def _media_type(path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".jxl": "image/jxl",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm",
        ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
    }.get(suffix, "application/octet-stream")


@router.get("/assets/{asset_id}/video/frame", summary="读取视频帧", status_code=status.HTTP_200_OK)
def get_video_frame(
    asset_id: str,
    at: Optional[float] = Query(None, ge=0),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return media_probe.extract_frame(asset_id, float(at or 0))


@router.get("/assets/{asset_id}/video/storyboard", summary="读取视频分镜", status_code=status.HTTP_200_OK)
def get_video_storyboard(
    asset_id: str,
    version: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    # 缓存根由媒体仓储统一解析（cache_folder 口径），本模块不反向依赖其内部路径规则。
    from gw.media import repository as media_repo

    return media_probe.storyboard(
        asset_id, version, cache_root=media_repo.cache_root_relative()
    )


@router.get("/facets", summary="读取素材筛选维度", status_code=status.HTTP_200_OK)
def get_registry_facets(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return repo.facets()


@router.get("/folders", summary="读取素材文件夹登记", status_code=status.HTTP_200_OK)
def get_registry_folders(
    root_id: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return repo.folders(root_id)


@router.get("/governance/overview", summary="读取治理回收概览", status_code=status.HTTP_200_OK)
def get_governance_overview(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _governance_auth(authorization, x_user_role)
    return repo.governance_overview()


@router.get("/governance/cascade-preview", summary="读取级联影响预览", status_code=status.HTTP_200_OK)
def get_cascade_preview(
    target_type: str = Query(..., min_length=1),
    target_id: str = Query(..., min_length=1),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _governance_auth(authorization, x_user_role)
    if target_type != "asset":
        # 画布/项目域不在本块范围：如实返回空影响集，不伪造跨域关系。
        return {"target_type": target_type, "target_id": target_id,
                "impact": {"assets": [], "projects": [], "canvases": [], "total": 0},
                "data_status": DATA_STATUS_NOT_INTEGRATED,
                "data_gaps": ["governance_store_not_connected"]}
    result = repo.cascade_preview(target_id)
    return {
        "target_type": target_type, "target_id": target_id,
        "impact": {"assets": [target_id], "projects": result["projects"],
                   "canvases": [], "total": len(result["projects"]) + 1},
        "data_status": DATA_STATUS_OK, "data_gaps": [],
    }


@router.get("/governance/suggestions", summary="读取治理建议清单（MVP1-T5，只建议）", status_code=status.HTTP_200_OK)
def get_governance_suggestions(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """治理建议清单：命名/相似名/查重/向量近重/受控打标，只读不落任何字段。"""
    context = _governance_auth(authorization, x_user_role)
    result = governance.build_suggestions()
    result["operator_role"] = context.role
    return result


@router.post("/governance/suggestions/confirm", summary="确认单条治理建议（写入确认字段）", status_code=status.HTTP_200_OK)
def confirm_governance_suggestion(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """逐条确认建议：强制 expected_version CAS；确认动作追加只读审计记录。"""
    context = _governance_auth(authorization, x_user_role)
    body = _payload(payload)
    suggestion_id = str(body.get("suggestion_id") or "").strip()
    if not suggestion_id:
        raise CleanroomException(400, "INVALID_REQUEST", "suggestion_id 不能为空")
    expected_version = _version(body, required=True)
    return governance.confirm_suggestion(
        suggestion_id,
        expected_version,
        operator=context.subject or None,
    )


@router.get("/governance/suggestions/confirmations", summary="读取治理建议确认记录", status_code=status.HTTP_200_OK)
def get_governance_suggestion_confirmations(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _governance_auth(authorization, x_user_role)
    return governance.list_confirmations()


@router.get("/preferences/team", summary="读取团队素材偏好", status_code=status.HTTP_200_OK)
def get_team_preferences(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return repo.team_preferences()


@router.get("/presets", summary="读取素材预设", status_code=status.HTTP_200_OK)
def get_registry_presets(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return repo.list_presets()


@router.get("/project-directory-templates", summary="读取项目目录模板", status_code=status.HTTP_200_OK)
def get_project_directory_templates(
    project_type: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    result = repo.list_directory_templates(project_type)
    return {**result, "templates": [_template_view(item, {}) for item in result["templates"]]}


@router.get("/recycle-bin", summary="读取素材回收站", status_code=status.HTTP_200_OK)
def get_recycle_bin(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    raw = repo.state().read()
    from gw.core.asset_visibility import asset_visible
    visible = [
        _recycle_entry_projection(dict(value))
        for value in raw['recycle_bin'].values()
        if value.get('kind') != 'asset' or asset_visible(value.get('payload', {}))
    ]
    return {"items": visible,
            "assets": [value for value in visible if value.get("kind") == "asset"],
            "projects": [], "canvases": [],
            "revision": raw["revision"], "data_status": DATA_STATUS_OK, "data_gaps": []}


@router.get("/remote-assets", summary="读取远程素材登记", status_code=status.HTTP_200_OK)
def get_remote_assets(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    return repo.list_remote_assets()


@router.get("/workspace-jobs/{job_id}", summary="读取工作区任务", status_code=status.HTTP_200_OK)
def get_workspace_job(
    job_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    actor = _read_auth(authorization, x_user_role)
    job = index_jobs.service().store.read()["jobs"].get(job_id)
    if job and job.get("project_id"):
        owner_key = None
        role = str(getattr(actor, "role", "") or "")
        try:
            owner_key = owner_key_for_context(actor)
        except CleanroomException:
            owner_key = None
        if not default_projects_service.can_read_project(job["project_id"], owner_key=owner_key, role=role):
            raise CleanroomException(404, "WORKSPACE_JOB_NOT_FOUND", "工作区任务不存在")
    return index_jobs.service().detail(job_id, actor)

# ---------------------------------------------------------------------------
# 写入端点：真实落盘，写后可读回
# ---------------------------------------------------------------------------


@router.post("/assets/import", summary="导入素材登记", status_code=status.HTTP_200_OK)
async def import_registry_assets(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """导入素材：JSON 批量登记，或 multipart 单文件真实落盘到数据目录。

    真实文件写入数据目录（不是素材目录），文件名由服务端生成，不信任调用方路径。
    """
    _edit_auth(authorization, x_user_role)
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise CleanroomException(400, "INVALID_REQUEST", "缺少 file 上传字段")
        data = await upload.read()
        if not data:
            raise CleanroomException(400, "INVALID_REQUEST", "上传文件为空")
        if len(data) > MAX_IMPORT_BYTES:
            raise CleanroomException(413, "PAYLOAD_TOO_LARGE", "上传文件超过 64MB 上限")
        name = str(form.get("name") or getattr(upload, "filename", "") or "未命名素材").strip()
        result = repo.import_assets([{"name": name, "kind": _kind_from_name(name),
                                      "size_bytes": len(data)}], "upload", None)
        asset_id = result["asset_ids"][0]
        target = repo.stored_upload_path(asset_id, name)

        def mutate(raw: Dict[str, Any]) -> Any:
            item = raw["assets"][asset_id]
            item["display_path"] = None
            item["stored_name"] = target.name
            item["size_bytes"] = len(data)
            return raw
        repo.state().mutate(mutate)
        target.write_bytes(data)
        asset = repo.get_asset(asset_id)
        return {"asset": asset, "assets": [asset], "asset_ids": [asset_id],
                "revision": repo.state().read()["revision"],
                "data_status": DATA_STATUS_OK, "data_gaps": []}
    payload = await _json_body(request)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        if payload.get("name"):
            items = [payload]
        else:
            raise CleanroomException(400, "INVALID_REQUEST", "缺少 items 或 name")
    return repo.import_assets(items, str(payload.get("source_kind") or "manual"), _version(payload))


def _kind_from_name(name: str) -> str:
    """按扩展名判定素材种类；未命中即为 unknown，不猜测。"""
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}:
        return "image"
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}:
        return "audio"
    if suffix in {".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv"}:
        return "document"
    return "unknown"


@router.post("/assets/archive", summary="打包下载所选素材", status_code=status.HTTP_200_OK)
def archive_registry_assets(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """前端「打包下载」入口：把所选素材的真实本地文件打包为 zip。

    无任何可打包文件时 503（不返回空假包）；越界/未配置根目录同样失败关闭。
    """
    _read_auth(authorization, x_user_role)
    body = _payload(payload)
    asset_ids = _ids(body)
    if not asset_ids:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 asset_ids")
    content = repo.export_archive(asset_ids)
    name = str(body.get("name") or "assets")
    return Response(content=content, media_type="application/zip",
                    headers={"Content-Disposition": _content_disposition(name, "assets", ".zip"),
                             "X-Archive-Filename": _ascii_filename(name, "assets") + ".zip"})


@router.post("/assets/export-pdf", summary="导出素材 PDF", status_code=status.HTTP_200_OK)
def export_registry_pdf(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    body = _payload(payload)
    return _pdf_response(_ids(body), str(body.get("name") or "资产导出"))


@router.post("/assets/archive-bundle", include_in_schema=False)
def export_registry_archive(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """把所选素材的本地文件真实打包为 zip（无可用文件即 503）。"""
    _read_auth(authorization, x_user_role)
    body = _payload(payload)
    content = repo.export_archive(_ids(body))
    return Response(content=content, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="assets.zip"'})


@router.post("/assets/relations", summary="建立素材关系", status_code=status.HTTP_200_OK)
def create_asset_relation(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    asset_ids = _ids(body)
    if len(asset_ids) >= 2:
        return repo.related_assets(asset_ids, str(body.get("relation_type") or body.get("kind") or "related"),
                                   _version(body))
    from_asset_id = str(body.get("from_asset_id") or "")
    to_asset_id = str(body.get("to_asset_id") or "")
    if not from_asset_id or not to_asset_id:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 asset_ids 或 from_asset_id/to_asset_id")
    return repo.create_relation(from_asset_id, to_asset_id,
                                str(body.get("relation_type") or body.get("kind") or "related"),
                                _version(body))


@router.post("/assets/resolve-reference", summary="解析素材引用", status_code=status.HTTP_200_OK)
def resolve_asset_reference(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    body = _payload(payload)
    reference = str(body.get("asset_id") or "")
    if not reference:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 asset_id")
    return repo.resolve_reference(reference)


@router.post("/assets/tags", summary="批量添加标签", status_code=status.HTTP_200_OK)
def add_asset_tags(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    names = body.get("names") or body.get("tags") or []
    names = [str(n) for n in names] if isinstance(names, list) else []
    result = repo.add_tags(_ids(body), names, _version(body))
    return {**result, "assets": repo.assets_snapshot(_ids(body))}


@router.patch("/assets/{asset_id}", summary="更新素材登记", status_code=status.HTTP_200_OK)
def update_registry_asset(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    raw_asset = repo.get_asset(asset_id)
    if str(getattr(context, "role", "") or "").lower() not in {"editor", "admin", "governor"}:
        raise CleanroomException(404, "ASSET_NOT_FOUND", f"素材 {asset_id} 不存在")
    project_ids = list(raw_asset.get("project_ids") or [])
    if raw_asset.get("project_id") and raw_asset["project_id"] not in project_ids:
        project_ids.append(raw_asset["project_id"])
    for pid in project_ids:
        try:
            require_project_write_context(pid, context)
        except CleanroomException as exc:
            if exc.status_code == 404:
                raise CleanroomException(404, "ASSET_NOT_FOUND", f"素材 {asset_id} 不存在")
            raise
    return repo.update_asset(asset_id, _payload(payload))


@router.delete("/assets/{asset_id}", summary="删除素材（移入回收站）", status_code=status.HTTP_200_OK)
def delete_registry_asset(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
    x_gw_recycler_mode: Optional[str] = Header(None, alias="X-GW-Recycle-Mode"),
):
    context = _read_auth(authorization, x_user_role)
    raw_asset = repo.get_asset(asset_id)
    if str(getattr(context, "role", "") or "").lower() not in {"editor", "admin", "governor"}:
        raise CleanroomException(404, "ASSET_NOT_FOUND", f"素材 {asset_id} 不存在")
    project_ids = list(raw_asset.get("project_ids") or [])
    if raw_asset.get("project_id") and raw_asset["project_id"] not in project_ids:
        project_ids.append(raw_asset["project_id"])
    for pid in project_ids:
        try:
            require_project_write_context(pid, context)
        except CleanroomException as exc:
            if exc.status_code == 404:
                raise CleanroomException(404, "ASSET_NOT_FOUND", f"素材 {asset_id} 不存在")
            raise
    source_id = str(raw_asset.get("storage_root_id") or raw_asset.get("root_id") or "")
    source_policy = default_storage_settings_service.get_snapshot().local_library_read_only
    if source_id and source_policy.get(source_id, False):
        raise CleanroomException(403, "SOURCE_READ_ONLY", "素材来源为只读，不能移入回收站")
    request_payload = _payload(payload)
    recycler_mode = str(request_payload.get("recycler_mode") or x_gw_recycler_mode or "managed")
    if recycler_mode not in {"source", "managed"}:
        raise CleanroomException(400, "INVALID_REQUEST", "回收目录模式不合法")
    result = repo.delete_asset(asset_id, _version(request_payload, required=True), context)
    from gw.asset_registry import lifecycle
    try:
        operation = lifecycle.recycle_asset(
            asset_id, result, context, recycler_mode=recycler_mode
        )
        if operation is not None:
            result = {**result, "operation": operation}
    except Exception:
        from gw.asset_library.service import default_asset_library_service
        stashed = dict((repo.state().read().get("recycle_bin", {}).get(result.get("recycle_entry_id"), {}) or {}).get("payload") or {}).get("library_item")
        if stashed is not None:
            default_asset_library_service.insert_item_snapshot(stashed)
        def undo(raw_state):
            entry = raw_state["recycle_bin"].pop(result.get("recycle_entry_id"), None)
            if entry is not None:
                raw_state["assets"][asset_id] = entry.get("payload") or {}
                repo._bump(raw_state)
        repo.state().mutate(undo)
        raise
    return _asset_closure_projection(
        result,
        asset_id=asset_id,
        version_id=raw_asset.get("version_id"),
        operation="recycle",
        closure_status="recovery_required",
        next_action="restore_reference",
    )


@router.post("/assets/{asset_id}/file-operations/rename", summary="物理重命名单文件",
             status_code=status.HTTP_200_OK)
def rename_asset_file(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _edit_auth(authorization, x_user_role)
    from gw.asset_registry import lifecycle
    return lifecycle.rename_file(asset_id, _payload(payload), context)


@router.get("/file-operations/{op_id}", summary="读取单文件操作日志", status_code=status.HTTP_200_OK)
def get_asset_file_operation(
    op_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _read_auth(authorization, x_user_role)
    from gw.asset_registry import lifecycle
    return {"operation": lifecycle.get_operation(op_id), "data_status": "ok"}


@router.post("/assets/{asset_id}/image-versions", summary="创建图片版本", status_code=status.HTTP_200_OK)
def create_image_version(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    return repo.create_image_version(
        asset_id, str(body.get("source_path") or ""), _version(body),
        edit=body.get("edit") if isinstance(body.get("edit"), dict) else None,
        canvas_id=str(body.get("canvas_id") or "") or None,
        root_id=str(body.get("root_id") or "") or None,
    )


@router.post("/assets/{asset_id}/derived-assets", summary="登记本地派生物", status_code=status.HTTP_200_OK)
def register_derived_asset(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    return repo.register_derived_asset(
        asset_id,
        str(body.get("name") or ""),
        str(body.get("path") or ""),
    )


@router.patch("/assets/{asset_id}/image-versions/{version_id}", summary="更新图片版本", status_code=status.HTTP_200_OK)
def update_image_version(
    asset_id: str,
    version_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    hidden = body.get("hidden")
    return repo.update_image_version(asset_id, version_id, body.get("label"),
                                     _version(body), None if hidden is None else bool(hidden))


@router.delete("/assets/{asset_id}/image-versions/{version_id}", summary="删除图片版本", status_code=status.HTTP_200_OK)
def delete_image_version(
    asset_id: str,
    version_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    return repo.delete_image_version(asset_id, version_id)


@router.post("/assets/{asset_id}/open-local", summary="本机打开前路径准入校验", status_code=status.HTTP_200_OK)
def open_asset_local(
    asset_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """只做路径准入校验，不启动任何外部程序（不调用 explorer/start/subprocess）。"""
    _edit_auth(authorization, x_user_role)
    return repo.open_local(asset_id)


@router.delete("/assets/{asset_id}/relations/{related_asset_id}", summary="删除素材关系", status_code=status.HTTP_200_OK)
def delete_asset_relation(
    asset_id: str,
    related_asset_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    return repo.delete_relation(asset_id, related_asset_id)


@router.delete("/assets/{asset_id}/tags/{tag_id}", summary="删除素材标签", status_code=status.HTTP_200_OK)
def delete_asset_tag(
    asset_id: str,
    tag_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    result = repo.delete_tag(asset_id, tag_id)
    asset = repo.get_asset(asset_id)
    return {**result, "asset": asset, "assets": [asset]}


@router.post("/assets/{asset_id}/video/clip", summary="真实剪辑视频片段", status_code=status.HTTP_200_OK)
def create_video_clip(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    start = body.get("start_seconds", body.get("start", 0)) or 0
    end = body.get("end_seconds", body.get("end"))
    if end is None:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 end_seconds")
    return media_probe.clip(asset_id, float(start), float(end), _version(body))


@router.post("/folders", summary="创建素材文件夹", status_code=status.HTTP_200_OK)
def create_registry_folder(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.create_folder(str(body.get("name") or ""), body.get("parent"), _version(body))
    return {**result, "folders": repo.folders(body.get("root_id"))["folders"]}

# ---------------------------------------------------------------------------
# 治理 / 索引
# ---------------------------------------------------------------------------


@router.post("/governance/asset-trash/{entry_id}/restore", summary="恢复回收站条目", status_code=status.HTTP_200_OK)
def restore_asset_trash(
    entry_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _governance_auth(authorization, x_user_role)
    asset_id, version_id = _recycle_asset_subject(entry_id)
    result = repo.restore_asset_trash(entry_id, context, _version(_payload(payload), required=True))
    return _asset_closure_projection(
        result,
        asset_id=asset_id or result.get("asset_id"),
        version_id=version_id,
        operation="restore",
        closure_status="closed",
        next_action=None,
    )


@router.post("/governance/assets/{asset_id}/restore", summary="恢复已归档素材", status_code=status.HTTP_200_OK)
def restore_asset(
    asset_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _governance_auth(authorization, x_user_role)
    result = repo.restore_asset(asset_id, context)
    return _asset_closure_projection(
        result,
        asset_id=asset_id,
        operation="restore",
        closure_status="closed",
        next_action=None,
    )


@router.post("/governance/audit-outbox/reconcile", summary="重放审计 outbox", status_code=status.HTTP_200_OK)
def reconcile_audit_outbox(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _governance_auth(authorization, x_user_role)
    return repo.reconcile_audit_outbox()


@router.post("/governance/canvases/purge-expired", summary="清理过期画布（记录式）", status_code=status.HTTP_200_OK)
def purge_expired_canvases(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """画布域不在本块范围：只记录真实操作条目，不伪造画布状态或删除画布。"""
    _governance_auth(authorization, x_user_role)
    body = _payload(payload)
    days = int(body.get("retention_days") or 30)
    result = repo.purge_expired_canvases(days)
    return {**result, "data_gaps": ["canvas_store_not_connected"]}


@router.post("/governance/canvases/{canvas_id}/restore", summary="恢复画布（记录式）", status_code=status.HTTP_200_OK)
def restore_canvas(
    canvas_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """画布域不在本块范围：只记录真实操作条目，不伪造画布状态。"""
    _governance_auth(authorization, x_user_role)
    result = repo.restore_canvas(canvas_id)
    return {
        **result,
        "closure_status": "blocked",
        "blocker": {
            "code": "PERSISTENCE_FAILED",
            "scope": "canvas",
            "subject": {"canvas_id": canvas_id},
            "message": "当前路由只记录恢复操作，画布真源未接入，无法确认恢复事实。",
            "retryable": False,
            "observed_at": _closure_now(),
        },
        "recovery": {
            "operation": "restore",
            "recycle_entry_id": None,
            "state": "recovery_required",
            "preserved_subjects": [{"canvas_id": canvas_id}],
            "failed_scopes": ["canvas"],
            "idempotency_key": f"restore:canvas:{canvas_id}",
            "occurred_at": _closure_now(),
        },
        "next_action": "inspect_persisted_state",
        "data_gaps": ["canvas_store_not_connected"],
    }


@router.post("/governance/operations", summary="登记治理操作", status_code=status.HTTP_200_OK)
def execute_governance_operation(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _governance_auth(authorization, x_user_role)
    body = _payload(payload)
    name = str(body.get("operation") or body.get("name") or "").strip()
    if not name:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 operation")
    return repo.record_operation(name, body)


@router.post("/index/sync", summary="同步素材索引统计", status_code=status.HTTP_200_OK)
def sync_asset_index(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    actor = _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    if body.get("background") is True:
        return JSONResponse(index_jobs.service().submit("sync", body, actor, idempotency_key), status_code=202)
    return repo.index_sync()


@router.post("/reindex", summary="重建素材索引", status_code=status.HTTP_200_OK)
def reindex_registry(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    actor = _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    if body.get("register_root") is not None and body.get("background") is not True:
        raise CleanroomException(400, "INVALID_REQUEST", "登记素材库必须通过后台索引请求提交")
    background = body.get("background", True)
    if type(background) is not bool:
        raise CleanroomException(400, "INVALID_REQUEST", "background 必须为布尔值")
    if not background:
        raise CleanroomException(400, "INVALID_REQUEST", "重建素材索引必须通过后台任务提交")
    if background:
        if "background" not in body and not idempotency_key:
            idempotency_key = uuid.uuid4().hex
        register_root = body.get("register_root")
        if register_root is not None:
            if not isinstance(register_root, dict):
                raise CleanroomException(400, "INVALID_REQUEST", "register_root 必须为对象")
            source_id = str(register_root.get("id") or "").strip()
            source_name = str(register_root.get("name") or "").strip()
            raw_path = register_root.get("path")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source_id):
                raise CleanroomException(400, "INVALID_REQUEST", "素材来源标识无效")
            if not source_name or len(source_name) > 32:
                raise CleanroomException(400, "INVALID_REQUEST", "素材来源名称必须为 1 到 32 个字符")
            expected_version = _version(register_root, required=True)
            root, prefix = storage.resolve_asset_registration_root(raw_path)
            registered_path = str(root.joinpath(*prefix.split("/"))) if prefix else str(root)
            snapshot = default_storage_settings_service.get_snapshot()
            existing = next((item for item in snapshot.local_libraries if str(item.get("id")) == source_id), None)
            if existing:
                if storage.directory_path_key(str(existing.get("path") or "")) != storage.directory_path_key(registered_path):
                    raise CleanroomException(409, "ASSET_ROOT_ID_CONFLICT", "素材来源标识已用于其他目录")
                source_name = str(existing.get("name") or source_name)
            else:
                duplicate = next((item for item in snapshot.local_libraries
                                  if item.get("path") and
                                  storage.directory_path_key(str(item["path"])) == storage.directory_path_key(registered_path)), None)
                if duplicate:
                    raise CleanroomException(409, "ASSET_ROOT_ALREADY_REGISTERED", "该目录已登记为素材来源")
                entries = [dict(item) for item in snapshot.extra_local_entries]
                entries.append({"id": source_id, "kind": source_id, "name": source_name, "path": registered_path})
                names = dict(snapshot.local_library_names)
                names[source_id] = source_name
                order = [str(item) for item in snapshot.local_library_order if str(item) != source_id]
                order.append(source_id)
                default_storage_settings_service.patch(StorageSettingsPatchRequest(
                    dirs=dict(snapshot.dirs), extra_local_entries=entries,
                    local_library_names=names, local_library_order=order,
                    expected_version=expected_version,
                ))
            index_payload = {key: value for key, value in body.items() if key != "register_root"}
            index_payload["roots"] = [source_id]
            accepted = index_jobs.service().submit("reindex", index_payload, actor, idempotency_key)
            return JSONResponse({**accepted, "registered_root": {"id": source_id, "name": source_name}},
                                status_code=202)
        return JSONResponse(index_jobs.service().submit("reindex", body, actor, idempotency_key), status_code=202)
    return repo.reindex(_version(_payload(payload)))


def _root_alias_candidate(path: Path, source: Dict[str, Any], bound: Any) -> Optional[Dict[str, str]]:
    """只为已登记且经文件系统确认的来源生成别名候选。"""
    source_id = str(source.get("id") or "")
    if not source_id:
        return None
    known_paths = [str(source.get("path") or "")]
    if isinstance(bound, dict) and isinstance(bound.get("aliases"), list):
        known_paths.extend(str(value) for value in bound["aliases"] if value)
    same_source = False
    for raw_known in known_paths:
        if not raw_known:
            continue
        try:
            known_path = Path(storage.normalize_directory_path(raw_known)).resolve(strict=True)
            if os.path.samefile(path, known_path):
                same_source = True
                break
        except (OSError, RuntimeError, ValueError):
            continue
    if not same_source:
        return None
    root_id = str(bound.get("root_id") or "") if isinstance(bound, dict) else ""
    canonical_relpath = str(bound.get("canonical_relpath") or "") if isinstance(bound, dict) else ""
    if not root_id:
        try:
            source_root, source_prefix = storage.resolve_asset_registration_root(
                str(source.get("path") or "")
            )
            root_id = storage.root_identity_id(source_root)
            canonical_relpath = storage.canonical_relpath(source_prefix) if source_prefix else ""
        except CleanroomException:
            return None
    return {
        "storage_root_id": source_id,
        "name": str(source.get("name") or source_id),
        "root_id": root_id,
        "canonical_relpath": canonical_relpath,
    }


def _root_alias_path(raw_path: Any) -> tuple[str, Path, str, str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise CleanroomException(400, "INVALID_PATH", "路径参数不合法")
    normalized = storage.normalize_directory_path(raw_path)
    try:
        path = Path(normalized).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise CleanroomException(400, "INVALID_PATH", "目录不存在或不可访问") from None
    if not path.is_dir():
        raise CleanroomException(400, "INVALID_PATH", "目录不存在或不可访问")
    try:
        root, prefix = storage.resolve_asset_registration_root(normalized)
    except CleanroomException as exc:
        if exc.code != "PATH_OUTSIDE_ALLOWED_ROOTS":
            raise
        # 外部 alias 仅在后续与已准入来源通过 samefile 验证后才可建议/绑定；
        # 不把候选路径本身加入允许根或用于文件读取。
        return normalized, path, "", ""
    canonical = storage.canonical_relpath(prefix) if prefix else ""
    return normalized, path, storage.root_identity_id(root), canonical


@router.post("/root-aliases/suggestions", summary="建议已登记素材来源的路径别名")
def suggest_root_aliases(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    normalized, path, candidate_root_id, _canonical = _root_alias_path(
        _payload(payload).get("path")
    )
    registry_state = repo.state().read()
    state_aliases = registry_state.get("root_aliases", {})
    candidates = []
    for source in default_storage_settings_service.get_snapshot().local_libraries:
        source_id = str(source.get("id") or "")
        candidate = _root_alias_candidate(
            path, source, state_aliases.get(source_id, {})
        )
        if candidate and candidate["storage_root_id"] not in {
            item["storage_root_id"] for item in candidates
        }:
            candidates.append(candidate)
    if not candidates and not candidate_root_id:
        raise CleanroomException(
            403, "PATH_OUTSIDE_ALLOWED_ROOTS", "目标路径不在允许根目录内"
        )
    return {
        "path": normalized,
        "candidates": candidates,
        "revision": registry_state["revision"],
    }


@router.post("/root-aliases/confirm", summary="确认素材来源路径别名")
def confirm_root_alias(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    expected_version = _version(body, required=True)
    normalized, path, candidate_root_id, _ = _root_alias_path(
        body.get("path")
    )
    source_id = str(body.get("storage_root_id") or "")
    source = next(
        (
            item
            for item in default_storage_settings_service.get_snapshot().local_libraries
            if str(item.get("id") or "") == source_id
        ),
        None,
    )
    if source is None:
        raise CleanroomException(404, "ROOT_NOT_FOUND", "素材来源根不存在")
    state_aliases = repo.state().read().get("root_aliases", {})
    candidate = _root_alias_candidate(path, source, state_aliases.get(source_id, {}))
    if candidate is None:
        if not candidate_root_id:
            raise CleanroomException(
                403, "PATH_OUTSIDE_ALLOWED_ROOTS", "目标路径不在允许根目录内"
            )
        raise CleanroomException(409, "ROOT_ALIAS_MISMATCH", "该路径未指向所选的已登记来源")
    bound_root_id = candidate["root_id"] or candidate_root_id
    bound_relpath = candidate["canonical_relpath"]

    def bind(raw: Dict[str, Any]) -> Dict[str, Any]:
        repo._cas(raw, expected_version, required=True)
        aliases = raw.setdefault("root_aliases", {})
        record = aliases.get(source_id)
        if not isinstance(record, dict):
            record = {
                "root_id": bound_root_id,
                "canonical_relpath": bound_relpath,
                "aliases": [],
            }
            aliases[source_id] = record
        elif record.get("root_id") != bound_root_id:
            raise CleanroomException(409, "ROOT_ALIAS_IDENTITY_CONFLICT", "素材来源根身份已改变")
        values = record.setdefault("aliases", [])
        if normalized not in values:
            values.append(normalized)
            record["canonical_relpath"] = bound_relpath
            repo._bump(raw)
        return {
            "bound": True,
            "storage_root_id": source_id,
            "name": str(source.get("name") or source_id),
            "path": normalized,
            "root_id": bound_root_id,
            "canonical_relpath": bound_relpath,
            "revision": raw["revision"],
        }

    return repo.state().mutate(bind)


# ---------------------------------------------------------------------------
# 配置：偏好 / 能力开关 / 索引自动化
# ---------------------------------------------------------------------------


@router.patch("/preferences/team", summary="更新团队素材偏好", status_code=status.HTTP_200_OK)
def update_team_preferences(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    return repo.update_team_preferences(_payload(payload))


@router.patch("/settings/features/{feature_id}", summary="更新素材能力开关", status_code=status.HTTP_200_OK)
def update_registry_feature(
    feature_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.update_feature(feature_id, bool(body.get("enabled")))
    snapshot = repo.features_snapshot()
    feature = {"id": feature_id, "name": FEATURE_NAMES.get(feature_id, feature_id),
               "enabled": result["enabled"], "data_status": DATA_STATUS_OK}
    return {**result, "feature": feature,
            "features": [{"id": key, "name": FEATURE_NAMES.get(key, key), "enabled": bool(value)}
                         for key, value in snapshot["features"].items()]}


@router.patch("/settings/index-automation", summary="更新索引自动化配置", status_code=status.HTTP_200_OK)
def update_index_automation(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    interval = body.get("interval_minutes")
    return repo.update_index_automation(
        None if body.get("enabled") is None else bool(body.get("enabled")),
        None if interval is None else int(interval), _version(body))


# ---------------------------------------------------------------------------
# 预设 / 目录模板
# ---------------------------------------------------------------------------


@router.post("/presets", summary="创建素材预设", status_code=status.HTTP_200_OK)
def create_registry_preset(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.create_preset(str(body.get("name") or ""), body.get("definition") or body.get("payload") or {},
                                _version(body))
    preset = dict(result["preset"])
    preset["id"] = preset["preset_id"]
    return {**result, "preset": preset}


@router.delete("/presets/{preset_id}", summary="删除素材预设", status_code=status.HTTP_200_OK)
def delete_registry_preset(
    preset_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    return repo.delete_preset(preset_id)


@router.post("/project-directory-templates", summary="创建项目目录模板", status_code=status.HTTP_200_OK)
def create_directory_template(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.create_directory_template(body)
    return {**result, "template": _template_view(result["template"], body)}


@router.patch("/project-directory-templates/{template_id}", summary="更新项目目录模板", status_code=status.HTTP_200_OK)
def update_directory_template(
    template_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.update_directory_template(template_id, body)
    return {**result, "template": _template_view(result["template"], body)}


@router.post("/project-directory-templates/{template_id}/archive", summary="归档项目目录模板", status_code=status.HTTP_200_OK)
def archive_directory_template(
    template_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.archive_directory_template(template_id, body.get("archived", True), body.get("expected_version"))
    return {**result, "template": _template_view(result["template"], body)}


@router.post("/project-directory-templates/{template_id}/default", summary="设为默认目录模板", status_code=status.HTTP_200_OK)
def set_default_directory_template(
    template_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    result = repo.set_default_directory_template(template_id, body.get("expected_version"))
    return {**result, "template": _template_view(result["template"], body)}


def _template_view(record: Dict[str, Any], request_body: Dict[str, Any]) -> Dict[str, Any]:
    """目录模板对外视图：补齐前端使用的字段名，不改变落盘结构。"""
    view = dict(record)
    view["id"] = record["template_id"]
    folders = list(record.get("folders") or [])
    view["directory_tree"] = folders
    view["folder_count"] = len(folders)
    view["is_archived"] = bool(record.get("archived"))
    view["version"] = int(record.get("version", 1))
    view["slot_mapping"] = record.get("slot_mapping") or {}
    view["usage_count"] = int(record.get("usage_count") or 0)
    return view

# ---------------------------------------------------------------------------
# 项目实体 / 项目门 / 项目回收
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/entities", summary="创建项目实体", status_code=status.HTTP_200_OK)
def create_project_entity(
    project_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    require_project_write_context(project_id, context)
    body = _payload(payload)
    result = repo.create_project_entity(project_id, str(body.get("name") or ""),
                                        str(body.get("entity_type") or "generic"), body)
    return {**result, "entity": _entity_view(result["entity"])}


@router.patch("/project-entities/{entity_id}", summary="更新项目实体", status_code=status.HTTP_200_OK)
def update_project_entity(
    entity_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    if body.get("state") is not None and body.get("status") is None:
        body["status"] = body["state"]
    result = repo.update_project_entity(entity_id, body)
    return {**result, "entity": _entity_view(result["entity"])}


@router.post("/projects/{project_id}/assets", summary="关联项目素材", status_code=status.HTTP_200_OK)
def link_project_assets(
    project_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    require_project_write_context(project_id, context)
    body = _payload(payload)
    asset_ids = _ids(body) or ([str(body["asset_id"])] if body.get("asset_id") else [])
    if not asset_ids:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 asset_ids")
    result = repo.link_project_assets(project_id, asset_ids, body)
    return {**result, "assets": repo.assets_snapshot(asset_ids)}


@router.get("/projects/{project_id}/gates", summary="按权限发现项目阶段门", status_code=status.HTTP_200_OK)
def list_project_gates(
    project_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """owner与治理角色可读；其他已认证主体只得到空集合，不泄露门或owner。"""
    context = _read_auth(authorization, x_user_role)
    gates = repo.project_gates(
        project_id,
        owner_key=owner_key_for_context(context),
        role=context.role,
    )["gates"]
    return {"project_id": project_id, "gates": gates, "data_status": DATA_STATUS_OK}


@router.get("/project-gates/{gate_id}", summary="读取项目阶段门", status_code=status.HTTP_200_OK)
def get_project_gate(
    gate_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """详情对无权主体隐藏为同一404。"""
    context = _read_auth(authorization, x_user_role)
    result = repo.get_project_gate(
        gate_id,
        owner_key=owner_key_for_context(context),
        role=context.role,
    )
    return {"gate": result["gate"], "data_status": DATA_STATUS_OK}


@router.patch("/project-gates/{gate_id}", summary="更新项目门", status_code=status.HTTP_200_OK)
def update_project_gate(
    gate_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """owner或治理角色按门自身版本执行状态迁移；项目生命周期屏障在域锁内校验。"""
    context = _read_auth(authorization, x_user_role)
    project_id = default_projects_service.project_id_for_gate(gate_id)
    require_project_write_context(project_id, context)
    body = _payload(payload)
    if body.get("status") is not None and body.get("state") is None:
        body["state"] = body["status"]
    result = repo.update_project_gate(
        gate_id,
        body,
        owner_key=owner_key_for_context(context),
        role=context.role,
    )
    return {"gate": result["gate"], "data_status": DATA_STATUS_OK}

@router.post("/project-recycle/{project_id}/restore", summary="恢复回收站项目（CAS）", status_code=status.HTTP_200_OK)
def restore_project_recycle(
    project_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """治理权限下接入项目中心恢复状态机，未知项目404、过期版本409。"""
    _governance_auth(authorization, x_user_role)
    result = repo.restore_project_recycle(project_id, _payload(payload).get("expected_version"))
    return {
        **result,
        "closure_status": "closed",
        "blocker": None,
        "recovery": {
            "operation": "restore",
            "recycle_entry_id": None,
            "state": "completed",
            "preserved_subjects": [{"project_id": project_id}],
            "failed_scopes": [],
            "idempotency_key": f"restore:project:{project_id}",
            "occurred_at": _closure_now(),
        },
        "next_action": None,
    }


@router.post("/recycle-bin/{entry_id}/restore", summary="恢复回收站条目", status_code=status.HTTP_200_OK)
def restore_recycle_entry(
    entry_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _governance_auth(authorization, x_user_role)
    asset_id, version_id = _recycle_asset_subject(entry_id)
    body = _payload(payload)
    if body.get("resolution") not in (None, "reject", "suffix"):
        raise CleanroomException(400, "INVALID_REQUEST", "恢复策略不合法")
    resolution = str(body.get("resolution") or "reject")
    from gw.asset_registry import lifecycle
    result = lifecycle.restore_asset_files(
        entry_id,
        resolution,
        context,
        _version(body, required=True),
    )
    return _asset_closure_projection(
        result,
        asset_id=asset_id or result.get("asset_id"),
        version_id=version_id,
        operation="restore",
        closure_status="closed",
        next_action=None,
    )


def _entity_view(record: Dict[str, Any]) -> Dict[str, Any]:
    view = dict(record)
    view["id"] = record["entity_id"]
    view["status"] = record.get("status") or record.get("state") or "pending"
    view["code"] = record.get("code") or record["entity_id"]
    return view


# ---------------------------------------------------------------------------
# 远程素材 / 工作区任务
# ---------------------------------------------------------------------------


@router.post("/remote-assets", summary="登记远程素材", status_code=status.HTTP_200_OK)
def create_remote_asset(
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    body = _payload(payload)
    url = str(body.get("url") or "").strip()
    if not url:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 url")
    result = repo.create_remote_asset(url, str(body.get("name") or ""), _version(body))
    record = dict(result["remote_asset"])
    record["id"] = record["asset_id"]
    record["kind"] = str(body.get("kind") or "remote")
    return {**result, "remote_asset": record, "assets": [record]}


@router.delete("/remote-assets/{asset_id}", summary="删除远程素材", status_code=status.HTTP_200_OK)
def delete_remote_asset(
    asset_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _edit_auth(authorization, x_user_role)
    return repo.delete_remote_asset(asset_id)


@router.post("/workspace-jobs/{job_id}/{action}", summary="操作工作区任务", status_code=status.HTTP_200_OK)
def operate_workspace_job(
    job_id: str,
    action: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    if action not in index_jobs.ACTIONS:
        raise CleanroomException(400, "INVALID_ACTION", "不支持的任务操作")
    actor = _read_auth(authorization, x_user_role)
    job = index_jobs.service().store.read()["jobs"].get(job_id)
    if not job:
        raise CleanroomException(404, "WORKSPACE_JOB_NOT_FOUND", "工作区任务不存在")
    if job.get("project_id"):
        require_project_write_context(job["project_id"], actor)
    else:
        _edit_auth(authorization, x_user_role)
    return index_jobs.service().operate(job_id, action, _payload(payload), actor)


# ---------------------------------------------------------------------------
# C4-AC5 未归属收件箱
# ---------------------------------------------------------------------------


unassigned_router = APIRouter(prefix="/api/assets", tags=["asset-registry"],
                              dependencies=[Depends(asset_request_scope)])

_jxl_transcode_service_lock = threading.Lock()
_jxl_transcode_service_root: str | None = None
_jxl_transcode_service: jxl_tool.JxlLosslessTranscodeService | None = None


def _jxl_transcode_jobs() -> jxl_tool.JxlLosslessTranscodeService:
    """为当前数据根目录惰性创建单实例转码任务服务。"""
    global _jxl_transcode_service_root, _jxl_transcode_service
    root = str(storage.data_root().resolve())
    with _jxl_transcode_service_lock:
        if _jxl_transcode_service is None or _jxl_transcode_service_root != root:
            _jxl_transcode_service = jxl_tool.JxlLosslessTranscodeService(
                repo.media_file, repo.record_jxl_lossless_transcodes
            )
            _jxl_transcode_service_root = root
        return _jxl_transcode_service


def _scan_jxl_transcode_assets() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """只扫描可见、登记且可读取的本机 JPEG 素材，不返回物理路径。"""
    raw = repo.state().read()
    registered_count = 0
    available_count = 0
    pending: list[dict[str, Any]] = []
    pending_bytes = 0
    for asset_id, record in raw.get("assets", {}).items():
        if not isinstance(record, dict) or record.get("archived") or not repo._visible(record):
            continue
        display_path = str(record.get("display_path") or "")
        if not display_path:
            versions = raw.get("image_versions", {}).get(asset_id, {})
            ordered_versions = sorted(
                (value for value in versions.values() if isinstance(value, dict)),
                key=lambda value: str(value.get("version_id") or ""),
                reverse=True,
            )
            display_path = next(
                (str(value.get("display_path") or "") for value in ordered_versions if value.get("display_path")),
                "",
            )
        if Path(display_path).suffix.lower() not in {".jpg", ".jpeg"}:
            continue
        registered_count += 1
        try:
            source = repo.media_file(str(asset_id))
            if source is None or not source.is_file():
                continue
            source_stat = source.stat()
        except (CleanroomException, OSError):
            continue
        available_count += 1
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        previous = metadata.get("jxl_lossless_transcode")
        completed = False
        if isinstance(previous, dict) and previous.get("verified") is True:
            output_relative = str(previous.get("output") or "")
            if output_relative.startswith("media_output/jxl-lossless/") and output_relative.lower().endswith(".jxl"):
                try:
                    output = storage.resolve_output_path(output_relative, must_exist=True)
                    output_stat = output.stat()
                    completed = (
                        int(previous.get("source_size_bytes", -1)) == source_stat.st_size
                        and int(previous.get("source_mtime_ns", -1)) == source_stat.st_mtime_ns
                        and int(previous.get("output_size_bytes", -1)) == output_stat.st_size
                        and output_stat.st_size > 0
                    )
                except (CleanroomException, OSError, TypeError, ValueError):
                    completed = False
        if completed:
            continue
        size_bytes = int(source_stat.st_size)
        pending_bytes += size_bytes
        pending.append(
            {
                "asset_id": str(asset_id),
                "expected_version": int(record.get("version", 1)),
                "source_size_bytes": size_bytes,
                "source_mtime_ns": int(source_stat.st_mtime_ns),
            }
        )
    return (
        {
            "jpeg_assets": registered_count,
            "available_count": available_count,
            "unavailable_count": registered_count - available_count,
            "pending_count": len(pending),
            "pending_bytes": pending_bytes,
            "estimated_savings_min_bytes": pending_bytes // 5,
            "estimated_savings_max_bytes": pending_bytes // 4,
            "originals_retained": True,
            "data_status": "ok",
            "data_gaps": [],
        },
        pending,
    )


@unassigned_router.get(
    "/transcode-jpeg-lossless/scan",
    summary="扫描待无损转码 JPEG 素材",
    status_code=status.HTTP_200_OK,
)
def scan_jxl_lossless_transcodes(
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    scan, _ = _scan_jxl_transcode_assets()
    jobs = _jxl_transcode_jobs()
    return {
        **scan,
        "tool_status": jxl_tool.detect_tools(),
        "active_job": jobs.active_for_owner(asset_owner_key(context)),
    }


@unassigned_router.post(
    "/transcode-jpeg-lossless",
    summary="提交 JPEG XL 无损转码任务",
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_jxl_lossless_transcodes(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _edit_auth(authorization, x_user_role)
    jxl_tool._require_tools()
    body = await _json_body(request)
    _, candidates = _scan_jxl_transcode_assets()
    requested_ids = set(_ids(body))
    if requested_ids:
        candidate_ids = {item["asset_id"] for item in candidates}
        if requested_ids - candidate_ids:
            raise CleanroomException(409, "NO_PENDING_JPEG", "所选素材不存在或已无需转码")
        candidates = [item for item in candidates if item["asset_id"] in requested_ids]
    if not candidates:
        raise CleanroomException(409, "NO_PENDING_JPEG", "没有待转码的 JPEG 素材")
    return _jxl_transcode_jobs().submit(candidates, asset_owner_key(context))


@unassigned_router.get(
    "/transcode-jpeg-lossless/jobs/{job_id}",
    summary="读取 JPEG XL 无损转码任务",
    status_code=status.HTTP_200_OK,
)
def get_jxl_lossless_transcode_job(
    job_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _read_auth(authorization, x_user_role)
    return _jxl_transcode_jobs().get(job_id, asset_owner_key(context))


@unassigned_router.post(
    "/transcode-jpeg-lossless/jobs/{job_id}/cancel",
    summary="安全取消 JPEG XL 无损转码任务",
    status_code=status.HTTP_200_OK,
)
def cancel_jxl_lossless_transcode_job(
    job_id: str,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = _edit_auth(authorization, x_user_role)
    return _jxl_transcode_jobs().cancel(job_id, asset_owner_key(context))


@unassigned_router.get("/unassigned", summary="未归属收件箱", status_code=status.HTTP_200_OK)
def list_unassigned_inbox(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """返回当前认证账户自己拥有、且没有 project_id 的资产；匿名一律 401。

    无 workflow_owner 的资产只进入本地实例管理员（role == 'admin'）的收件箱，
    标记 source_unknown=true / origin_label='来源未知'；有主资产
    source_unknown=false / origin_label=null，且只在所有者收件箱出现。
    本接口只读取，绝不自动归属或猜测项目。
    """
    context = _read_auth(authorization, x_user_role)
    return repo.list_unassigned_assets(asset_owner_key(context), limit, offset, role=context.role)


@unassigned_router.post("/{asset_id}/move-to-project", summary="未归属资产移入项目",
                        status_code=status.HTTP_200_OK)
async def move_unassigned_asset_to_project(
    asset_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """只有资产所有者且对目标项目有写权限才能移动；按资产 CAS，冲突 409；已归属资产 409 拒绝。

    CAS 基准是资产自身版本（收件箱每项的 asset_version），而非库级 revision：
    请求体必须提供正整数 expected_asset_version；仅当其缺省时兼容旧字段
    expected_version（同为资产级版本语义），两者同传以 expected_asset_version 为准。
    无主资产仅本地实例管理员可移动：非管理员与不存在同形 404；管理员移动时在
    同一次 CAS 写入内回填 metadata.workflow_owner，冲突 409 即不回填。
    """
    context = _edit_auth(authorization, x_user_role)
    body = await _json_body(request)
    project_id = str(body.get("project_id") or "").strip()
    expected_version = body.get("expected_asset_version")
    if expected_version is None:
        # 旧字段兼容：仅当新字段缺省时按同一资产级语义读取 expected_version。
        expected_version = body.get("expected_version")
    if not project_id or type(expected_version) is not int or expected_version < 1:
        raise CleanroomException(400, "INVALID_REQUEST",
                                 "必须提供 project_id 与正整数 expected_asset_version")
    # 目标项目写权限：项目owner或治理角色；项目不存在/无权同形 404，归档或回收站 403 只读。
    default_projects_service.require_project_write_access(
        project_id, owner_key=owner_key_for_context(context), role=context.role)
    return repo.move_asset_to_project(asset_id, project_id, expected_version,
                                      asset_owner_key(context), role=context.role)


__all__ = ["router", "unassigned_router"]
