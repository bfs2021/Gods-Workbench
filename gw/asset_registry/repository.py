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

"""Phase 12 素材注册表的真实落盘数据源。

真实数据源：
- 注册表主体（资产/关系/标签/图片版本/预设/目录模板/项目实体/项目门/回收站/远程素材）
  落到 `core.storage.JsonState` 单实例 JSON，重启可恢复；
- 索引类操作真实遍历 `GW_ALLOWED_ROOTS`（未配置即失败关闭）；
- 远程素材用 `httpx` 做真实 HTTP 探测；网络失败如实降级，不编造元数据。

零伪造：稳定 ID 由 `storage.next_sequence()` 确定性生成，禁止随机/uuid；
写操作要求 `expected_version`（CAS），冲突 409。
证据边界：单实例 JSON；多 worker 并发写属部署方职责，未闭环。
"""

from __future__ import annotations

import io
import base64
import hashlib
import json
import logging
import math
import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import ValidationError

from gw.core import storage
from gw.cinematography.schema import MasterEntityType
from gw.asset_registry import audit_sink
from gw.asset_registry import media_probe
from gw.asset_registry.models import CinematographyAssetFilters
from gw.asset_registry.service import normalize_asset_query, parse_utc_datetime
from gw.core.errors import CleanroomException

NS_REGISTRY = "asset_registry"
ASSET_SEARCH_INDEX_FILENAME = "asset_registry_search.sqlite3"
DATA_STATUS_OK = "ok"
_ENTITY_FILTER_INDEX_CACHE_KEY: Optional[Tuple[str, str]] = None
_ENTITY_FILTER_INDEX_CACHE_VALUE: Tuple[
    Dict[str, Tuple[str, str]], Dict[str, set[str]], Dict[Tuple[str, str], set[str]]
] = ({}, {}, {})
_ENTITY_FILTER_INDEX_CACHE_LOCK = threading.RLock()
_LOGGER = logging.getLogger(__name__)


def _blank() -> Dict[str, Any]:
    return {
        "revision": 1,
        "assets": {},
        "relations": [],
        "tags": {},
        "image_versions": {},
        "presets": {},
        "directory_templates": {},
        "project_entities": {},
        "project_gates": {},
        "recycle_bin": {},
        "remote_assets": {},
        "outbox": {},
        "operations": [],
        "folders": {},
        "workspace_jobs": {},
        "team_preferences": {"team_id": None, "default_library_id": None, "default_folder_id": None,
                             "view": "grid", "sort": "newest", "version": 1},
        "features": {"thumbnail": True, "video_storyboard": False, "metadata": True,
                     "fulltext": True, "font": True, "model": True,
                     "index_automation": False},
        "index_automation": {"enabled": False, "interval_minutes": 60, "version": 1},
        "sequences": {},
    }


def state() -> storage.JsonState:
    return storage.JsonState(NS_REGISTRY, _blank)


def _seq(raw: Dict[str, Any], prefix: str, taken: Any = None) -> str:
    """生成确定性序号 ID；同时参考 sequences 与目标集合已有键，避免跨集合冲突。

    注意：必须传入目标集合的现有键（如 raw["tags"].keys()），否则不同集合会
    各自从 0001 开始并互相覆盖。
    """
    keys = list(raw.setdefault("sequences", {}).keys())
    if taken is not None:
        keys.extend(list(taken))
    return storage.next_sequence(prefix, keys)


def _asset_sequence_start(raw: Dict[str, Any]) -> int:
    """一次读取现有资产 ID 高水位，避免扫描时每新增一条都重扫完整集合。"""
    next_id = storage.next_sequence(
        "ast_", [*raw.get("sequences", {}).keys(), *raw.get("assets", {}).keys()]
    )
    return int(next_id[len("ast_"):])


def _bump(raw: Dict[str, Any]) -> int:
    raw["revision"] = int(raw.get("revision", 1)) + 1
    return raw["revision"]


def _cas(raw: Dict[str, Any], expected: Optional[int], *, required: bool = False) -> None:
    """CAS：需要版本的写操作拒绝缺失值，过期版本返回 409。"""
    current = int(raw.get("revision", 1))
    if expected is None and not required:
        return
    if type(expected) is not int or expected < 1:
        raise CleanroomException(400, "INVALID_REQUEST", "expected_version 必须为正整数")
    if expected != current:
        raise CleanroomException(
            409, "VERSION_CONFLICT", "注册表版本冲突，请重新读取后重试",
            extra={"expected_version": expected, "current_version": current},
            expose_extra_fields={"expected_version", "current_version"},
        )


def _visible(record: Dict[str, Any]) -> bool:
    """asset_visible 的旧数据容错：metadata 为 None 或非 dict 时按空 metadata 判定。"""
    from gw.core.asset_visibility import asset_visible
    if not isinstance(record.get("metadata"), dict):
        record = {**record, "metadata": {}}
    return asset_visible(record)


def _removed_root_ids() -> set[str]:
    removed: set[str] = set()
    try:
        from gw.asset_registry import index_jobs
        for facet in index_jobs.root_facets():
            if str(facet.get("availability") or "") == "removed":
                if facet.get("id"):
                    removed.add(str(facet["id"]))
                if facet.get("root_id"):
                    removed.add(str(facet["root_id"]))
                if facet.get("storage_root_id"):
                    removed.add(str(facet["storage_root_id"]))
    except Exception:
        pass
    try:
        from gw.settings.service import default_storage_allowed_roots_service
        snapshot = default_storage_allowed_roots_service.get_snapshot()
        for root in snapshot.roots:
            if not root.active:
                removed.add(str(root.id))
    except Exception:
        pass
    return removed


def _is_removed_asset(asset: Dict[str, Any], removed_root_ids: set[str]) -> bool:
    if asset.get("root_availability") == "removed":
        return True
    root_id = str(asset.get("root_id") or "")
    if root_id and root_id in removed_root_ids:
        return True
    storage_root_id = str(asset.get("storage_root_id") or "")
    if storage_root_id and storage_root_id in removed_root_ids:
        return True
    return False


def _iso_from_epoch(seconds: Any) -> str:
    """把文件系统时间戳转成与 ``storage.now_iso()`` 同口径的 ISO 8601 UTC 字符串。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(value) or value <= 0:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_display_names() -> Dict[str, str]:
    """来源 ID 到显示名映射：与左栏资产库同源（``root_facets``），不另存副本。

    ``root_facets`` 本身对每个来源做有界挂载探测，异常时按空表处理——宁可显示名
    缺失，也不能让一次探测失败把整个列表请求打成 500。
    """
    from gw.asset_registry import index_jobs

    names: Dict[str, str] = {}
    try:
        facets = index_jobs.root_facets()
    except Exception:
        return names
    for facet in facets:
        source_id = str(facet.get("id") or "")
        name = str(facet.get("name") or "").strip()
        if source_id and name and source_id not in names:
            names[source_id] = name
    return names


def _asset_source_names(asset: Dict[str, Any], names: Dict[str, str]) -> List[str]:
    """返回素材登记所在来源库的显示名；来源登记不可解析时退回物理根名。"""
    for key in ("storage_root_id", "root_id"):
        name = names.get(str(asset.get(key) or ""), "")
        if name:
            return [name]
    return []


def _asset_modified_at(asset: Dict[str, Any], raw: Dict[str, Any]) -> str:
    """按素材登记位置读取真实文件修改时间；读不到就返回空串，不伪造时间。"""
    asset_id = str(asset.get("asset_id") or asset.get("id") or "")
    if not asset_id:
        return ""
    try:
        path = _media_file_in_state(raw, asset_id)
    except CleanroomException:
        return ""
    if path is None:
        return ""
    try:
        return _iso_from_epoch(path.stat().st_mtime)
    except OSError:
        return ""


def annotate_asset_provenance(
    items: Iterable[Dict[str, Any]],
    *,
    raw: Optional[Dict[str, Any]] = None,
    resolve_modified: bool = True,
) -> List[Dict[str, Any]]:
    """补下发真实来源库名与文件修改时间（QIN-539 / GitHub #592）。

    - ``source_names``：素材登记所在来源库的显示名（与左栏一致）；前端在多来源
      条目上沿用既有「N 库」渲染。
    - ``storage_root_name``：``source_names`` 首位，供只取单值的旧渲染复用。
    - ``modified_at``：文件修改时间（ISO 8601 UTC）。索引期已落盘则直接取用；
      历史条目仅在缺失时兜底 stat 一次，取不到就留空，不编造时间。

    只装饰调用方给到的条目（分页后的当前页），不对全库逐条 stat。
    """
    snapshot = raw if raw is not None else state().read()
    names = _source_display_names()
    decorated: List[Dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        source_names = _asset_source_names(entry, names)
        if source_names:
            entry["source_names"] = source_names
            entry["storage_root_name"] = source_names[0]
        else:
            entry.pop("source_names", None)
        if not str(entry.get("modified_at") or "") and resolve_modified:
            modified_at = _asset_modified_at(entry, snapshot)
            if modified_at:
                entry["modified_at"] = modified_at
        decorated.append(entry)
    return decorated


def _require(raw: Dict[str, Any], asset_id: str) -> Dict[str, Any]:
    item = raw["assets"].get(asset_id)
    if item is None or not _visible(item):
        raise CleanroomException(404, "ASSET_NOT_FOUND", "素材 %s 不存在" % asset_id)
    return item


def _plain_asset(asset_id: str, name: str, kind: str, **extra: Any) -> Dict[str, Any]:
    payload = {
        "id": asset_id, "asset_id": asset_id, "name": name, "kind": kind, "archived": False,
        "hidden": False, "version": 1, "created_at": storage.now_iso(), "updated_at": storage.now_iso(),
        "tags": [], "relations": [], "display_path": None, "metadata": {},
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


# 关联素材投影字段：只暴露前端渲染关联卡片所需的最小子集。
# 不外泄磁盘绝对路径、存储根真实位置与内部元数据；缩略图 URL 由前端按 asset_id 拼接。
_RELATED_ASSET_FIELDS = (
    "id", "asset_id", "name", "kind", "status", "size", "size_bytes",
    "mime_type", "storage_root_id", "public_url", "is_remote", "created_at",
)


def _relation_target_index(raw: Dict[str, Any]) -> Dict[str, List[str]]:
    """按来源素材聚合出向关系的目标 id，保持登记顺序且去重。"""
    index: Dict[str, List[str]] = {}
    for relation in raw.get("relations", []):
        from_id = str(relation.get("from_asset_id") or "")
        to_id = str(relation.get("to_asset_id") or "")
        if not from_id or not to_id or from_id == to_id:
            continue
        targets = index.setdefault(from_id, [])
        if to_id not in targets:
            targets.append(to_id)
    return index


def _related_assets_for(
    raw: Dict[str, Any],
    asset: Dict[str, Any],
    index: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """回填该素材已关联的素材摘要。

    关系是单向登记（来源 → 目标），这里只给出来源素材可见的关联对象，
    不反向补全，避免凭空造出对称关系。目标素材已被移除时跳过，不编造占位记录。
    """
    asset_id = str(asset.get("asset_id") or asset.get("id") or "")
    if not asset_id:
        return []
    targets = (index if index is not None else _relation_target_index(raw)).get(asset_id, [])
    if not targets:
        return []
    assets = raw.get("assets", {})
    related: List[Dict[str, Any]] = []
    for target_id in targets:
        target = assets.get(target_id)
        if target is None:
            continue
        summary = {
            key: target.get(key)
            for key in _RELATED_ASSET_FIELDS
            if target.get(key) is not None
        }
        summary.setdefault("id", target_id)
        summary.setdefault("asset_id", target_id)
        related.append(summary)
    return related


def _asset_shot_text(asset: Dict[str, Any]) -> List[str]:
    """收集资产登记镜头的标签与摘要，供检索文档收录镜头文本（MVP1-T7/F2）。

    镜头索引行只存起止时间戳与内容哈希，不含可检索文本；若资产文档也不收
    镜头文本，仅出现在镜头标签或摘要里的检索词会整条漏召回，镜头级检索因此
    永远 0 命中。这里只读取登记内容，不派生新字段。
    """
    metadata = asset.get("metadata")
    segments = metadata.get("video_segments") if isinstance(metadata, dict) else None
    if not isinstance(segments, list):
        return []
    values: List[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        tags = segment.get("tags")
        if isinstance(tags, list):
            values.extend(
                tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()
            )
        summary = segment.get("summary")
        if isinstance(summary, str) and summary.strip():
            values.append(summary.strip())
    return values


def _asset_search_document(asset: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从资产记录提取可检索文档；缺 id 或名称的记录不进入索引。"""
    if not isinstance(asset, dict):
        return None
    asset_id = str(asset.get("asset_id") or asset.get("id") or "").strip()
    title = str(asset.get("name") or "").strip()
    if not asset_id or not title:
        return None
    tags = asset.get("tags")
    tag_text = [
        tag.strip() for tag in tags
        if isinstance(tag, str) and tag.strip()
    ] if isinstance(tags, list) else []
    content = " ".join(
        value for value in (
            str(asset.get("kind") or "").strip(),
            str(asset.get("display_path") or "").strip(),
            *tag_text,
            *_asset_shot_text(asset),
        ) if value
    )
    return {"asset_id": asset_id, "title": title, "content": content}


def shot_source_key(asset: Dict[str, Any]) -> str:
    """素材侧内容指纹：镜头 hash 的「来源素材」维度（QIN-468）。

    优先使用扫描期写入的真实文件 `sha256`，让同一素材的字节副本（不同
    `asset_id`、不同路径）仍能命中同一镜头哈希，保住跨资产查重能力；
    素材尚无真实指纹时退回稳定 `asset_id`，宁可漏报跨资产重复，也不
    再把不同素材折叠成同一 hash。
    """
    digest = str(asset.get("sha256") or "").strip().lower()
    if digest:
        return "sha256:" + digest
    asset_id = str(asset.get("asset_id") or asset.get("id") or "").strip()
    return "asset:" + asset_id


def shot_content_hash(segment: Dict[str, Any], source_key: str) -> str:
    """按登记内容稳定计算镜头内容哈希（MVP1-AC2/AC10 幂等与重建键）。

    哈希输入为来源素材指纹 + 注册表中登记的镜头内容字段（起止时间戳、
    标签、摘要），不含派生缩略图等可再生产物；同输入必得同哈希，重建
    前后一致。`source_key` 由 `shot_source_key()` 给出且必填：QIN-468 的
    事故根因正是「只按区间/标签/摘要算哈希」，缺省来源会让不同素材的
    镜头再次塌缩成同一 hash。

    迁移说明（QIN-468 / AC3）：`content_hash` 只存在于可重建的 SQLite
    检索索引 `mvp1_shots`，JSON 注册表真源不落该字段，因此语义升级无需
    数据迁移脚本，升级后执行一次检索索引重建即可得到新哈希。
    """
    payload = {
        "source": str(source_key),
        "start_sec": float(segment["start_sec"]),
        "end_sec": float(segment["end_sec"]),
        "tags": list(segment.get("tags") or []),
        "summary": str(segment.get("summary") or ""),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _asset_shot_rows(asset: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """把资产登记的 video_segments 转成镜头级索引行（MVP1-T1 契约）。

    只收敛字段完整且时间戳合法的段落，序号按收敛后顺序从 0 连续编号；
    非法段落跳过并计数返回，不伪造缺省时间戳。
    """
    asset_id = str(asset.get("asset_id") or asset.get("id") or "").strip()
    if not asset_id:
        return [], 0
    metadata = asset.get("metadata")
    segments = metadata.get("video_segments") if isinstance(metadata, dict) else None
    if not isinstance(segments, list):
        return [], 0
    root_id = str(asset.get("root_id") or asset.get("storage_root_id") or "")
    source_key = shot_source_key(asset)
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for segment in segments:
        if not isinstance(segment, dict):
            skipped += 1
            continue
        start = segment.get("start_sec")
        end = segment.get("end_sec")
        if isinstance(start, bool) or isinstance(end, bool):
            skipped += 1
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            skipped += 1
            continue
        try:
            start_sec = float(start)
            end_sec = float(end)
        except (TypeError, ValueError, OverflowError):
            skipped += 1
            continue
        if not (math.isfinite(start_sec) and math.isfinite(end_sec)):
            skipped += 1
            continue
        if start_sec < 0 or end_sec <= start_sec:
            skipped += 1
            continue
        rows.append({
            "asset_id": asset_id,
            "root_id": root_id,
            "shot_index": len(rows),
            "start_sec": start_sec,
            "end_sec": end_sec,
            "content_hash": shot_content_hash(segment, source_key),
        })
    return rows, skipped


def _sync_asset_shot_rows(engine: Any, asset: Dict[str, Any]) -> int:
    """按资产整组刷新镜头行；返回写入行数。

    只处理声明了 ``video_segments`` 的资产（图片等零额外写入）；先删后写保证
    重扫与重切分都不残留旧镜头行，写入内容与 ``rebuild_search_index`` 完全一致。
    """
    metadata = asset.get("metadata")
    if not isinstance(metadata, dict):
        return 0
    if not isinstance(metadata.get("video_segments"), list):
        return 0
    asset_id = str(asset.get("asset_id") or asset.get("id") or "").strip()
    if not asset_id:
        return 0
    rows, _skipped = _asset_shot_rows(asset)
    engine.delete_shots(asset_id)
    if not rows:
        return 0
    engine.batch_upsert_shots(rows)
    return len(rows)


def _upsert_asset_search_documents(assets: Any) -> List[bool]:
    """把资产文档、镜头行与索引元数据写入本地检索索引。

    这是入库（JSON 批量登记、后台索引任务提交）的产品化写入入口（MVP1-T7/F2）：
    此前只写资产文档，镜头行与索引元数据永远为空，导致 `/search/shots` 对已入库
    素材 0 命中、查重候选无数据。镜头行只写入声明了 ``video_segments`` 的资产，
    并按资产整组替换；索引元数据仅在缺失时补写诚实值（无嵌入模型则模型字段为
    空），不覆盖已登记嵌入模型的既有元数据。

    写入失败一律抛 ``CleanroomException``：调用方的 ``state().mutate()`` 随之放弃
    提交，不产生「注册表已入库、检索索引缺失」的半成功状态。
    """
    documents: Dict[str, Dict[str, str]] = {}
    records: List[Dict[str, Any]] = []
    for asset in assets:
        document = _asset_search_document(asset)
        if document is None:
            continue
        documents[document["asset_id"]] = document
        records.append(asset)
    if not documents:
        return []

    from gw.core.sqlite_vec_engine import SQLiteVecEngine

    db_path = storage.data_root() / ASSET_SEARCH_INDEX_FILENAME
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with SQLiteVecEngine(db_path) as engine:
            results = engine.batch_upsert_documents(list(documents.values()))
            for asset in records:
                _sync_asset_shot_rows(engine, asset)
            if engine.index_metadata() is None:
                engine.set_index_metadata(
                    model_id=None,
                    model_version=None,
                    dimensions=engine.dimensions,
                    quantization="float32" if engine.vector_enabled else None,
                )
    except CleanroomException:
        raise
    except Exception as exc:
        raise CleanroomException(
            500, "SEARCH_INDEX_WRITE_FAILED", "本地检索索引写入失败，入库已中止"
        ) from exc
    return results


def _storage_source_for_location(
    root_id: str, canonical_relpath: str, prior_source_id: str = ""
) -> str:
    """根据新位置匹配最长的已登记素材库前缀。"""
    from gw.asset_registry import index_jobs

    _, aliases = index_jobs._roots()
    matches = []
    for alias_id, alias in aliases.items():
        if str(alias.get("root_id") or "") != root_id:
            continue
        raw_prefix = str(alias.get("prefix") or "")
        prefix = storage.canonical_relpath(raw_prefix) if raw_prefix else ""
        if prefix and (
            canonical_relpath != prefix
            and not canonical_relpath.startswith(prefix + "/")
        ):
            continue
        matches.append((len(prefix), str(alias.get("storage_root_id") or alias_id)))
    if not matches:
        return root_id

    longest_prefix = max(length for length, _ in matches)
    source_ids = sorted(
        source_id for length, source_id in matches if length == longest_prefix
    )
    if prior_source_id in source_ids:
        return prior_source_id
    if len(source_ids) > 1:
        raise CleanroomException(
            409,
            "ASSET_SOURCE_AMBIGUOUS",
            "素材路径匹配多个相同优先级的已登记来源",
        )
    return source_ids[0]


def _unavailable(endpoint: str, code: str, message: str) -> None:
    raise CleanroomException(
        503, code, message,
        extra={"endpoint": endpoint, "unavailable": True, "data_status": "not_integrated"},
        expose_extra_fields={"endpoint", "unavailable", "data_status"},
    )

# ---------------------------------------------------------------------------
# 资产 CRUD
# ---------------------------------------------------------------------------

def import_assets(items: List[Dict[str, Any]], source_kind: str, expected_version: Optional[int]) -> Dict[str, Any]:
    """真实创建资产登记；稳定 asset_id 由确定性序号生成。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        created = []
        for entry in items:
            name = str(entry.get("name") or "").strip()
            if not name:
                raise CleanroomException(400, "INVALID_REQUEST", "资产名称不能为空")
            asset_id = _seq(raw, "ast_", raw["assets"].keys())
            metadata = dict(entry.get("metadata") or {})
            if "video_segments" in entry:
                metadata["video_segments"] = entry["video_segments"]
            tags = list(entry.get("tags") or [])
            extra: Dict[str, Any] = {
                "size_bytes": entry.get("size_bytes"),
                "tags": tags,
                "metadata": metadata,
            }
            if entry.get("modified_at"):
                extra["modified_at"] = str(entry["modified_at"])
            if entry.get("frame_rate") is not None:
                extra["frame_rate"] = entry["frame_rate"]
            if entry.get("duration") is not None:
                extra["duration"] = entry["duration"]
            if entry.get("project_id") is not None:
                extra["project_id"] = entry["project_id"]
            if entry.get("project_ids") is not None:
                extra["project_ids"] = list(entry["project_ids"])
            record = _plain_asset(asset_id, name, str(entry.get("kind") or source_kind or "unknown"),
                                  **extra)
            raw["assets"][asset_id] = record
            raw["sequences"][asset_id] = True
            created.append(dict(record))
        _upsert_asset_search_documents(created)
        revision = _bump(raw)
        return {"assets": created, "asset_ids": [a["asset_id"] for a in created],
                "revision": revision, "data_status": DATA_STATUS_OK, "data_gaps": []}
    return state().mutate(mutate)


def update_asset(asset_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        item = _require(raw, asset_id)
        _cas(raw, payload.get("expected_version"))
        if payload.get("name"):
            item["name"] = str(payload["name"]).strip()
        for key in ("kind", "display_path", "archived"):
            if key in payload and payload[key] is not None:
                item[key] = payload[key]
        if "tags" in payload and isinstance(payload["tags"], list):
            item["tags"] = payload["tags"]
        if "metadata" in payload and isinstance(payload["metadata"], dict):
            item.setdefault("metadata", {}).update(payload["metadata"])
        if "video_segments" in payload and isinstance(payload["video_segments"], list):
            item.setdefault("metadata", {})["video_segments"] = payload["video_segments"]
        if "frame_rate" in payload and payload["frame_rate"] is not None:
            item["frame_rate"] = payload["frame_rate"]
        if "duration" in payload and payload["duration"] is not None:
            item["duration"] = payload["duration"]
        if payload.get("display_path") is not None:
            requested_root_id = str(payload.get("root_id") or item.get("root_id") or "")
            path = storage.resolve_request_path(
                str(payload["display_path"]),
                root_id=requested_root_id or None,
            )
            root_id, canonical_relpath = storage.location_identity(path)
            if requested_root_id and root_id != requested_root_id:
                raise CleanroomException(409, "ROOT_ID_MISMATCH", "素材路径与来源根身份不匹配")
            prior_source_id = str(item.get("storage_root_id") or "")
            item["root_id"] = root_id
            item["storage_root_id"] = _storage_source_for_location(
                root_id, canonical_relpath, prior_source_id
            )
            item["canonical_relpath"] = canonical_relpath
        item["version"] = int(item.get("version", 1)) + 1
        item["updated_at"] = storage.now_iso()
        revision = _bump(raw)
        return {"asset": dict(item), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def rename_asset_file(
    asset_id: str, expected_version: int, new_name: str, display_path: str
) -> Dict[str, Any]:
    """物理重命名已在事务内完成；本函数在同一注册表事务内记账并做 CAS。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        item = _require(raw, asset_id)
        current = int(item.get("version", 1))
        if expected_version != current:
            raise CleanroomException(
                409,
                "VERSION_CONFLICT",
                "素材版本冲突，请重新读取后重试",
                extra={
                    "expected_version": expected_version,
                    "current_version": current,
                },
                expose_extra_fields={"expected_version", "current_version"},
            )
        item["name"] = new_name
        item["display_path"] = display_path
        item["canonical_relpath"] = storage.canonical_relpath(display_path)
        item["version"] = int(item.get("version", 1)) + 1
        item["updated_at"] = storage.now_iso()
        revision = _bump(raw)
        return {"asset": dict(item), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def archive_assets(asset_ids: List[str], expected_version: Optional[int]) -> Dict[str, Any]:
    """归档资产（不是删除）：移出活跃集合，可被 restore 恢复。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        archived = []
        for asset_id in asset_ids:
            item = raw["assets"].get(asset_id)
            if item is None:
                continue
            item = _require(raw, asset_id)
            item["archived"] = True
            item["archived_at"] = storage.now_iso()
            item["version"] = int(item.get("version", 1)) + 1
            archived.append(asset_id)
        revision = _bump(raw)
        return {"archived": archived, "count": len(archived),
                "assets": [dict(raw["assets"][aid]) for aid in archived],
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def delete_asset(asset_id: str, expected_version: Optional[int], context=None) -> Dict[str, Any]:
    """删除资产：移入回收站并保留可恢复副本。

    同进同出裁定（C4-AC6 / issue #12）：资产注册表是资产存在性的唯一真源。工作流产物与
    工作流文档经 asset_hub_bridge 以同一 asset_id 同时登记在本注册表和素材库
    （AssetLibraryService），两份记录必须随删除/恢复同进同出：

    - 删除时把同 asset_id 的素材库条目一并移除，完整记录暂存进回收站条目
      payload["library_item"]；素材库没有同名条目时不做任何素材库改动。
    - 原子顺序：先移除素材库条目，素材库更新失败则整个删除失败（注册表未动）；
      素材库成功后注册表写入失败则回滚素材库移除，不留半程状态。
    - 恢复见 restore_from_recycle：暂存条目原样放回（同 id/同库/同分类/同字段，幂等）。
    """
    from gw.asset_library.service import default_asset_library_service
    snapshot = state().read()
    _require(snapshot, asset_id)
    _cas(snapshot, expected_version, required=True)
    library_item = default_asset_library_service.remove_item_snapshot(asset_id)
    try:
        def mutate(raw: Dict[str, Any]) -> Any:
            item = _require(raw, asset_id)
            _cas(raw, expected_version, required=True)
            del raw["assets"][asset_id]
            payload = dict(item)
            if library_item is not None:
                payload["library_item"] = library_item
            entry_id = _seq(raw, "rcy_", raw["recycle_bin"].keys())
            raw["recycle_bin"][entry_id] = {"entry_id": entry_id, "kind": "asset", "asset_id": asset_id,
                                            "payload": payload, "deleted_at": storage.now_iso()}
            event_id = audit_sink.append_event(raw, "asset.deleted", asset_id, context)
            revision = _bump(raw)
            return {"event_id": event_id, "asset_id": asset_id, "deleted": True, "recycle_entry_id": entry_id,
                    "recycled": {"entry_id": entry_id, "asset_id": asset_id, "removed_previews": 0},
                    "revision": revision, "data_status": DATA_STATUS_OK}
        return state().mutate(mutate)
    except Exception:
        if library_item is not None:
            default_asset_library_service.insert_item_snapshot(library_item)
        raise


def restore_from_recycle(entry_id: str, context=None, expected_version: Optional[int] = None) -> Dict[str, Any]:
    """从回收站恢复条目（同进同出：见 delete_asset docstring 的裁定）。

    回收站条目 payload["library_item"] 暂存过素材库条目时，恢复会把该条目原样放回
    素材库（同 id/同库/同分类/同字段；同 asset_id 已存在则幂等跳过），并从恢复的
    注册表记录中剥掉暂存键。原子顺序与删除对称：先回插素材库（失败即整体失败，
    注册表未动）；注册表恢复失败（404/409）则回滚刚插入的素材库条目。
    """
    from gw.asset_library.service import default_asset_library_service
    peek = state().read()
    _cas(peek, expected_version, required=True)
    existing = peek["recycle_bin"].get(entry_id)
    if existing is None:
        raise CleanroomException(404, "RECYCLE_ENTRY_NOT_FOUND", "回收站条目不存在")
    stashed = dict(existing.get("payload") or {}).get("library_item")
    inserted = False
    if existing["kind"] == "asset" and stashed is not None:
        from gw.core.asset_visibility import asset_visible
        if not asset_visible(existing.get("payload", {})):
            raise CleanroomException(404, "RECYCLE_ENTRY_NOT_FOUND", "回收站条目不存在")
        if existing["asset_id"] in peek["assets"]:
            raise CleanroomException(409, "ASSET_RESTORE_CONFLICT", "恢复目标资产已经存在")
        inserted = default_asset_library_service.insert_item_snapshot(stashed)
    try:
        def mutate(raw: Dict[str, Any]) -> Any:
            _cas(raw, expected_version, required=True)
            entry = raw["recycle_bin"].get(entry_id)
            if entry is None:
                raise CleanroomException(404, "RECYCLE_ENTRY_NOT_FOUND", "回收站条目不存在")
            if entry["kind"] == "asset":
                from gw.core.asset_visibility import asset_visible
                if not asset_visible(entry.get('payload', {})):
                    raise CleanroomException(404, 'RECYCLE_ENTRY_NOT_FOUND', '回收站条目不存在')
                if entry["asset_id"] in raw["assets"]:
                    raise CleanroomException(409, "ASSET_RESTORE_CONFLICT", "恢复目标资产已经存在")
                payload = dict(entry["payload"])
                payload.pop("library_item", None)
                raw["assets"][entry["asset_id"]] = payload
                audit_sink.append_event(raw, "asset.recycle_restored", entry["asset_id"], context)
            del raw["recycle_bin"][entry_id]
            revision = _bump(raw)
            return {"entry_id": entry_id, "restored": True, "kind": entry["kind"],
                    "revision": revision, "data_status": DATA_STATUS_OK}
        return state().mutate(mutate)
    except Exception:
        if inserted and isinstance(stashed, dict):
            default_asset_library_service.remove_item_snapshot(str(stashed.get("asset_id") or ""))
        raise


def restore_asset(asset_id: str, context=None) -> Dict[str, Any]:
    """从归档状态恢复资产。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        item = _require(raw, asset_id)
        was_archived = bool(item.get("archived"))
        item["archived"] = False
        item.pop("archived_at", None)
        if was_archived:
            audit_sink.append_event(raw, "asset.unarchived", asset_id, context)
        item["version"] = int(item.get("version", 1)) + 1
        revision = _bump(raw)
        return {"asset_id": asset_id, "restored": True, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def get_asset(asset_id: str) -> Dict[str, Any]:
    raw = state().read()
    item = annotate_asset_provenance([_require(raw, asset_id)], raw=raw)[0]
    # 详情同时回填已关联素材，前端据此渲染关联列表与「解除关联」入口。
    return {**item, "related_assets": _related_assets_for(raw, item)}


_ASSET_CATEGORY_EXTENSIONS = {
    "image": frozenset({
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif",
        "avif", "raw", "svg", "eps", "psd", "ai", "cdr", "hdr", "exr",
    }),
    "video": frozenset({"mp4", "mov", "mkv", "avi", "webm", "m4v", "flv", "wmv"}),
    "audio": frozenset({"mp3", "wav", "flac", "aac", "ogg", "m4a", "aiff", "aif", "wma", "opus"}),
    "document": frozenset({
        "txt", "csv", "tsv", "json", "xml", "html", "htm", "md", "markdown", "rtf", "doc", "docx",
        "odt", "ppt", "pptx", "odp", "xls", "xlsx", "ods", "pdf", "srt", "vtt", "zip",
    }),
    "model": frozenset({
        "stl", "3mf", "obj", "fbx", "gltf", "glb", "usd", "usdz", "step", "stp", "iges", "igs",
        "dxf", "ply", "3ds", "dae", "ifc", "wrl", "vrml", "blend", "c4d", "max", "ma", "mb",
        "ckpt", "safetensors", "pt", "pth", "onnx", "gguf",
    }),
    "font": frozenset({"ttf", "otf", "ttc", "otc", "woff", "woff2"}),
}
_ASSET_CATEGORY_BY_EXTENSION = {
    extension: category
    for category, extensions in _ASSET_CATEGORY_EXTENSIONS.items()
    for extension in extensions
}


def _asset_extension(asset: Dict[str, Any]) -> str:
    metadata = asset.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    values = [
        asset.get("name"), asset.get("display_path"), asset.get("relative_path"), asset.get("file_name"),
        asset.get("filename"), metadata.get("name"), metadata.get("file_name"),
        metadata.get("filename"), metadata.get("original_filename"),
    ]
    direct_extension = str(metadata.get("extension") or metadata.get("file_extension") or "")
    values.append(direct_extension)
    for value in values:
        text = str(value or "").split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
        name = text.rsplit("/", 1)[-1].strip().lower()
        extension = name.rsplit(".", 1)[-1] if "." in name else name.lstrip(".")
        if extension in _ASSET_CATEGORY_BY_EXTENSION:
            return extension
    return ""


def _asset_category(asset: Dict[str, Any]) -> str:
    extension_category = _ASSET_CATEGORY_BY_EXTENSION.get(_asset_extension(asset))
    if extension_category:
        return extension_category
    kind = str(asset.get("kind") or asset.get("media_type") or asset.get("type") or "").lower()
    if kind == "text":
        return "document"
    return kind if kind in _ASSET_CATEGORY_EXTENSIONS else "other"


def list_assets(limit: int, offset: int, query: Optional[str], kind: Optional[str],
                archived: Optional[bool], *, cursor: Optional[str] = None, sort: Optional[str] = None,
                view: Optional[str] = None, category: Optional[str] = None, tag: Optional[str] = None,
                project_id: Optional[str] = None, include_tags: Optional[str] = None,
                exclude_tags: Optional[str] = None, root_id: Optional[str] = None,
                collection_id: Optional[str] = None, recent_days: Optional[int] = None,
                structured: bool = False, scope: Any = None, text: Optional[str] = None,
                filters: Any = None, include: Any = None,
                entity_type: Optional[str] = None,
                entity_id: Optional[str] = None,
                shot_size: Optional[str] = None,
                camera_movement: Optional[str] = None,
                focal_length: Optional[str] = None,
                lighting_phase: Optional[str] = None,
                aspect_ratio: Optional[str] = None,
                include_removed: bool = False,
                dynamic_limit: Optional[int] = None,
                folder_path: Optional[str] = None,
                board_sources: Optional[str] = None) -> Dict[str, Any]:
    if structured:
        if (
            root_id
            or folder_path
            or board_sources
            or collection_id
            or any(
                value is not None
                for value in (
                    kind,
                    archived,
                    category,
                    tag,
                    project_id,
                    include_tags,
                    exclude_tags,
                    recent_days,
                    entity_type,
                    entity_id,
                    shot_size,
                    camera_movement,
                    focal_length,
                    lighting_phase,
                    aspect_ratio,
                )
            )
        ):
            raise CleanroomException(400, "UNSUPPORTED_FILTER_VALUE",
                                     "结构化查询不能混用旧版过滤参数")
        return _list_structured_assets(
            limit=limit,
            offset=offset,
            cursor=cursor,
            scope=scope,
            text=text,
            filters=filters,
            sort=sort,
            include=include,
            include_removed=include_removed,
        )
    cutoff = None
    if recent_days is not None:
        if type(recent_days) is not int or not 1 <= recent_days <= 36500:
            raise CleanroomException(400, "INVALID_REQUEST", "recent_days必须是1到36500的整数")
        cutoff = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                  - timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = state().read()
    ordering = sort or "asset_id"
    if ordering not in {"asset_id", "newest", "oldest", "name", "size_desc"}:
        raise CleanroomException(400, "UNSUPPORTED_OPTION", "不支持的资产排序")
    if view and view not in {"card", "grid", "list"}:
        raise CleanroomException(400, "UNSUPPORTED_OPTION", "不支持的资产显示方式")

    cinematography_input: Dict[str, Any] = {}
    for key, val in (
        ("entity_type", entity_type),
        ("entity_id", entity_id),
        ("shot_size", shot_size),
        ("camera_movement", camera_movement),
        ("focal_length", focal_length),
        ("lighting_phase", lighting_phase),
        ("aspect_ratio", aspect_ratio),
    ):
        if val is not None:
            cinematography_input[key] = val

    normalized_cinematography: Dict[str, Any] = {}
    if cinematography_input:
        try:
            validated = CinematographyAssetFilters.model_validate(cinematography_input)
            normalized_cinematography = validated.model_dump(
                mode="json", exclude_unset=True
            )
        except ValidationError as exc:
            paths = sorted(
                {
                    ".".join(str(part) for part in error.get("loc", ()))
                    for error in exc.errors()
                }
            )
            field_paths = ", ".join(paths) or "filters"
            raise CleanroomException(
                400,
                "UNSUPPORTED_FILTER_VALUE",
                f"摄制或实体母库过滤字段无效：{field_paths}",
            ) from None

    includes = sorted({part.strip() for part in (include_tags or "").split(",") if part.strip()})
    excludes = sorted({part.strip() for part in (exclude_tags or "").split(",") if part.strip()})
    cinematography_tokens = sorted(
        (key, tuple(val) if isinstance(val, list) else val)
        for key, val in normalized_cinematography.items()
    )
    board_scope = None
    if board_sources is not None:
        try:
            board_scope = json.loads(board_sources)
            if (not isinstance(board_scope, dict)
                    or set(board_scope) != {"visible", "known", "automatic", "automatic_only"}
                    or any(not isinstance(board_scope[key], list)
                           or any(not isinstance(value, str) or len(value) > 128 for value in board_scope[key])
                           or len(board_scope[key]) > 500 for key in ("visible", "known"))
                    or any(type(board_scope[key]) is not bool for key in ("automatic", "automatic_only"))):
                raise ValueError()
        except (ValueError, TypeError):
            raise CleanroomException(400, "INVALID_REQUEST", "看板来源过滤无效") from None
    filters = [query or "", kind or "", archived, ordering, category or "", tag or "", project_id or "",
               includes, excludes, cutoff, root_id or "", collection_id or "",
               cinematography_tokens, folder_path or "", board_scope]
    fingerprint = hashlib.sha256(json.dumps(filters, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            cursor_keys = set(decoded) if isinstance(decoded, dict) else set()
            if (not isinstance(decoded, dict)
                    or cursor_keys not in ({"offset", "revision", "query"},
                                           {"offset", "revision", "query", "last_location"})
                    or type(decoded["offset"]) is not int or decoded["offset"] < 0
                    or type(decoded["revision"]) is not int or decoded["query"] != fingerprint
                    or (offset and offset != decoded["offset"])):
                raise ValueError()
            if "last_location" in decoded and (
                not isinstance(decoded["last_location"], dict)
                or set(decoded["last_location"]) != {"root_id", "canonical_relpath"}
                or not all(isinstance(value, str) for value in decoded["last_location"].values())
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise CleanroomException(400, "INVALID_CURSOR", "分页游标无效或不属于当前查询") from None
        if decoded["revision"] != raw["revision"]:
            raise CleanroomException(409, "VERSION_CONFLICT", "资产列表已变化，请从第一页重新读取",
                                     extra={"expected_version": decoded["revision"], "current_version": raw["revision"]},
                                     expose_extra_fields={"expected_version", "current_version"})
        offset = decoded["offset"]
    from gw.core.asset_visibility import asset_visible
    removed_ids = set() if include_removed else _removed_root_ids()
    items = [
        dict(a) for a in raw["assets"].values()
        if asset_visible(a) and (include_removed or not _is_removed_asset(a, removed_ids))
    ]
    root_facets = []
    if root_id or any(
        not item.get("is_remote") and (item.get("storage_root_id") or item.get("root_id"))
        for item in items
    ):
        from gw.asset_registry import index_jobs

        root_facets = index_jobs.root_facets()
    root_availability = {
        str(root.get("id") or ""): str(root.get("availability") or "offline")
        for root in root_facets
    }
    root_availability_reason = {
        str(root.get("id") or ""): str(root.get("availability_reason") or "")
        for root in root_facets
    }
    from gw.settings.service import default_storage_settings_service
    source_read_only = default_storage_settings_service.get_snapshot().local_library_read_only
    for item in items:
        source_id = str(item.get("storage_root_id") or "")
        asset_root_id = str(item.get("root_id") or "")
        item["can_recycle"] = False
        if not item.get("is_remote") and (source_id or asset_root_id):
            availability_id = source_id or asset_root_id
            item["can_recycle"] = not source_read_only.get(availability_id, False) and root_availability.get(availability_id) == "available"
            item["root_availability"] = root_availability.get(
                availability_id, "offline"
            )
            item["root_availability_reason"] = root_availability_reason.get(
                availability_id, "MOUNT_ROOT_REMOVED"
            )
    if root_id:
        known_roots = {item["id"] for item in root_facets}
        if root_id not in known_roots:
            raise CleanroomException(404, "ROOT_NOT_FOUND", "素材来源根不存在")
        items = [a for a in items if str(a.get("storage_root_id") or a.get("root_id") or "") == root_id]
    elif not include_removed:
        items = [a for a in items if a.get("root_availability") != "removed"]
    if board_scope is not None:
        visible_sources = set(board_scope["visible"])
        known_sources = set(board_scope["known"])
        def belongs_to_board(asset: Dict[str, Any]) -> bool:
            source = str(asset.get("storage_root_id") or "")
            if board_scope["automatic_only"]:
                return bool(visible_sources and board_scope["automatic"] and source not in known_sources)
            if source in known_sources:
                return source in visible_sources
            return bool(visible_sources and board_scope["automatic"])
        items = [item for item in items if belongs_to_board(item)]
    if folder_path:
        selected_folder = folder_path.replace("\\", "/").strip("/")
        def in_folder(asset: Dict[str, Any]) -> bool:
            relative = str(asset.get("relative_path") or asset.get("canonical_relpath") or "").replace("\\", "/").strip("/")
            folder = relative.rpartition("/")[0]
            if selected_folder == "__root__":
                return not folder
            return folder == selected_folder or folder.startswith(selected_folder + "/")
        items = [item for item in items if in_folder(item)]
    if collection_id:
        def belongs_to_collection(asset: Dict[str, Any]) -> bool:
            value = asset.get("collection_id")
            if value is None:
                value = asset.get("folder_id")
            if isinstance(value, (list, tuple, set)):
                return collection_id in {str(item) for item in value}
            return str(value or "") == collection_id
        items = [a for a in items if belongs_to_collection(a)]
    if archived is not None:
        items = [a for a in items if bool(a.get("archived")) == bool(archived)]
    if kind:
        normalized_kind = str(kind).lower()
        if normalized_kind in {*_ASSET_CATEGORY_EXTENSIONS, "other"}:
            items = [a for a in items if _asset_category(a) == normalized_kind]
        else:
            items = [a for a in items if str(a.get("kind") or "") == kind]
    if query:
        needle = query.casefold()
        def _matches_query(a: Dict[str, Any]) -> bool:
            if needle in str(a.get("name", "")).casefold():
                return True
            if needle in str(a.get("asset_id", "")).casefold():
                return True
            for tag_val in a.get("tags") or []:
                if needle in str(tag_val).casefold():
                    return True
            metadata = a.get("metadata") or {}
            for seg in metadata.get("video_segments") or []:
                if isinstance(seg, dict):
                    for st in seg.get("tags") or []:
                        if needle in str(st).casefold():
                            return True
                    if needle in str(seg.get("summary") or "").casefold():
                        return True
            return False
        items = [a for a in items if _matches_query(a)]
    if cutoff:
        items = [a for a in items if str(a.get("created_at") or "") >= cutoff]
    if category:
        items = [a for a in items if _asset_category(a) == category]
    if project_id:
        items = [a for a in items if project_id in a.get("project_ids", []) or a.get("project_id") == project_id]
    def tagged(asset, selected):
        return selected in asset.get("tags", []) or any(
            str(raw["tags"].get(value, {}).get("name", "")).casefold() == selected.casefold()
            for value in asset.get("tags", []))
    if tag:
        includes.append(tag)
    if includes:
        items = [a for a in items if all(tagged(a, selected) for selected in includes)]
    if excludes:
        items = [a for a in items if not any(tagged(a, selected) for selected in excludes)]
    if normalized_cinematography:
        active_cinematography = dict(normalized_cinematography)
        if project_id and "project_id" not in active_cinematography:
            active_cinematography["project_id"] = [project_id]
        items = _apply_cinematography_filters(items, active_cinematography)
    # 先以稳定ID排序，再稳定按所选字段排序，保证同值分页不重复/丢项。
    items.sort(key=lambda a: a["asset_id"])
    if ordering in {"newest", "oldest"}:
        items.sort(key=lambda a: str(a.get("created_at") or ""), reverse=ordering == "newest")
    elif ordering == "name":
        items.sort(key=lambda a: str(a.get("name") or "").casefold())
    elif ordering == "size_desc":
        items.sort(key=lambda a: a.get("size_bytes") if type(a.get("size_bytes")) in {int, float} else -1, reverse=True)
    total = len(items)
    if dynamic_limit is not None and total > 1500:
        limit = min(limit, dynamic_limit)
    page = items[offset:offset + limit]
    # 只装饰当前页：来源名与文件修改时间按页下发，避免为全库逐条 stat。
    page = annotate_asset_provenance(page, raw=raw)
    # 分页副本上补关联素材摘要：只读路径不回写状态，副本避免污染后续 mutate 的原始记录。
    relation_index = _relation_target_index(raw)
    page = [
        {**asset, "related_assets": _related_assets_for(raw, asset, relation_index)}
        for asset in page
    ]
    has_more = offset + len(page) < total
    next_offset = offset + len(page) if has_more else None
    next_cursor = None
    if has_more:
        last_asset = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "offset": next_offset,
            "revision": raw["revision"],
            "query": fingerprint,
            "last_location": {
                "root_id": str(last_asset.get("root_id") or ""),
                "canonical_relpath": str(last_asset.get("canonical_relpath") or ""),
            },
        }, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"assets": page, "items": page, "total": total, "total_known": True,
            "limit": limit, "offset": offset, "has_more": has_more, "next_offset": next_offset,
            "next_cursor": next_cursor, "revision": raw["revision"],
            "data_status": DATA_STATUS_OK, "data_gaps": []}


def _structured_cursor_fingerprint(normalized: Dict[str, Any]) -> str:
    """游标只绑定规范化查询和当前认证主体的不可逆归属键。"""
    from gw.core.asset_visibility import asset_owner_key, current_auth_context

    context = current_auth_context.get()
    principal = asset_owner_key(context) if context is not None else "anonymous"
    payload = {"query": normalized, "principal": principal}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _structured_asset_facets(items: List[Dict[str, Any]], raw: Dict[str, Any]) -> Dict[str, Any]:
    kinds = Counter(str(asset.get("kind") or "unknown") for asset in items)
    categories = Counter(_asset_category(asset) for asset in items)
    tag_counts = Counter(tag for asset in items for tag in set(asset.get("tags", [])))
    tags = [
        {**value, "count": tag_counts.get(value["tag_id"], 0)}
        for value in sorted(raw["tags"].values(), key=lambda item: item["tag_id"])
        if tag_counts.get(value["tag_id"], 0)
    ]
    return {
        "kinds": [{"name": name, "count": count} for name, count in sorted(kinds.items())],
        "categories": [{"name": name, "count": count} for name, count in sorted(categories.items())],
        "tags": tags,
        "collections": [],
        "roots": [],
        "revision": raw["revision"],
    }


def _structured_asset_projection(asset: Dict[str, Any], *, include_preview: bool) -> Dict[str, Any]:
    """把旧资产记录投影为 C11 稳定实体字段，不改变落盘记录。"""
    item = dict(asset)
    item.pop("id", None)
    item["entity_type"] = "asset"
    item["tag_id"] = list(asset.get("tags") or [])
    if not item.get("project_id") and asset.get("project_ids"):
        item["project_id"] = asset["project_ids"][0]
    item["status"] = "archived" if bool(asset.get("archived")) else "active"
    if include_preview:
        item["preview"] = {
            "availability": "not_integrated",
            "reason_code": "MEDIA_PROXY_NOT_INTEGRATED",
            "next_action": "use_asset_media_endpoint",
        }
    return item


def _separate_cinematography_filters(filters: Any) -> Tuple[Dict[str, Any], Any]:
    """把新摄制筛选从旧过滤器中分出，其余字段仍交给 C11 校验器。"""
    parsed = filters
    if isinstance(filters, str):
        stripped = filters.strip()
        if not stripped.startswith("{"):
            return {}, filters
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return {}, filters
    if not isinstance(parsed, dict):
        return {}, filters

    fields = set(CinematographyAssetFilters.model_fields)
    selected = {key: value for key, value in parsed.items() if key in fields}
    if not selected:
        return {}, filters
    legacy = {key: value for key, value in parsed.items() if key not in fields}
    try:
        normalized = CinematographyAssetFilters.model_validate(selected)
    except ValidationError as exc:
        paths = sorted(
            {
                ".".join(str(part) for part in error.get("loc", ()))
                for error in exc.errors()
            }
        )
        field_paths = ", ".join(
            f"filters.{path}" if path else "filters" for path in paths
        ) or "filters"
        raise CleanroomException(
            400,
            "UNSUPPORTED_FILTER_VALUE",
            f"摄制或实体母库过滤字段无效：{field_paths}",
        ) from None
    return normalized.model_dump(mode="json", exclude_unset=True), legacy


def _asset_metadata_value(asset: Dict[str, Any], field: str) -> Any:
    metadata = asset.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    for source in (
        metadata.get("cinematography"),
        asset.get("cinematography"),
        metadata,
        asset,
    ):
        if isinstance(source, dict) and source.get(field) is not None:
            return source[field]
    return None


def _matches_metadata_values(
    asset: Dict[str, Any], field: str, expected: set[str]
) -> bool:
    value = _asset_metadata_value(asset, field)
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    return any(
        str(getattr(item, "value", item)) in expected
        for item in values
        if item is not None
    )


def _asset_master_entity_ids(asset: Dict[str, Any]) -> set[str]:
    metadata = asset.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    values = metadata.get("entity_ids", asset.get("entity_ids", []))
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip() and value.isascii()
    }


def _entity_filter_index() -> Tuple[
    Tuple[
        Dict[str, Tuple[str, str]],
        Dict[str, set[str]],
        Dict[Tuple[str, str], set[str]],
    ],
    int,
]:
    """按本次读取的母库内容缓存实体 ID、类型及项目/类型组合索引。"""
    global _ENTITY_FILTER_INDEX_CACHE_KEY, _ENTITY_FILTER_INDEX_CACHE_VALUE

    from gw.entities import repository as entities_repository

    raw, _, entity_bytes = entities_repository.read_entities_snapshot()
    content_bytes = entity_bytes if entity_bytes is not None else b""
    revision = raw["revision"]
    records = raw["entities"]

    cache_key = (
        str(storage.data_root()),
        hashlib.sha256(content_bytes).hexdigest(),
    )
    with _ENTITY_FILTER_INDEX_CACHE_LOCK:
        if _ENTITY_FILTER_INDEX_CACHE_KEY == cache_key:
            return _ENTITY_FILTER_INDEX_CACHE_VALUE, revision

    by_id: Dict[str, Tuple[str, str]] = {}
    by_type: Dict[str, set[str]] = {}
    by_project_type: Dict[Tuple[str, str], set[str]] = {}
    for key, record in records.items():
        if not isinstance(record, dict) or record.get("archived"):
            continue
        entity_id = record.get("entity_id")
        project_id = record.get("project_id")
        entity_type = record.get("entity_type")
        if (
            not isinstance(entity_id, str)
            or entity_id != str(key)
            or entity_id != entity_id.strip()
            or not entity_id
            or not entity_id.isascii()
            or any(char.isspace() for char in entity_id)
            or not isinstance(project_id, str)
            or project_id != project_id.strip()
            or not project_id
            or not project_id.isascii()
            or any(char.isspace() for char in project_id)
        ):
            continue
        try:
            normalized_type = MasterEntityType(entity_type).value
        except (TypeError, ValueError):
            continue
        by_id[entity_id] = (normalized_type, project_id)
        by_type.setdefault(normalized_type, set()).add(entity_id)
        by_project_type.setdefault(
            (project_id, normalized_type), set()
        ).add(entity_id)

    index = (by_id, by_type, by_project_type)
    with _ENTITY_FILTER_INDEX_CACHE_LOCK:
        if _ENTITY_FILTER_INDEX_CACHE_KEY != cache_key:
            _ENTITY_FILTER_INDEX_CACHE_KEY = cache_key
            _ENTITY_FILTER_INDEX_CACHE_VALUE = index
        return _ENTITY_FILTER_INDEX_CACHE_VALUE, revision


def _apply_cinematography_filters(
    items: List[Dict[str, Any]],
    filters: Dict[str, Any],
    *,
    entity_indexes: Optional[
        Tuple[
            Dict[str, Tuple[str, str]],
            Dict[str, set[str]],
            Dict[Tuple[str, str], set[str]],
        ]
    ] = None,
) -> List[Dict[str, Any]]:
    for field in (
        "shot_size",
        "camera_movement",
        "focal_length",
        "lighting_phase",
        "aspect_ratio",
    ):
        expected = set(filters.get(field) or [])
        if not expected:
            continue
        items = [
            asset
            for asset in items
            if _matches_metadata_values(asset, field, expected)
        ]

    selected_entity_ids = set(filters.get("entity_id") or [])
    selected_entity_types = set(filters.get("entity_type") or [])
    if selected_entity_ids or selected_entity_types:
        if entity_indexes is None:
            entity_indexes, _ = _entity_filter_index()
        by_id, by_type, by_project_type = entity_indexes
        raw_project_scope = filters.get("project_id") or []
        if isinstance(raw_project_scope, str):
            raw_project_scope = [raw_project_scope]
        project_scope = set(raw_project_scope)
        if selected_entity_ids:
            # 精确 ID 过滤直接查映射，避免随母库规模增长而扫描全部实体。
            eligible_ids = set()
            for entity_id in selected_entity_ids:
                record = by_id.get(entity_id)
                if record is None:
                    continue
                entity_type, entity_project_id = record
                if selected_entity_types and entity_type not in selected_entity_types:
                    continue
                if project_scope and entity_project_id not in project_scope:
                    continue
                eligible_ids.add(entity_id)
        elif project_scope:
            eligible_ids = set().union(
                *(
                    by_project_type.get((proj_id, entity_type), set())
                    for proj_id in project_scope
                    for entity_type in selected_entity_types
                )
            )
        else:
            eligible_ids = set().union(
                *(by_type.get(entity_type, set()) for entity_type in selected_entity_types)
            )
        items = [
            asset
            for asset in items
            if _asset_master_entity_ids(asset).intersection(eligible_ids)
        ]
    return items


def _list_structured_assets(
    *,
    limit: int,
    offset: int,
    cursor: Optional[str],
    scope: Any,
    text: Any,
    filters: Any,
    sort: Optional[str],
    include: Any,
    include_removed: bool = False,
) -> Dict[str, Any]:
    cinematography_filters, legacy_filters = _separate_cinematography_filters(filters)
    normalized = normalize_asset_query(
        scope=scope,
        text=text,
        filters=legacy_filters,
        sort=sort,
        limit=limit,
        include=include,
    )
    normalized["filters"].update(cinematography_filters)
    raw = state().read()
    filter_values = normalized["filters"]
    has_entity_filter = bool(
        filter_values.get("entity_id") or filter_values.get("entity_type")
    )
    entity_indexes: Optional[
        Tuple[
            Dict[str, Tuple[str, str]],
            Dict[str, set[str]],
            Dict[Tuple[str, str], set[str]],
        ]
    ] = None
    entity_revision: Optional[int] = None
    if has_entity_filter:
        entity_indexes, entity_revision = _entity_filter_index()
    fingerprint = _structured_cursor_fingerprint(normalized)
    cursor_last_id: Optional[str] = None
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            decoded = json.loads(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
            expected_cursor_keys = {"offset", "revision", "query", "last_id"}
            if has_entity_filter:
                expected_cursor_keys.add("entity_revision")
            if (
                not isinstance(decoded, dict)
                or set(decoded) != expected_cursor_keys
                or type(decoded["offset"]) is not int
                or decoded["offset"] < 0
                or type(decoded["revision"]) is not int
                or (
                    has_entity_filter
                    and (
                        type(decoded["entity_revision"]) is not int
                        or decoded["entity_revision"] < 1
                    )
                )
                or decoded["query"] != fingerprint
                or (offset and offset != decoded["offset"])
                or not isinstance(decoded["last_id"], str)
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise CleanroomException(400, "INVALID_CURSOR", "分页游标无效或不属于当前查询") from None
        if decoded["revision"] != raw["revision"]:
            raise CleanroomException(
                409,
                "VERSION_CONFLICT",
                "资产列表已变化，请从第一页重新读取",
                extra={"expected_version": decoded["revision"], "current_version": raw["revision"]},
                expose_extra_fields={"expected_version", "current_version"},
            )
        if has_entity_filter and decoded["entity_revision"] != entity_revision:
            raise CleanroomException(
                409,
                "VERSION_CONFLICT",
                "实体母库已变化，请从第一页重新读取",
                extra={
                    "expected_version": decoded["entity_revision"],
                    "current_version": entity_revision,
                },
                expose_extra_fields={"expected_version", "current_version"},
            )
        offset = decoded["offset"]
        cursor_last_id = decoded["last_id"]

    from gw.core.asset_visibility import asset_visible

    removed_ids = set() if include_removed else _removed_root_ids()
    items = [
        dict(asset)
        for asset in raw["assets"].values()
        if asset_visible(asset) and (include_removed or not _is_removed_asset(asset, removed_ids))
    ]
    needle = normalized["text"].casefold()
    if needle:
        def _matches_text(asset: Dict[str, Any]) -> bool:
            if needle in str(asset.get("name") or "").casefold():
                return True
            if needle in str(asset.get("asset_id") or "").casefold():
                return True
            for tag_val in asset.get("tags") or []:
                if needle in str(tag_val).casefold():
                    return True
            metadata = asset.get("metadata") or {}
            for seg in metadata.get("video_segments") or []:
                if isinstance(seg, dict):
                    for st in seg.get("tags") or []:
                        if needle in str(st).casefold():
                            return True
                    if needle in str(seg.get("summary") or "").casefold():
                        return True
            return False

        items = [asset for asset in items if _matches_text(asset)]
    if filter_values.get("kind"):
        kinds = set(filter_values["kind"])
        items = [asset for asset in items if _asset_category(asset) in kinds]
    if filter_values.get("project_id"):
        projects = set(filter_values["project_id"])
        items = [
            asset
            for asset in items
            if projects.intersection(set(asset.get("project_ids") or [])
                                     | ({asset["project_id"]} if asset.get("project_id") else set()))
        ]
    if filter_values.get("tag_id"):
        tags = set(filter_values["tag_id"])
        items = [asset for asset in items if tags.intersection(set(asset.get("tags") or []))]
    if filter_values.get("status", {}).get("asset"):
        statuses = set(filter_values["status"]["asset"])
        items = [
            asset
            for asset in items
            if ("archived" if bool(asset.get("archived")) else "active") in statuses
        ]
    if "created_at" in filter_values:
        window = filter_values["created_at"]
        start = parse_utc_datetime(window["from"]) if window.get("from") else None
        end = parse_utc_datetime(window["to"]) if window.get("to") else None
        filtered: List[Dict[str, Any]] = []
        for asset in items:
            try:
                created = parse_utc_datetime(asset.get("created_at"))
            except CleanroomException:
                continue
            if start is not None and created < start:
                continue
            if end is not None and created >= end:
                continue
            filtered.append(asset)
        items = filtered

    items = _apply_cinematography_filters(
        items, filter_values, entity_indexes=entity_indexes
    )

    # 先按稳定 ID，再按主排序键，保证同值时 ID 升序且跨页不重不漏。
    items.sort(key=lambda asset: str(asset.get("asset_id") or ""))
    if normalized["sort"] in {"newest", "oldest"}:
        items.sort(
            key=lambda asset: str(asset.get("created_at") or ""),
            reverse=normalized["sort"] == "newest",
        )
    elif normalized["sort"] == "name":
        items.sort(key=lambda asset: str(asset.get("name") or "").casefold())

    if cursor is not None and (offset == 0 or offset > len(items)
                               or items[offset - 1].get("asset_id") != cursor_last_id):
        raise CleanroomException(400, "INVALID_CURSOR", "分页游标无效或不属于当前查询")

    total = len(items)
    page = items[offset:offset + limit]
    page = annotate_asset_provenance(page, raw=raw)
    has_more = offset + len(page) < total
    next_offset = offset + len(page) if has_more else None
    next_cursor = None
    if has_more:
        cursor_payload = {
            "offset": next_offset,
            "revision": raw["revision"],
            "query": fingerprint,
            "last_id": page[-1]["asset_id"],
        }
        if has_entity_filter:
            cursor_payload["entity_revision"] = entity_revision
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(cursor_payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")

    include_values = set(normalized["include"])
    projected = [
        {
            **_structured_asset_projection(asset, include_preview="preview" in include_values),
            "related_assets": _related_assets_for(raw, asset),
        }
        for asset in page
    ]
    applied_filters: Dict[str, Any] = {"scope": normalized["scope"], "sort": normalized["sort"]}
    if normalized["text"]:
        applied_filters["text"] = normalized["text"]
    applied_filters.update(filter_values)
    gaps = ["media_proxy_not_integrated"] if "preview" in include_values else []
    return {
        "assets": projected,
        "items": projected,
        "total": total,
        "total_known": True,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": next_offset,
        "next_cursor": next_cursor,
        "revision": raw["revision"],
        "page": {
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "collection_revision": raw["revision"],
        },
        "applied_filters": applied_filters,
        "facets": _structured_asset_facets(items, raw) if "facets" in include_values else None,
        "data_status": "degraded" if gaps else "complete",
        "data_gaps": gaps,
    }


def _record_owner(record: Dict[str, Any]) -> Optional[str]:
    """读取资产可证明的归属键；只有工作流桥接写入的 workflow_owner 算归属。

    与 gw/core/asset_visibility.py 同口径：先取顶层 workflow_owner，缺失时再取
    metadata.workflow_owner；metadata 非字典（含 None）时忽略，不视为有主。
    """
    metadata = record.get("metadata")
    owner = record.get("workflow_owner") or (metadata.get("workflow_owner") if isinstance(metadata, dict) else None)
    return str(owner) if owner else None


def asset_is_unassigned(record: Dict[str, Any]) -> bool:
    """未归属 = 没有 project_id 投影，也没有任何 project_ids 关联。"""
    if record.get("project_id"):
        return False
    project_ids = record.get("project_ids")
    return not (isinstance(project_ids, list) and project_ids)


SOURCE_UNKNOWN_LABEL = "来源未知"


def _annotate_inbox_asset(item: Dict[str, Any]) -> Dict[str, Any]:
    """收件箱视图字段：无主资产标记来源未知，有主资产显式为否。

    asset_version 复用资产自身的 version 计数（保留原 version 字段），
    是移动接口 expected_asset_version 的比对基准。
    """
    unknown = _record_owner(item) is None
    item["source_unknown"] = unknown
    item["origin_label"] = SOURCE_UNKNOWN_LABEL if unknown else None
    item["asset_version"] = int(item.get("version", 1))
    return item


def list_unassigned_assets(owner_key: str, limit: int = 100, offset: int = 0, *,
                           role: str = "") -> Dict[str, Any]:
    """未归属收件箱：返回可证明属于该主体、且未归属任何项目的资产。

    无 workflow_owner 的登记资产只进入本地实例管理员（role == 'admin'）的收件箱，
    标记 source_unknown=true / origin_label='来源未知'；其他账户永远看不到无主资产。
    有主资产只在所有者自己的收件箱出现（source_unknown=false / origin_label=null）。
    每项附带 asset_version（资产自身 version，移动接口 expected_asset_version 的比对基准）；
    顶层 revision 仍为注册表库级版本。
    本接口只读取，绝不自动归属或猜测项目。
    """
    admin_inbox = role == "admin"
    raw = state().read()

    def inbox_member(record: Dict[str, Any]) -> bool:
        owner = _record_owner(record)
        return owner == owner_key if owner is not None else admin_inbox

    removed_ids = _removed_root_ids()
    items = [_annotate_inbox_asset(dict(a)) for a in raw["assets"].values()
             if _visible(a) and not _is_removed_asset(a, removed_ids) and asset_is_unassigned(a) and inbox_member(a)]
    items.sort(key=lambda a: a["asset_id"])
    total = len(items)
    page = items[offset:offset + limit]
    page = annotate_asset_provenance(page, raw=raw)
    relation_index = _relation_target_index(raw)
    page = [
        {**asset, "related_assets": _related_assets_for(raw, asset, relation_index)}
        for asset in page
    ]
    return {"assets": page, "items": page, "total": total, "total_known": True,
            "limit": limit, "offset": offset, "has_more": offset + len(page) < total,
            "revision": raw["revision"], "data_status": DATA_STATUS_OK, "data_gaps": []}


def move_asset_to_project(asset_id: str, project_id: str, expected_asset_version: int,
                          owner_key: str, *, role: str = "") -> Dict[str, Any]:
    """未归属资产移入项目：资产所有者可执行；无主资产仅本地实例管理员（role == 'admin'）
    可执行，且在同一次 CAS 写入内把 metadata.workflow_owner 回填为管理员归属键。
    CAS 按资产自身版本（item 的 version，即收件箱 asset_version）比对，而非库级 revision：
    其他资产变动推高库 revision 不影响本资产的移动；比对先于任何变更，
    冲突 409 即整笔不落盘、不回填；成功后资产 version 与库 revision 各自 +1；
    已归属资产拒绝改归属；非管理员对无主资产与不存在同形 404。
    """
    if type(expected_asset_version) is not int or expected_asset_version < 1:
        raise CleanroomException(400, "INVALID_REQUEST", "必须提供正整数expected_asset_version")
    clean_project = str(project_id or "").strip()
    if not clean_project:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少 project_id")
    admin_may_claim = role == "admin"

    def mutate(raw: Dict[str, Any]) -> Any:
        item = _require(raw, asset_id)
        owner = _record_owner(item)
        if owner != owner_key and not (owner is None and admin_may_claim):
            raise CleanroomException(404, "ASSET_NOT_FOUND", "素材 %s 不存在" % asset_id)
        if not asset_is_unassigned(item):
            raise CleanroomException(409, "ASSET_ALREADY_ASSIGNED",
                                     "素材已归属项目，未归属收件箱接口不能更改归属")
        # 按资产 CAS：先于任何变更比对，owner 回填与项目归属才能在同一版本快照内原子生效。
        current_asset_version = int(item.get("version", 1))
        if current_asset_version != expected_asset_version:
            raise CleanroomException(
                409, "VERSION_CONFLICT", "资产版本冲突，请重新读取后重试",
                extra={"expected_version": expected_asset_version,
                       "current_version": current_asset_version, "asset_id": asset_id},
                expose_extra_fields={"expected_version", "current_version", "asset_id"},
            )
        if owner is None:
            # 无主资产：回填与项目归属在同一次 CAS 写入内原子生效。
            # metadata 可能缺失、为 None 或非 dict（旧数据形态），先归一成 dict 再回填。
            if not isinstance(item.get("metadata"), dict):
                item["metadata"] = {}
            item["metadata"]["workflow_owner"] = owner_key
        item["project_id"] = clean_project
        item.setdefault("project_ids", [])
        if clean_project not in item["project_ids"]:
            item["project_ids"].append(clean_project)
        item["version"] = int(item.get("version", 1)) + 1
        item["updated_at"] = storage.now_iso()
        revision = _bump(raw)
        return {"asset": _annotate_inbox_asset(dict(item)), "asset_id": asset_id,
                "project_id": clean_project,
                "moved": True, "revision": revision,
                "data_status": DATA_STATUS_OK, "data_gaps": []}
    return state().mutate(mutate)


def project_asset_counts() -> Dict[str, int]:
    """统计各可见项目关联的资产总数。"""
    raw = state().read()
    from gw.core.asset_visibility import asset_visible
    removed_ids = _removed_root_ids()
    counts: Counter[str] = Counter()
    for a in raw["assets"].values():
        if not asset_visible(a) or _is_removed_asset(a, removed_ids):
            continue
        pids = set(a.get("project_ids") or [])
        if a.get("project_id"):
            pids.add(a.get("project_id"))
        for pid in pids:
            if pid:
                counts[pid] += 1
    return dict(counts)


def facets() -> Dict[str, Any]:
    raw = state().read()
    from gw.core.asset_visibility import asset_visible
    removed_ids = _removed_root_ids()
    assets = [
        a for a in raw["assets"].values()
        if asset_visible(a) and not _is_removed_asset(a, removed_ids)
    ]
    kinds = Counter(_asset_category(asset) for asset in assets)
    categories = Counter(_asset_category(asset) for asset in assets)
    tag_counts = Counter(tag for asset in assets for tag in set(asset.get("tags", [])))
    tags = [{**value, "count": tag_counts.get(value["tag_id"], 0)}
            for value in sorted(raw["tags"].values(), key=lambda item: item["tag_id"])]
    from gw.asset_registry import index_jobs
    return {"kinds": [{"name": name, "count": count} for name, count in sorted(kinds.items())],
            "categories": [{"name": name, "count": count} for name, count in sorted(categories.items())],
            "tags": tags, "collections": [], "roots": index_jobs.root_facets(), "revision": raw["revision"],
            "data_status": DATA_STATUS_OK, "data_gaps": []}


def preview_asset_ids() -> List[str]:
    """全库可预热素材 id：与 ``facets()`` 同一口径（可见 + 未移除 + 未归档）。

    QIN-525：预热范围覆盖全部素材类型，**不按顶层 ``kind`` 过滤**——索引素材的
    ``kind`` 是 ``indexed``，按 ``image`` / ``video`` 白名单过滤会让整个任务空跑
    （QIN-519 的实测缺陷：界面显示「新增 0 · 跳过 0 · 失败 0 · 耗时 0 秒」）。
    类型判定统一交给渲染层按扩展名分派：位图与视频走真实预览，音频画包络，
    字体渲染样张，文档 / Office / 3D / 未知后缀绘制类型占位图。
    """
    raw = state().read()
    from gw.core.asset_visibility import asset_visible

    removed_ids = _removed_root_ids()
    asset_ids: List[str] = []
    for asset_id, record in sorted(raw["assets"].items()):
        if record.get("archived"):
            continue
        if not asset_visible(record) or _is_removed_asset(record, removed_ids):
            continue
        asset_ids.append(str(asset_id))
    return asset_ids


def folders(root_id: Optional[str]) -> Dict[str, Any]:
    raw = state().read()
    items = [dict(f) for f in sorted(raw["folders"].values(), key=lambda x: x["folder_id"])]
    if root_id:
        items = [f for f in items if f.get("parent_id") == root_id]
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    removed_ids = _removed_root_ids()
    for asset in raw["assets"].values():
        if not _visible(asset) or _is_removed_asset(asset, removed_ids):
            continue
        source_id = str(asset.get("storage_root_id") or asset.get("root_id") or "")
        if not source_id or (root_id and source_id != root_id):
            continue
        relative = str(asset.get("relative_path") or asset.get("canonical_relpath") or "").replace("\\", "/").strip("/")
        folder = relative.rpartition("/")[0]
        if folder:
            summary = indexed.setdefault((source_id, folder), {"count": 0, "categories": {}})
            summary["count"] += 1
            category = _asset_category(asset)
            summary["categories"][category] = summary["categories"].get(category, 0) + 1
    indexed_folders = [{"root_id": source, "path": path, **summary}
                       for (source, path), summary in sorted(indexed.items())]
    return {"folders": items, "indexed_folders": indexed_folders,
            "root_id": root_id, "revision": raw["revision"],
            "data_status": DATA_STATUS_OK, "data_gaps": []}


def create_folder(name: str, parent_id: Optional[str], expected_version: Optional[int]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        clean = (name or "").strip()
        if not clean:
            raise CleanroomException(400, "INVALID_REQUEST", "文件夹名称不能为空")
        folder_id = _seq(raw, "fld_", raw["folders"].keys())
        record = {"folder_id": folder_id, "name": clean, "parent_id": parent_id,
                  "created_at": storage.now_iso()}
        raw["folders"][folder_id] = record
        revision = _bump(raw)
        return {"folder": record, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)

# ---------------------------------------------------------------------------
# 关系与标签
# ---------------------------------------------------------------------------

def create_relation(from_asset_id: str, to_asset_id: str, kind: str, expected_version: Optional[int]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        _require(raw, from_asset_id)
        _require(raw, to_asset_id)
        _cas(raw, expected_version)
        relation_id = _seq(raw, "rel_", [r["relation_id"] for r in raw["relations"]])
        relation = {"relation_id": relation_id, "from_asset_id": from_asset_id,
                    "to_asset_id": to_asset_id, "kind": kind or "related",
                    "created_at": storage.now_iso()}
        raw["relations"].append(relation)
        raw["assets"][from_asset_id].setdefault("relations", []).append(relation_id)
        revision = _bump(raw)
        return {"relation": relation, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def delete_relation(asset_id: str, related_asset_id: str) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        _require(raw, asset_id)
        _require(raw, related_asset_id)
        before = len(raw["relations"])
        raw["relations"] = [r for r in raw["relations"]
                            if not (r["from_asset_id"] == asset_id and r["to_asset_id"] == related_asset_id)]
        removed = before - len(raw["relations"])
        if removed == 0:
            raise CleanroomException(404, "RELATION_NOT_FOUND", "关系不存在")
        item = raw["assets"].get(asset_id)
        if item is not None:
            remaining = {r["relation_id"] for r in raw["relations"]}
            item["relations"] = [rid for rid in item.get("relations", []) if rid in remaining]
        revision = _bump(raw)
        return {"removed": removed, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def add_tags(asset_ids: List[str], names: List[str], expected_version: Optional[int]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        for asset_id in asset_ids:
            if asset_id in raw['assets']:
                _require(raw, asset_id)
        _cas(raw, expected_version)
        added = []
        for name in names:
            clean = str(name).strip()
            if not clean:
                continue
            tag_id = None
            for existing_id, tag in raw["tags"].items():
                if tag["name"].casefold() == clean.casefold():
                    tag_id = existing_id
                    break
            if tag_id is None:
                tag_id = _seq(raw, "tag_", raw["tags"].keys())
                raw["tags"][tag_id] = {"tag_id": tag_id, "name": clean, "created_at": storage.now_iso()}
            for asset_id in asset_ids:
                item = raw["assets"].get(asset_id)
                if item is not None and tag_id not in item.setdefault("tags", []):
                    item["tags"].append(tag_id)
            added.append({"tag_id": tag_id, "name": clean})
        revision = _bump(raw)
        return {"tags": added, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def delete_tag(asset_id: str, tag_id: str) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        item = _require(raw, asset_id)
        tags = item.setdefault("tags", [])
        if tag_id not in tags:
            raise CleanroomException(404, "TAG_NOT_FOUND", "资产未绑定该标签")
        tags.remove(tag_id)
        revision = _bump(raw)
        return {"asset_id": asset_id, "tag_id": tag_id, "removed": True,
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def resolve_reference(reference: str) -> Dict[str, Any]:
    """真实解析已存在的 asset_id 引用；未登记即 404。"""
    item = state().read()["assets"].get(reference)
    if item is None:
        raise CleanroomException(404, "ASSET_NOT_FOUND", "引用 %s 未登记" % reference)
    return {"reference": reference, "resolved": True, "asset": dict(item), "data_status": DATA_STATUS_OK}


# ---------------------------------------------------------------------------
# 图片版本
# ---------------------------------------------------------------------------

def create_image_version(asset_id: str, source_path: str, expected_version: Optional[int],
                         edit: Optional[Dict[str, Any]] = None,
                         canvas_id: Optional[str] = None,
                         root_id: Optional[str] = None) -> Dict[str, Any]:
    """创建图片版本；edit 为前端序列化的非破坏性编辑参数（原样落盘，不做推测）。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        _require(raw, asset_id)
        _cas(raw, expected_version)
        display = None
        size = None
        location_root_id = None
        canonical_relpath = None
        if source_path:
            path = storage.resolve_within_roots(source_path, root_id=root_id)
            if not path.is_file():
                raise CleanroomException(404, "FILE_NOT_FOUND", "源图片不存在")
            display = storage.relative_display(path)
            size = path.stat().st_size
            location_root_id, canonical_relpath = storage.location_identity(path)
            if root_id and location_root_id != root_id:
                raise CleanroomException(409, "ROOT_ID_MISMATCH", "素材路径与来源根身份不匹配")
        version_id = _seq(raw, "imgv_", raw["image_versions"].get(asset_id, {}).keys())
        existing = len(raw["image_versions"].setdefault(asset_id, {}))
        record = {"id": version_id, "version_id": version_id, "asset_id": asset_id,
                  "version_number": existing + 1, "is_hidden": False, "hidden": False,
                  "edit": dict(edit) if isinstance(edit, dict) else None,
                  "canvas_id": canvas_id or None, "display_path": display,
                  "root_id": location_root_id, "canonical_relpath": canonical_relpath,
                  "size_bytes": size, "created_at": storage.now_iso(), "label": None}
        raw["image_versions"].setdefault(asset_id, {})[version_id] = record
        revision = _bump(raw)
        return {"version": dict(record), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def update_image_version(asset_id: str, version_id: str, label: Optional[str] = None,
                         expected_version: Optional[int] = None,
                         hidden: Optional[bool] = None) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        record = raw["image_versions"].get(asset_id, {}).get(version_id)
        if record is None:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "图片版本不存在")
        if label is not None:
            record["label"] = str(label).strip() or None
        if hidden is not None:
            record["hidden"] = bool(hidden)
            record["is_hidden"] = bool(hidden)
        record["updated_at"] = storage.now_iso()
        revision = _bump(raw)
        return {"version": dict(record), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def delete_image_version(asset_id: str, version_id: str) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        versions = raw["image_versions"].get(asset_id, {})
        if version_id not in versions:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "图片版本不存在")
        del versions[version_id]
        revision = _bump(raw)
        return {"version_id": version_id, "deleted": True, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def list_image_versions(asset_id: str) -> Dict[str, Any]:
    raw = state().read()
    _require(raw, asset_id)
    items = [dict(v) for v in sorted(raw["image_versions"].get(asset_id, {}).values(),
                                      key=lambda x: x["version_id"])]
    return {"versions": items, "asset_id": asset_id, "revision": raw["revision"],
            "data_status": DATA_STATUS_OK, "data_gaps": []}


def get_image_version_media(asset_id: str, version_id: str) -> Dict[str, Any]:
    record = state().read()["image_versions"].get(asset_id, {}).get(version_id)
    if record is None:
        raise CleanroomException(404, "VERSION_NOT_FOUND", "图片版本不存在")
    if not record.get("display_path") and not record.get("edit"):
        _unavailable("/api/asset-registry/assets/{asset_id}/image-versions/{version_id}/media",
                     "MEDIA_NOT_AVAILABLE", "该版本没有可读取的媒体文件")
    return {"version": dict(record), "data_status": DATA_STATUS_OK}


def get_asset_media(asset_id: str) -> Dict[str, Any]:
    """读取资产媒体描述；无可用媒体时显式 503，不伪造 URL。"""
    raw = state().read()
    _require(raw, asset_id)
    readable = [dict(v) for v in raw["image_versions"].get(asset_id, {}).values() if v.get("display_path")]
    if not readable:
        _unavailable("/api/asset-registry/assets/{asset_id}/media",
                     "MEDIA_NOT_AVAILABLE", "该素材没有可读取的媒体文件")
    return {"asset_id": asset_id, "versions": readable, "data_status": DATA_STATUS_OK}


def derived_files(asset_id: str, *, cache_root: str | None = None) -> Dict[str, Path]:
    """返回已登记派生物的稳定名称到产物路径映射。

    分镜产物位于媒体缓存根内，缓存根由调用方注入；未注入时只列旧版固定根，
    本模块不反向依赖 gw.media（避免领域循环导入）。
    """
    result: Dict[str, Path] = {}
    raw = state().read()
    item = raw["assets"].get(asset_id)
    metadata = item.get("metadata") if item else None
    if isinstance(metadata, dict):
        for name, record in metadata.get("derived_assets", {}).items():
            try:
                root_id = record.get("root_id") or (item or {}).get("root_id")
                path = storage.resolve_within_roots(
                    str(record.get("path") or ""),
                    root_id=str(root_id) if root_id else None,
                )
            except CleanroomException:
                continue
            if path.is_file():
                result[str(name)] = path
    storyboard_dirs = [media_probe.legacy_storyboard_directory(asset_id)]
    if cache_root:
        storyboard_dirs.insert(
            0, media_probe.storyboard_directory(asset_id, cache_root=cache_root)
        )
    for storyboard_dir in storyboard_dirs:
        try:
            if not storyboard_dir.exists():
                continue
            for path in sorted(storyboard_dir.iterdir()):
                if path.is_file() and media_probe.is_storyboard_cache_name(path.name):
                    result[path.name] = path
        except (CleanroomException, OSError):
            continue
    return result


def register_derived_asset(asset_id: str, name: str, raw_path: str) -> Dict[str, Any]:
    """登记已存在的产物文件；文件必须在允许根内且真实存在。"""
    clean_name = "".join(ch for ch in (name or "") if ch.isalnum() or ch in "-_. ").strip()
    if not clean_name or not raw_path:
        raise CleanroomException(400, "INVALID_REQUEST", "缺少派生物名称或路径")
    item_snapshot = _require(state().read(), asset_id)
    previous_root_id = str(item_snapshot.get("root_id") or "")
    path = storage.resolve_within_roots(raw_path, root_id=previous_root_id or None)
    if not path.is_file():
        raise CleanroomException(404, "FILE_NOT_FOUND", "派生物文件不存在")
    root_id, canonical_relpath = storage.location_identity(path)

    def mutate(raw: Dict[str, Any]) -> Any:
        item = _require(raw, asset_id)
        records = item.setdefault("metadata", {}).setdefault("derived_assets", {})
        records[clean_name] = {
            "path": storage.relative_display(path),
            "root_id": root_id,
            "canonical_relpath": canonical_relpath,
            "registered_at": storage.now_iso(),
        }
        revision = _bump(raw)
        return {"asset_id": asset_id, "name": clean_name, "registered": True,
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def record_jxl_lossless_transcodes(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """CAS 记账已通过 SHA256 往返校验的 JPEG XL 派生物。"""
    if not isinstance(items, list):
        raise CleanroomException(400, "INVALID_REQUEST", "转码记录必须是数组")

    def mutate(raw: Dict[str, Any]) -> Dict[str, Any]:
        results = []
        changed = False
        for entry in items:
            asset_id = str(entry.get("asset_id") or "").strip()
            record = raw.get("assets", {}).get(asset_id)
            if record is None or not _visible(record):
                results.append({"asset_id": asset_id, "recorded": False, "error_code": "ASSET_NOT_FOUND"})
                continue
            expected = entry.get("expected_version")
            current = int(record.get("version", 1))
            if type(expected) is not int or expected < 1:
                results.append({"asset_id": asset_id, "recorded": False, "error_code": "INVALID_REQUEST"})
                continue
            if expected != current:
                results.append({"asset_id": asset_id, "recorded": False, "error_code": "VERSION_CONFLICT"})
                continue

            output = str(entry.get("output") or "")
            source_sha256 = str(entry.get("source_sha256") or "")
            verified_sha256 = str(entry.get("verified_sha256") or "")
            if (
                not output.startswith("media_output/jxl-lossless/")
                or not output.lower().endswith(".jxl")
                or len(source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in source_sha256.lower())
                or source_sha256 != verified_sha256
                or entry.get("verified") is not True
            ):
                results.append({"asset_id": asset_id, "recorded": False, "error_code": "LOSSLESS_VERIFICATION_FAILED"})
                continue
            try:
                derived = storage.resolve_output_path(output, must_exist=True)
                output_size = derived.stat().st_size
            except (CleanroomException, OSError):
                results.append({"asset_id": asset_id, "recorded": False, "error_code": "JXL_OUTPUT_NOT_FOUND"})
                continue
            if output_size <= 0:
                results.append({"asset_id": asset_id, "recorded": False, "error_code": "JXL_OUTPUT_INVALID"})
                continue

            metadata = record.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                record["metadata"] = metadata
            metadata["jxl_lossless_transcode"] = {
                "output": output,
                "source_sha256": source_sha256,
                "verified_sha256": verified_sha256,
                "source_size_bytes": int(entry.get("source_size_bytes", 0)),
                "source_mtime_ns": int(entry.get("source_mtime_ns", 0)),
                "output_size_bytes": output_size,
                "saved_bytes": int(entry.get("saved_bytes", 0)),
                "verified": True,
                "verified_at": str(entry.get("verified_at") or storage.now_iso()),
            }
            record["version"] = current + 1
            record["updated_at"] = storage.now_iso()
            changed = True
            results.append({"asset_id": asset_id, "recorded": True})
        if changed:
            _bump(raw)
        return {
            "items": results,
            "revision": int(raw.get("revision", 1)),
            "data_status": DATA_STATUS_OK,
            "data_gaps": [],
        }

    return state().mutate(mutate)


def open_local(asset_id: str) -> Dict[str, Any]:
    """只做路径准入校验，不启动任何外部程序。"""
    item = _require(state().read(), asset_id)
    display = item.get("display_path")
    if not display:
        raise CleanroomException(409, "NO_LOCAL_PATH", "该素材未登记本地路径")
    root_id = str(item.get("root_id") or "")
    path = storage.resolve_within_roots(str(display), root_id=root_id or None)
    return {"asset_id": asset_id, "display_path": storage.relative_display(path),
            "exists": path.exists(), "opened": False,
            "reason": "未启动外部文件管理器；仅完成路径准入校验", "data_status": DATA_STATUS_OK}

# ---------------------------------------------------------------------------
# 预设 / 目录模板 / 项目实体与门 / 项目回收
# ---------------------------------------------------------------------------

def create_preset(name: str, payload: Dict[str, Any], expected_version: Optional[int]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        clean = (name or "").strip()
        if not clean:
            raise CleanroomException(400, "INVALID_REQUEST", "预设名称不能为空")
        preset_id = _seq(raw, "pre_", raw["presets"].keys())
        record = {"preset_id": preset_id, "name": clean, "payload": payload or {},
                  "created_at": storage.now_iso()}
        raw["presets"][preset_id] = record
        revision = _bump(raw)
        return {"preset": record, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def list_presets() -> Dict[str, Any]:
    raw = state().read()
    items = [dict(v) for v in sorted(raw["presets"].values(), key=lambda x: x["preset_id"])]
    return {"presets": items, "revision": raw["revision"],
            "data_status": DATA_STATUS_OK if items else "empty", "data_gaps": []}


def delete_preset(preset_id: str) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        if preset_id not in raw["presets"]:
            raise CleanroomException(404, "PRESET_NOT_FOUND", "预设不存在")
        del raw["presets"][preset_id]
        revision = _bump(raw)
        return {"preset_id": preset_id, "deleted": True, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def _template_fields(payload: Dict[str, Any], prior: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一模板创建/编辑字段，拒绝静默丢字段和无效目录。"""
    allowed = {"name", "project_type", "directory_tree", "folders", "slot_mapping", "expected_version"}
    if set(payload) - allowed:
        raise CleanroomException(400, "INVALID_REQUEST", "目录模板含不支持字段")
    result = dict(prior or {"name": "未命名模板", "project_type": "generic", "folders": [], "slot_mapping": {}})
    for field in ("name", "project_type"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise CleanroomException(400, "INVALID_REQUEST", "模板名称和类型必须是非空短文本")
            result[field] = value.strip()
    if "directory_tree" in payload and "folders" in payload and payload["directory_tree"] != payload["folders"]:
        raise CleanroomException(400, "INVALID_REQUEST", "directory_tree 与 folders 不一致")
    folders = payload.get("directory_tree", payload.get("folders", result.get("folders", [])))
    if not isinstance(folders, list) or len(folders) > 500:
        raise CleanroomException(400, "INVALID_REQUEST", "目录树必须是最多500项的相对路径数组")
    normalized = []
    for folder in folders:
        if (not isinstance(folder, str) or not folder.strip() or len(folder) > 500
                or folder.startswith(("/", "\\")) or ":" in folder
                or ".." in folder.replace("\\", "/").split("/")):
            raise CleanroomException(400, "INVALID_REQUEST", "目录树包含无效相对路径")
        normalized.append(folder.strip().replace("\\", "/"))
    result["folders"] = list(dict.fromkeys(normalized))
    mapping = payload.get("slot_mapping", result.get("slot_mapping", {}))
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                             or value not in result["folders"] for key, value in mapping.items()):
        raise CleanroomException(400, "INVALID_REQUEST", "槽位映射必须指向已声明目录")
    result["slot_mapping"] = dict(mapping)
    return result


def _template_cas(record: Dict[str, Any], expected: Any) -> None:
    current = int(record.get("version", 1))
    if type(expected) is not int or expected < 1:
        raise CleanroomException(400, "INVALID_REQUEST", "目录模板编辑必须提供正整数 expected_version")
    if expected != current:
        raise CleanroomException(409, "VERSION_CONFLICT", "目录模板版本冲突，请重新读取后重试",
                                 extra={"expected_version": expected, "current_version": current},
                                 expose_extra_fields={"expected_version", "current_version"})


def create_directory_template(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = _template_fields(payload)
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, payload.get("expected_version"))
        template_id = _seq(raw, "dtpl_", raw["directory_templates"].keys())
        record = {**fields, "template_id": template_id, "is_default": False, "version": 1,
                  "archived": False, "created_at": storage.now_iso()}
        raw["directory_templates"][template_id] = record
        return {"template": dict(record), "revision": _bump(raw), "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def list_directory_templates(project_type: Optional[str]) -> Dict[str, Any]:
    raw = state().read()
    items = [{**v, "version": int(v.get("version", 1))} for v in raw["directory_templates"].values()]
    if project_type:
        items = [i for i in items if i["project_type"] == project_type]
    items.sort(key=lambda x: x["template_id"])
    return {"templates": items, "project_type": project_type, "revision": raw["revision"],
            "data_status": DATA_STATUS_OK if items else "empty", "data_gaps": []}


def update_directory_template(template_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        record = raw["directory_templates"].get(template_id)
        if record is None:
            raise CleanroomException(404, "TEMPLATE_NOT_FOUND", "目录模板不存在")
        _template_cas(record, payload.get("expected_version"))
        updated = _template_fields(payload, record)
        updated.update(version=int(record.get("version", 1)) + 1, updated_at=storage.now_iso())
        raw["directory_templates"][template_id] = updated
        return {"template": dict(updated), "revision": _bump(raw), "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def archive_directory_template(template_id: str, archived: bool, expected_version: Any) -> Dict[str, Any]:
    if type(archived) is not bool:
        raise CleanroomException(400, "INVALID_REQUEST", "archived 必须是布尔值")
    def mutate(raw: Dict[str, Any]) -> Any:
        record = raw["directory_templates"].get(template_id)
        if record is None:
            raise CleanroomException(404, "TEMPLATE_NOT_FOUND", "目录模板不存在")
        _template_cas(record, expected_version)
        record.update(archived=archived, version=int(record.get("version", 1)) + 1, updated_at=storage.now_iso())
        return {"template": dict(record), "revision": _bump(raw), "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def set_default_directory_template(template_id: str, expected_version: Any) -> Dict[str, Any]:
    """集合内默认唯一；所有被实际修改的记录均递增自身版本。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        record = raw["directory_templates"].get(template_id)
        if record is None:
            raise CleanroomException(404, "TEMPLATE_NOT_FOUND", "目录模板不存在")
        _template_cas(record, expected_version)
        for other in raw["directory_templates"].values():
            target = other["template_id"] == template_id
            if target or bool(other.get("is_default")) != target:
                other.update(is_default=target, version=int(other.get("version", 1)) + 1, updated_at=storage.now_iso())
        return {"template": dict(record), "revision": _bump(raw), "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def create_project_entity(project_id: str, name: str, entity_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, payload.get("expected_version"))
        clean = (name or "").strip()
        if not clean:
            raise CleanroomException(400, "INVALID_REQUEST", "实体名称不能为空")
        entity_id = _seq(raw, "ent_", raw["project_entities"].keys())
        record = {"entity_id": entity_id, "project_id": project_id, "name": clean,
                  "entity_type": entity_type or "generic", "created_at": storage.now_iso()}
        raw["project_entities"][entity_id] = record
        revision = _bump(raw)
        return {"entity": record, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def update_project_entity(entity_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        record = raw["project_entities"].get(entity_id)
        if record is None:
            raise CleanroomException(404, "ENTITY_NOT_FOUND", "项目实体不存在")
        _cas(raw, payload.get("expected_version"))
        for key, value in payload.items():
            if key != "expected_version" and value is not None:
                record[key] = value
        record["updated_at"] = storage.now_iso()
        revision = _bump(raw)
        return {"entity": dict(record), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def create_project_gate(project_id: str, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """拒绝绕过项目创建事务单独造门；初始门只能随HTTP项目创建原子发布。"""
    raise CleanroomException(409, "PROJECT_GATES_REQUIRE_PROJECT_CREATE", "阶段门必须随项目创建请求提交")


def get_project_gate(
    gate_id: str,
    *,
    owner_key: Optional[str],
    role: str,
) -> Dict[str, Any]:
    """委派到项目真源，避免注册表快照暴露未授权门。"""
    from gw.projects_hub.service import default_projects_service
    gate = default_projects_service.get_project_gate(gate_id, owner_key=owner_key, role=role)
    return {"gate": gate, "data_status": DATA_STATUS_OK, "data_gaps": []}


def update_project_gate(
    gate_id: str,
    payload: Dict[str, Any],
    *,
    owner_key: Optional[str] = None,
    role: str = "",
) -> Dict[str, Any]:
    """委派到项目真源，由项目域锁统一执行权限、生命周期与门CAS。"""
    from gw.projects_hub.service import default_projects_service
    gate = default_projects_service.update_project_gate(
        gate_id,
        payload,
        owner_key=owner_key,
        role=role,
    )
    return {"gate": gate, "data_status": DATA_STATUS_OK, "data_gaps": []}

def link_project_assets(project_id: str, asset_ids: List[str], payload: Dict[str, Any]) -> Dict[str, Any]:
    """把已登记资产关联到项目；未登记的 asset_id 一律 404，不静默跳过。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, payload.get("expected_version"))
        linked = []
        for asset_id in asset_ids:
            item = _require(raw, asset_id)
            item.setdefault("project_ids", [])
            if project_id not in item["project_ids"]:
                item["project_ids"].append(project_id)
            linked.append(asset_id)
        for name in payload.get("entity_names") or []:
            entity_id = _seq(raw, "ent_", raw["project_entities"].keys())
            raw["project_entities"][entity_id] = {
                "entity_id": entity_id, "project_id": project_id, "name": str(name),
                "entity_type": "asset_entity", "created_at": storage.now_iso(),
            }
        revision = _bump(raw)
        return {"project_id": project_id, "asset_ids": linked, "linked": len(linked),
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def restore_project_recycle(project_id: str, expected_version: Any) -> Dict[str, Any]:
    """复用项目中心状态机与CAS，不把操作日志冒充状态恢复。"""
    if type(expected_version) is not int or expected_version < 1:
        raise CleanroomException(400, "INVALID_REQUEST", "必须提供正整数expected_version")
    from gw.projects_hub.service import default_projects_service
    result = default_projects_service.restore_from_trash(project_id, expected_version)
    return {"project_id": result.project_id, "restored": True, "version": result.version,
            "project": result.model_dump(mode="json"), "data_status": DATA_STATUS_OK, "data_gaps": []}

# ---------------------------------------------------------------------------
# 治理
# ---------------------------------------------------------------------------

def governance_overview() -> Dict[str, Any]:
    from gw.core.asset_visibility import asset_visible
    raw = state().read()
    return {
        "assets": [dict(a) for a in raw["assets"].values() if a.get("archived") and asset_visible(a)],
        "projects": [], "canvases": [],
        "audit_receipts": audit_sink.overview(),
        "outbox": {"pending": len([o for o in raw["outbox"].values() if o.get("state") == "pending"]),
                   "failed": len([o for o in raw["outbox"].values() if o.get("state") == "failed"]),
                   "remaining": len([o for o in raw["outbox"].values() if o.get("state") in {"pending", "failed"}]),
                   "replayed": len([o for o in raw["outbox"].values() if o.get("state") == "replayed"])},
        "revision": raw["revision"], "data_status": DATA_STATUS_OK, "data_gaps": [],
    }


def cascade_preview(asset_id: str) -> Dict[str, Any]:
    """真实计算级联影响：引用该资产的项目计数。"""
    item = _require(state().read(), asset_id)
    projects = item.get("project_ids", [])
    return {"asset_id": asset_id, "projects": projects, "canvases": [],
            "counts": {"projects": len(projects), "canvases": 0}, "data_status": DATA_STATUS_OK}


def restore_asset_trash(entry_id: str, context=None, expected_version: Optional[int] = None) -> Dict[str, Any]:
    return restore_from_recycle(entry_id, context, expected_version)


def restore_canvas(canvas_id: str) -> Dict[str, Any]:
    """画布域恢复：属画布闭环，本块记录真实操作条目，不伪造画布状态。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        raw["operations"].append({"operation": "canvas_restore", "canvas_id": canvas_id,
                                  "at": storage.now_iso(), "result": "recorded"})
        revision = _bump(raw)
        return {"canvas_id": canvas_id, "restored": True, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def purge_expired_canvases(retention_days: int) -> Dict[str, Any]:
    """真实统计本块已记录的画布操作条目（本块不持有画布数据，故只报告候选）。"""
    raw = state().read()
    candidates = [op for op in raw["operations"] if op.get("operation") == "canvas_restore"]
    return {"candidates": candidates, "count": len(candidates), "retention_days": retention_days,
            "purged": 0, "data_status": DATA_STATUS_OK}


def reconcile_audit_outbox() -> Dict[str, Any]:
    """投递到本地持久收据sink；实际读回ACK后才标记replayed。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        replayed, failed = [], []
        # 持久轮转游标避免前100个坏事件饿死后续事件；每批最多100条。
        # 游标与源状态同事务提交，ACK后提交失败可安全重试原批次。
        entries = list(raw["outbox"].items())
        cursor = raw.get("outbox_cursor")
        position = next((index + 1 for index, (key, _) in enumerate(entries) if key == cursor), 0)
        ordered = entries[position:] + entries[:position]
        candidates = [(key, entry) for key, entry in ordered
                      if entry.get("state") in {"pending", "failed"}][:100]
        for key, entry in candidates:
            raw["outbox_cursor"] = key
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            try:
                event = entry.get("event")
                if not isinstance(event, dict) or event.get("event_id") != key:
                    raise CleanroomException(409, "AUDIT_EVENT_INVALID", "审计事件无效")
                ack = audit_sink.deliver(event, entry.get("digest"))
            except (CleanroomException, OSError, ValueError, TypeError, KeyError):
                entry["state"] = "failed"
                entry["error_code"] = "AUDIT_DELIVERY_UNCONFIRMED"
                failed.append(key)
                continue
            entry["state"] = "replayed"
            entry["replayed_at"] = storage.now_iso()
            entry["receipt_digest"] = ack["digest"]
            entry.pop("error_code", None)
            replayed.append(key)
        revision = _bump(raw) if candidates else raw["revision"]
        remaining = sum(item.get("state") in {"pending", "failed"} for item in raw["outbox"].values())
        return {"replayed": replayed, "count": len(replayed), "failed": failed,
                "remaining": remaining, "revision": revision, "scope": "local_persistent_receipt",
                "data_status": "partial" if failed else DATA_STATUS_OK}
    return state().mutate(mutate)


def record_operation(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        record = {"operation": name, "payload": payload or {}, "at": storage.now_iso()}
        raw["operations"].append(record)
        revision = _bump(raw)
        return {"operation": record, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


# ---------------------------------------------------------------------------
# 索引（真实遍历允许根目录）
# ---------------------------------------------------------------------------

_INDEX_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {"god-cache", "god-recycler", ".gods-workbench"}
)


def _index_scan_path_key(path: Path) -> str:
    return storage.directory_path_key(str(Path(path)))


def _index_scan_path_is_within(path: Path, parent: Path) -> bool:
    path_key = _index_scan_path_key(path)
    parent_key = _index_scan_path_key(parent)
    if path_key == parent_key:
        return True
    separator = os.sep
    parent_prefix = parent_key.rstrip("\\/")
    if parent_prefix:
        parent_prefix += separator
    else:
        parent_prefix = separator
    return path_key.startswith(parent_prefix)


def _index_scan_directory_name_is_excluded(name: str) -> bool:
    normalized = str(name or "").casefold()
    return normalized.startswith(".") or normalized in _INDEX_EXCLUDED_DIRECTORY_NAMES


def _index_scan_file_name_is_excluded(name: str) -> bool:
    return str(name or "").startswith(".")


def _index_scan_runtime_data_root() -> Path | None:
    """按当前运行态取得数据根，避免扫描热路径重复解析整组三根路径。"""
    mode = str(os.environ.get("GW_RUNTIME_MODE") or "dev").strip().casefold()
    if mode == "test":
        raw_data_root = os.environ.get("GW_DATA_DIR", "").strip()
        if not raw_data_root:
            return None
        return Path(storage.normalize_directory_path(raw_data_root))
    if mode == "prod":
        localappdata = os.environ.get("LOCALAPPDATA", "").strip()
        if not localappdata:
            return None
        return Path(storage.normalize_directory_path(localappdata)) / "GodsWorkbench" / "data"
    if mode != "dev":
        return None
    raw_data_root = os.environ.get("GW_DATA_DIR", "").strip()
    if raw_data_root:
        return Path(storage.normalize_directory_path(raw_data_root))
    return Path(__file__).parents[2] / ".local" / "runtime-dev" / "data"


def _index_scan_read_state(
    data_root: Path, namespace: str, default: dict[str, Any]
) -> dict[str, Any]:
    """在命名空间锁内读取快照，避免 JsonState 每次重新解析运行路径。"""
    try:
        with storage.namespace_lock(namespace):
            with (data_root / f"{namespace}.json").open(encoding="utf-8") as handle:
                value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _index_scan_exclusion_configuration() -> tuple[list[Path], Path | None]:
    """每次扫描读取当前缓存与回收根，避免配置变更后仍沿用旧排除表。"""
    from gw.asset_registry import lifecycle

    data_root = _index_scan_runtime_data_root()
    if data_root is None:
        return [], None
    thumbnail_state = _index_scan_read_state(data_root, "media_settings", {})
    thumbnail_settings = dict(thumbnail_state.get("thumbnails") or {})
    cache_folder = str(thumbnail_settings.get("cache_folder") or "god-cache").strip()
    if not cache_folder or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cache_folder
    ):
        cache_folder = "god-cache"
    custom_cache_base = None
    if str(thumbnail_settings.get("mode") or "source") == "custom":
        raw_cache_base = str(thumbnail_settings.get("directory") or "").strip()
        if raw_cache_base:
            normalized_cache_base = storage.normalize_directory_path(raw_cache_base)
            custom_cache_base = Path(normalized_cache_base)
        cache_root = (
            custom_cache_base / cache_folder
            if custom_cache_base is not None
            else data_root / "media_output" / cache_folder
        )
    else:
        cache_root = data_root / "media_output" / cache_folder

    exclusion_roots = [Path(cache_root)]
    storage_state = _index_scan_read_state(data_root, "storage_settings", {})
    dirs = dict(storage_state.get("dirs") or {})
    raw_trash_root = str(dirs.get("trash") or "").strip()
    if raw_trash_root:
        trash_root = Path(storage.normalize_directory_path(raw_trash_root))
        exclusion_roots.append(trash_root / "god-recycler")

    # 当前回收实现仍把文件写入受管 registry_uploads 根；同时读取可配置根，
    # 让目录设置更新后立即影响下一轮扫描。
    exclusion_roots.append(
        data_root / Path(lifecycle._RECYCLER_RELATIVE)
    )

    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in exclusion_roots:
        resolved = Path(storage.normalize_directory_path(str(root)))
        key = _index_scan_path_key(resolved)
        if key and key not in seen:
            seen.add(key)
            unique_roots.append(resolved)
    return unique_roots, custom_cache_base


def _index_scan_exclusions_for_scope(
    scope_root: Path,
    configured_roots: list[Path],
    custom_cache_base: Path | None,
) -> list[Path]:
    exclusions = list(configured_roots)
    if custom_cache_base is not None:
        cache_base = Path(custom_cache_base)
        resolved_scope = Path(scope_root)
        if (
            _index_scan_path_is_within(cache_base, resolved_scope)
            and not _index_scan_path_is_within(resolved_scope, cache_base)
        ):
            exclusions.append(cache_base)
    return exclusions


def _index_scan_directory_is_excluded(
    directory: Path, exclusion_roots: list[Path]
) -> bool:
    if _index_scan_directory_name_is_excluded(directory.name):
        return True
    return any(
        _index_scan_path_is_within(directory, root) for root in exclusion_roots
    )


def _index_scan_scope_is_excluded(
    scan_root: Path, prefix: str, exclusion_roots: list[Path]
) -> bool:
    if _index_scan_directory_name_is_excluded(scan_root.name):
        return True
    if any(
        _index_scan_directory_name_is_excluded(part)
        for part in Path(prefix or ".").parts
        if part not in {"", "."}
    ):
        return True
    return any(
        _index_scan_path_is_within(scan_root, root) for root in exclusion_roots
    )


def _scan_allowed_roots() -> Tuple[List[Dict[str, Any]], List[str]]:
    roots = storage.allowed_roots()
    if not roots:
        return [], ["allowed_roots_not_configured"]
    found: List[Dict[str, Any]] = []
    configured_exclusion_roots, custom_cache_base = (
        _index_scan_exclusion_configuration()
    )

    def display_path(path: Path) -> str:
        matches = []
        for allowed_root in roots:
            try:
                matches.append((allowed_root, path.relative_to(allowed_root)))
            except ValueError:
                continue
        if not matches:
            return path.name
        _, relative = max(matches, key=lambda item: len(item[0].parts))
        return relative.as_posix()

    for root in roots:
        exclusion_roots = _index_scan_exclusions_for_scope(
            root, configured_exclusion_roots, custom_cache_base
        )
        if _index_scan_scope_is_excluded(root, "", exclusion_roots):
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                child_path = Path(entry.path)
                                if _index_scan_directory_is_excluded(
                                    child_path, exclusion_roots
                                ):
                                    continue
                                if os.name == "nt" and storage._is_reparse_point_info(
                                    entry.stat(follow_symlinks=False)
                                ):
                                    try:
                                        storage.resolve_within_roots(str(child_path))
                                    except CleanroomException:
                                        continue
                                pending.append(child_path)
                                continue
                            if _index_scan_file_name_is_excluded(entry.name):
                                continue
                            if not entry.is_file():
                                continue
                            info = entry.stat()
                            path = Path(entry.path)
                            found.append({
                                "display_path": (
                                    storage.relative_display(path)
                                    if entry.is_symlink()
                                    else display_path(path)
                                ),
                                "name": entry.name,
                                "size_bytes": info.st_size,
                            })
                        except OSError:
                            continue
            except OSError:
                continue
    return found, []


def reindex(expected_version: Optional[int]) -> Dict[str, Any]:
    """真实遍历允许根目录并登记新发现的资产；未配置根目录返回 503。"""
    files, gaps = _scan_allowed_roots()
    if gaps:
        _unavailable("/api/asset-registry/reindex", "INDEX_SOURCE_NOT_AVAILABLE",
                     "未配置允许根目录，无法建立索引")
    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        known = {
            a.get("display_path"): a
            for a in raw["assets"].values()
            if a.get("display_path")
        }
        added = []
        search_documents: Dict[str, Dict[str, Any]] = {}
        next_asset_number = _asset_sequence_start(raw)
        for item in files:
            existing = known.get(item["display_path"])
            if existing is not None:
                asset_id = str(existing.get("asset_id") or existing.get("id") or "")
                if asset_id:
                    search_documents[asset_id] = existing
                continue
            asset_id = f"ast_{next_asset_number:04d}"
            next_asset_number += 1
            record = _plain_asset(asset_id, item["name"], "indexed",
                                  size_bytes=item["size_bytes"], display_path=item["display_path"])
            raw["assets"][asset_id] = record
            known[item["display_path"]] = record
            search_documents[asset_id] = record
            added.append(dict(record))
        _upsert_asset_search_documents(search_documents.values())
        revision = _bump(raw)
        return {"indexed": len(added), "assets": added, "scanned": len(files),
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def index_sync() -> Dict[str, Any]:
    """真实统计索引与磁盘差集，不删除任何文件。"""
    files, gaps = _scan_allowed_roots()
    if gaps:
        _unavailable("/api/asset-registry/index/sync", "INDEX_SOURCE_NOT_AVAILABLE",
                     "未配置允许根目录，无法同步索引")
    raw = state().read()
    known = {a.get("display_path") for a in raw["assets"].values() if a.get("display_path")}
    on_disk = {item["display_path"] for item in files}
    return {"on_disk": len(on_disk), "registered": len(known),
            "missing_from_registry": sorted(on_disk - known),
            "missing_from_disk": sorted(known - on_disk),
            "revision": raw["revision"], "data_status": DATA_STATUS_OK}


def rebuild_search_index() -> Dict[str, Any]:
    """从 JSON 注册表真源完整重建本地检索索引（MVP1-AC10 重建钩子）。

    只读注册表，不改写、不删除任何资产或源文件；索引固定落在三态 data 根
    （MVP1-AC1），先写 .part 临时文件再原子替换，失败清理临时文件并保留旧索引。
    重建内容：资产文档、镜头级索引行与索引元数据；元数据如实记录当前无嵌入
    模型（model_id/model_version 为空），维度与向量存储量化格式取自引擎实况。
    """
    raw = state().read()
    assets = [a for a in raw["assets"].values() if isinstance(a, dict) and _visible(a)]
    documents = []
    for asset in assets:
        document = _asset_search_document(asset)
        if document is not None:
            documents.append(document)
    shot_rows: List[Dict[str, Any]] = []
    skipped_segments = 0
    for asset in assets:
        rows, skipped = _asset_shot_rows(asset)
        shot_rows.extend(rows)
        skipped_segments += skipped

    from gw.core.sqlite_vec_engine import SQLiteVecEngine

    db_path = storage.data_root() / ASSET_SEARCH_INDEX_FILENAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = db_path.with_name(db_path.stem + ".part" + db_path.suffix)
    if part_path.exists():
        part_path.unlink()
    try:
        with SQLiteVecEngine(part_path) as engine:
            engine.batch_upsert_documents(documents)
            if shot_rows:
                engine.batch_upsert_shots(shot_rows)
            engine.set_index_metadata(
                model_id=None,
                model_version=None,
                dimensions=engine.dimensions,
                quantization="float32" if engine.vector_enabled else None,
            )
        os.replace(part_path, db_path)
    except Exception:
        try:
            part_path.unlink()
        except OSError:
            pass
        raise
    return {
        "documents": len(documents),
        "shots": len(shot_rows),
        "skipped_segments": skipped_segments,
        "path": str(db_path),
        "data_status": DATA_STATUS_OK,
    }


def search_index_status() -> Dict[str, Any]:
    """只读检索索引状态：文件位置、文档/镜头行数与索引元数据。"""
    from gw.core.sqlite_vec_engine import SQLiteVecEngine

    db_path = storage.data_root() / ASSET_SEARCH_INDEX_FILENAME
    if not db_path.is_file():
        return {"path": str(db_path), "exists": False, "documents": 0, "shots": 0,
                "metadata": None, "data_status": DATA_STATUS_OK}
    with SQLiteVecEngine(db_path) as engine:
        metadata = engine.index_metadata()
        return {
            "path": str(db_path),
            "exists": True,
            "documents": engine.document_count(),
            "shots": engine.shot_count(),
            "metadata": None if metadata is None else {
                "model_id": metadata.model_id,
                "model_version": metadata.model_version,
                "dimensions": metadata.dimensions,
                "quantization": metadata.quantization,
            },
            "data_status": DATA_STATUS_OK,
        }


# ---------------------------------------------------------------------------
# 配置：团队偏好 / 功能开关 / 索引自动化
# ---------------------------------------------------------------------------

def team_preferences() -> Dict[str, Any]:
    raw = state().read()
    return {"preferences": dict(raw["team_preferences"]), "revision": raw["revision"],
            "data_status": DATA_STATUS_OK, "data_gaps": []}


def update_team_preferences(payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        prefs = raw["team_preferences"]
        _cas(raw, payload.get("expected_version"))
        for key in ("team_id", "default_library_id", "default_folder_id", "view", "sort"):
            if key in payload and payload[key] is not None:
                prefs[key] = payload[key]
        prefs["version"] = int(prefs.get("version", 1)) + 1
        revision = _bump(raw)
        return {"preferences": dict(prefs), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def update_feature(feature_id: str, enabled: bool) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        if feature_id not in raw["features"]:
            raise CleanroomException(404, "FEATURE_NOT_FOUND", "能力 %s 不存在" % feature_id)
        raw["features"][feature_id] = bool(enabled)
        revision = _bump(raw)
        return {"feature_id": feature_id, "enabled": bool(enabled),
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def update_index_automation(enabled: Optional[bool], interval_minutes: Optional[int],
                            expected_version: Optional[int]) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        cfg = raw["index_automation"]
        _cas(raw, expected_version)
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        if interval_minutes is not None:
            if int(interval_minutes) < 1:
                raise CleanroomException(400, "INVALID_REQUEST", "interval_minutes 必须为正整数")
            cfg["interval_minutes"] = int(interval_minutes)
        cfg["version"] = int(cfg.get("version", 1)) + 1
        revision = _bump(raw)
        return {"index_automation": dict(cfg), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def features_snapshot() -> Dict[str, Any]:
    raw = state().read()
    return {"features": dict(raw["features"]), "index_automation": dict(raw["index_automation"]),
            "revision": raw["revision"], "data_status": DATA_STATUS_OK, "data_gaps": []}


# ---------------------------------------------------------------------------
# 远程素材（真实 HTTP 探测，失败如实降级，不编造）
# ---------------------------------------------------------------------------

SSRF_DENY_PREFIXES = (
    "http://169.254.", "http://10.", "http://192.168.", "http://172.16.", "http://172.17.",
    "http://172.18.", "http://172.19.", "http://172.2", "http://172.30.", "http://172.31.",
    "http://0.", "file:", "ftp:", "gopher:",
)


def _guard_url(url: str) -> str:
    """按解析后的主机与解析地址检查，禁止用字符串前缀冒充回环地址。"""
    from urllib.parse import urlsplit
    from gw.settings.probes import guard_provider_url
    try:
        parsed = urlsplit(str(url or "").strip())
        if parsed.username is not None or parsed.password is not None:
            raise CleanroomException(400, "INVALID_URL", "素材地址不得包含凭据")
        parsed.port
    except ValueError:
        raise CleanroomException(400, "INVALID_URL", "素材地址格式无效") from None
    return guard_provider_url(url)


def create_remote_asset(url: str, name: str, expected_version: Optional[int]) -> Dict[str, Any]:
    """真实 HTTP HEAD 探测远程素材；失败如实记录 degraded，不编造元数据。"""
    import httpx

    target = _guard_url(url)
    content_type = None
    size_bytes = None
    gaps: List[str] = []
    try:
        with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
            response = client.head(target)
        if response.status_code >= 300:
            gaps.append("remote_head_status_%d" % response.status_code)
        else:
            content_type = response.headers.get("content-type")
            length = response.headers.get("content-length")
            size_bytes = int(length) if length and length.isdigit() else None
    except Exception:
        gaps.append("remote_probe_failed")

    def mutate(raw: Dict[str, Any]) -> Any:
        _cas(raw, expected_version)
        asset_id = _seq(raw, "rma_", raw["remote_assets"].keys())
        record = {"asset_id": asset_id, "name": (name or Path(target).name or "远程素材").strip(),
                  "url": target, "content_type": content_type, "size_bytes": size_bytes,
                  "created_at": storage.now_iso(),
                  "data_status": DATA_STATUS_OK if not gaps else "degraded", "data_gaps": gaps}
        raw["remote_assets"][asset_id] = record
        revision = _bump(raw)
        return {"remote_asset": dict(record), "revision": revision,
                "data_status": record["data_status"], "data_gaps": gaps}
    return state().mutate(mutate)


def list_remote_assets() -> Dict[str, Any]:
    raw = state().read()
    items = [dict(v) for v in sorted(raw["remote_assets"].values(), key=lambda x: x["asset_id"])]
    return {"remote_assets": items, "revision": raw["revision"],
            "data_status": DATA_STATUS_OK if items else "empty", "data_gaps": []}


def delete_remote_asset(asset_id: str) -> Dict[str, Any]:
    def mutate(raw: Dict[str, Any]) -> Any:
        if asset_id not in raw["remote_assets"]:
            raise CleanroomException(404, "REMOTE_ASSET_NOT_FOUND", "远程素材不存在")
        del raw["remote_assets"][asset_id]
        revision = _bump(raw)
        return {"asset_id": asset_id, "deleted": True, "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


# ---------------------------------------------------------------------------
# 工作区任务
# ---------------------------------------------------------------------------

WORKSPACE_JOB_STATES = {"cancel": "cancelled", "retry": "queued", "pause": "paused", "resume": "running"}


def operate_workspace_job(job_id: str, action: str) -> Dict[str, Any]:
    """真实切换工作区任务状态；未登记的 job_id 一律 404，不伪造任务。"""
    if action not in WORKSPACE_JOB_STATES:
        raise CleanroomException(400, "INVALID_ACTION", "不支持的操作：%s" % action)
    def mutate(raw: Dict[str, Any]) -> Any:
        job = raw.setdefault("workspace_jobs", {}).get(job_id)
        if job is None:
            raise CleanroomException(404, "WORKSPACE_JOB_NOT_FOUND", "工作区任务不存在")
        job["state"] = WORKSPACE_JOB_STATES[action]
        job["updated_at"] = storage.now_iso()
        revision = _bump(raw)
        return {"job_id": job_id, "action": action, "state": job["state"],
                "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


# ---------------------------------------------------------------------------
# 导出 PDF（纯标准库，合法字节流）
# ---------------------------------------------------------------------------

def export_pdf_bundle(asset_ids: List[str]) -> Tuple[bytes, int]:
    """使用单一快照导出，重复ID去重；未知资产使整请求404，不伪报导出数。"""
    from gw.core.text_pdf import build_text_pdf
    raw = state().read()
    selected = list(dict.fromkeys(asset_ids))
    records = [_require(raw, asset_id) for asset_id in selected]
    lines = ["%s | %s | %s" % (item["asset_id"], item["name"], item.get("kind", "-")) for item in records]
    text = "\n".join(lines) if lines else "（没有可导出的资产）"
    return build_text_pdf(text), len(records)


def export_pdf(asset_ids: List[str]) -> bytes:
    return export_pdf_bundle(asset_ids)[0]

# ---------------------------------------------------------------------------
# Phase 12 路由对齐补充（A2）
# ---------------------------------------------------------------------------

def _resolve_display(display: str, *, root_id: Optional[str] = None) -> Path:
    """把相对展示路径解析回允许根目录内的真实文件；越界或缺失一律失败关闭。"""
    normalized = str(display or "").replace("\\", "/")
    if normalized.startswith("registry_uploads/"):
        return storage.resolve_output_path(normalized)
    roots = storage.allowed_roots()
    if not roots:
        raise CleanroomException(403, "LOCAL_FILE_ACCESS_NOT_ADMITTED",
                                 "本机文件访问未获准入（未配置允许根目录）")
    candidate = Path(display)
    return storage.resolve_within_roots(str(candidate), root_id=root_id)


def _confirmed_alias_media_path(
    storage_root_id: str, root_id: str, canonical_relpath: str
) -> Optional[Path]:
    """仅通过与资产来源及位置身份匹配的已确认离线 alias 回读媒体。"""
    if not storage_root_id or not root_id or not canonical_relpath:
        return None
    try:
        canonical = storage.canonical_relpath(canonical_relpath)
        from gw.asset_registry import index_jobs

        _, aliases = index_jobs._roots()
        alias = aliases.get(storage_root_id)
        if (
            not isinstance(alias, dict)
            or str(alias.get("root_id") or "") != root_id
            or str(alias.get("storage_root_id") or "") != storage_root_id
            or alias.get("scan_path") is None
        ):
            return None
        prefix = str(alias.get("prefix") or "")
        prefix = storage.canonical_relpath(prefix) if prefix else ""
        if prefix:
            if canonical == prefix or not canonical.startswith(prefix + "/"):
                return None
            relative = canonical[len(prefix) + 1 :]
        else:
            relative = canonical
        if not relative:
            return None

        boundary = Path(alias["scan_path"])
        target = boundary.joinpath(*relative.split("/"))
        resolved_boundary = boundary.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        # 与 index_jobs 的扫描与哈希分支一致：映射盘上 resolve() 返回 UNC 而
        # absolute() 保留盘符，形态差本身不代表路径被篡改，故网络路径不按形态判失败。
        # 真实越界仍由下方 is_relative_to 与 is_file 失败关闭。
        network_alias = storage._is_network_path(str(boundary))
    except (CleanroomException, OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        (resolved_boundary != boundary.absolute() and not network_alias)
        or (resolved_target != target.absolute() and not network_alias)
        or not resolved_target.is_relative_to(resolved_boundary)
        or not resolved_target.is_file()
    ):
        return None
    return resolved_target


def _media_file_in_state(raw: dict, asset_id: str, version_id: Optional[str] = None) -> Optional[Path]:
    """在已加载的注册表快照里解析素材文件路径（批量入口复用同一口径）。"""
    root_id = None
    asset_record = raw["assets"].get(asset_id, {})
    if version_id:
        record = raw["image_versions"].get(asset_id, {}).get(version_id)
        if record is None:
            raise CleanroomException(404, "VERSION_NOT_FOUND", "图片版本不存在")
        display = record.get("display_path")
        root_id = record.get("root_id")
        if not root_id:
            root_id = asset_record.get("root_id")
        storage_root_id = record.get("storage_root_id") or asset_record.get("storage_root_id")
        canonical_relpath = record.get("canonical_relpath") or asset_record.get(
            "canonical_relpath"
        )
        if not display and record.get("edit"):
            return _media_file_in_state(raw, asset_id, None)
    else:
        item = _require(raw, asset_id)
        display = item.get("display_path")
        root_id = item.get("root_id")
        storage_root_id = item.get("storage_root_id") or ""
        canonical_relpath = item.get("canonical_relpath") or ""
        if not display:
            versions = sorted(raw["image_versions"].get(asset_id, {}).values(),
                              key=lambda x: x["version_id"], reverse=True)
            version = next((v for v in versions if v.get("display_path")), None)
            display = version.get("display_path") if version else None
            root_id = (version or {}).get("root_id") or root_id
            storage_root_id = (version or {}).get("storage_root_id") or storage_root_id
            canonical_relpath = (version or {}).get("canonical_relpath") or canonical_relpath
    if not display:
        return None
    try:
        path = _resolve_display(str(display), root_id=str(root_id) if root_id else None)
    except CleanroomException as exc:
        if exc.code == "ROOT_ID_REQUIRED":
            raise
        path = None
    if path is not None and path.is_file():
        return path
    return _confirmed_alias_media_path(
        str(storage_root_id or ""),
        str(root_id or ""),
        str(canonical_relpath or ""),
    )


def media_file(asset_id: str, version_id: Optional[str] = None) -> Optional[Path]:
    """返回素材/版本对应的真实本地文件；没有登记或文件缺失时返回 None。"""
    return _media_file_in_state(state().read(), asset_id, version_id)


def media_files(asset_ids: Iterable[str]) -> Dict[str, Path]:
    """批量解析素材文件：只加载一次注册表快照。

    全库预览预热是「几百上千项、每项都要源文件路径」的场景，逐项调
    ``media_file`` 会把 800KB 的 asset_registry.json 反复解析，成为并发出图的
    唯一串行瓶颈。解析口径完全复用 ``_media_file_in_state``，不另写一套。
    """
    raw = state().read()
    resolved: Dict[str, Path] = {}
    for asset_id in asset_ids:
        key = str(asset_id or "").strip()
        if not key:
            continue
        try:
            path = _media_file_in_state(raw, key)
        except CleanroomException:
            continue
        if path is not None:
            resolved[key] = path
    return resolved


def preview_asset_paths() -> Dict[str, Path]:
    """预热范围的素材文件：一次读注册表同时产出枚举与路径。

    全库预热原先要读两遍 800KB 的 ``asset_registry.json``（先枚举 ``preview_asset_ids``
    再批量解析 ``media_files``），合并后省掉一次完整解析；口径与 ``preview_asset_ids``
    完全一致（可见、未归档、非 removed），不另设一套过滤。
    """
    raw = state().read()
    from gw.core.asset_visibility import asset_visible

    removed_ids = _removed_root_ids()
    resolved: Dict[str, Path] = {}
    for asset_id, record in sorted(raw["assets"].items()):
        if record.get("archived"):
            continue
        if not asset_visible(record) or _is_removed_asset(record, removed_ids):
            continue
        try:
            path = _media_file_in_state(raw, str(asset_id))
        except CleanroomException:
            continue
        if path is not None:
            resolved[str(asset_id)] = path
    return resolved


def stored_upload_path(asset_id: str, filename: str) -> Path:
    """导入素材在数据目录内的落盘位置（服务端自生成路径，不暴露给调用方）。"""
    safe = "".join(ch for ch in (filename or "upload") if ch.isalnum() or ch in "-_. ")
    safe = safe.strip() or "upload"
    target_dir = storage.data_root() / "registry_uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / ("%s_%s" % (asset_id, safe))


def related_assets(asset_ids: List[str], kind: str, expected_version: Optional[int]) -> Dict[str, Any]:
    """按前端批量语义建立资产间关系：首个资产分别指向其余资产。"""
    clean = [str(a) for a in asset_ids if str(a).strip()]
    if len(clean) < 2:
        raise CleanroomException(400, "INVALID_REQUEST", "至少需要两个 asset_id")
    anchor, others = clean[0], clean[1:]
    created = []
    for other in others:
        created.append(create_relation(anchor, other, kind or "related", expected_version))
    raw = state().read()
    return {"relations": [r for r in raw["relations"] if r["from_asset_id"] == anchor],
            "count": len(created), "revision": raw["revision"],
            "data_status": DATA_STATUS_OK, "data_gaps": []}


def assets_snapshot(asset_ids: List[str]) -> List[Dict[str, Any]]:
    """回读一批资产；未登记的忽略（调用方负责 404 判定）。"""
    raw = state().read()
    from gw.core.asset_visibility import asset_visible
    return [dict(raw["assets"][a]) for a in asset_ids if a in raw["assets"] and asset_visible(raw['assets'][a])]


def create_workspace_job(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """登记一个真实的工作区任务（由索引/媒体流程产生），不伪造进度。"""
    def mutate(raw: Dict[str, Any]) -> Any:
        job_id = _seq(raw, "job_", raw["workspace_jobs"].keys())
        record = {"job_id": job_id, "name": name or "工作区任务",
                  "state": "queued", "created_at": storage.now_iso(),
                  "updated_at": storage.now_iso(), "payload": payload or {}}
        raw.setdefault("workspace_jobs", {})[job_id] = record
        revision = _bump(raw)
        return {"job": dict(record), "revision": revision, "data_status": DATA_STATUS_OK}
    return state().mutate(mutate)


def get_workspace_job(job_id: str) -> Dict[str, Any]:
    job = state().read().get("workspace_jobs", {}).get(job_id)
    if job is None:
        raise CleanroomException(404, "WORKSPACE_JOB_NOT_FOUND", "工作区任务 %s 不存在" % job_id)
    return {"job": dict(job), "data_status": DATA_STATUS_OK, "data_gaps": []}


def project_entities(project_id: Optional[str] = None) -> Dict[str, Any]:
    raw = state().read()
    items = [dict(v) for v in raw["project_entities"].values()]
    if project_id:
        items = [i for i in items if i.get("project_id") == project_id]
    items.sort(key=lambda x: x["entity_id"])
    return {"entities": items, "revision": raw["revision"], "data_status": DATA_STATUS_OK}


def project_gates(
    project_id: Optional[str] = None,
    *,
    owner_key: Optional[str] = None,
    role: str = "",
) -> Dict[str, Any]:
    """只从项目快照读取门，缺少访问身份时安全返回空集合。"""
    if not project_id:
        return {"gates": [], "data_status": DATA_STATUS_OK, "data_gaps": []}
    from gw.projects_hub.service import default_projects_service
    items = default_projects_service.list_project_gates(project_id, owner_key=owner_key, role=role)
    return {"gates": items, "data_status": DATA_STATUS_OK, "data_gaps": []}


def export_archive(asset_ids: List[str]) -> bytes:
    """把已登记的本地文件真实打包为 zip；无文件可打包时 503，不返回空假包。"""
    entries: List[Tuple[str, Path, str]] = []
    unavailable_asset_ids: List[str] = []
    seen_asset_ids: set[str] = set()
    used_names: set[str] = set()
    for raw_asset_id in asset_ids:
        asset_id = str(raw_asset_id or "").strip()
        if not asset_id or asset_id in seen_asset_ids:
            continue
        seen_asset_ids.add(asset_id)
        path = media_file(asset_id)
        if path is None:
            unavailable_asset_ids.append(asset_id)
            continue
        name = path.name
        arcname = name
        if arcname.casefold() in used_names:
            path_name = Path(name)
            safe_id = "".join(
                char
                for char in asset_id
                if char.isascii() and (char.isalnum() or char in "-_")
            )
            digest = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:10]
            if not safe_id:
                safe_id = "asset"
            if safe_id != asset_id:
                safe_id += "-" + digest
            arcname = "%s--%s%s" % (path_name.stem, safe_id, path_name.suffix)
            if arcname.casefold() in used_names:
                arcname = "%s--%s-%s%s" % (
                    path_name.stem,
                    safe_id,
                    digest,
                    path_name.suffix,
                )
            suffix = 2
            while arcname.casefold() in used_names:
                arcname = "%s--%s-%s-%d%s" % (
                    path_name.stem,
                    safe_id,
                    digest,
                    suffix,
                    path_name.suffix,
                )
                suffix += 1
        used_names.add(arcname.casefold())
        entries.append((arcname, path, asset_id))
    if unavailable_asset_ids:
        first_unavailable = unavailable_asset_ids[0]
        if not entries:
            raise CleanroomException(
                503,
                "NO_LOCAL_FILES",
                "所选素材没有可打包的本地文件（asset_id=%s）" % first_unavailable,
                extra={"asset_id": first_unavailable, "data_status": "partial"},
                expose_extra_fields={"asset_id", "data_status"},
            )
        raise CleanroomException(
            503,
            "ARCHIVE_ITEM_UNAVAILABLE",
            "所选素材缺少可读取的本地文件（asset_id=%s）" % first_unavailable,
            extra={"asset_id": first_unavailable, "data_status": "partial"},
            expose_extra_fields={"asset_id", "data_status"},
        )
    if not entries:
        _unavailable("/api/asset-registry/assets/archive", "NO_LOCAL_FILES",
                     "所选素材没有可打包的本地文件")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, path, asset_id in entries:
            try:
                archive.write(path, arcname=arcname)
            except Exception:
                raise CleanroomException(
                    503,
                    "ARCHIVE_ITEM_UNREADABLE",
                    "所选素材读取失败（asset_id=%s）" % asset_id,
                    extra={"asset_id": asset_id, "data_status": "partial"},
                    expose_extra_fields={"asset_id", "data_status"},
                ) from None
    return buffer.getvalue()


def remove_bridge_asset(asset_id: str, owner: str):
    """桥接清理只作用于已证明归属的记录，保留原始素材和其他主体。"""
    def mutate(raw):
        record = raw.get("assets", {}).get(asset_id)
        if record and record.get("metadata", {}).get("workflow_owner") == owner:
            del raw["assets"][asset_id]
            _bump(raw)
    return state().mutate(mutate)


def register_asset_record(
    asset_id: str,
    name: str,
    kind: str,
    *,
    url: Optional[str] = None,
    task_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """在底层注册表中登记或更新资产，保证 asset_id 稳定绑定。"""
    source_context = (metadata or {}).get("source_context", {})
    source_project_id = source_context.get("project_id") if isinstance(source_context, dict) else None
    has_workflow_owner = bool((metadata or {}).get("workflow_owner"))
    def mutate(raw: Dict[str, Any]) -> Any:
        assets = raw.setdefault("assets", {})
        if asset_id in assets:
            record = assets[asset_id]
            old_owner = record.get("metadata", {}).get("workflow_owner")
            if old_owner != (metadata or {}).get("workflow_owner") and (old_owner or (metadata or {}).get("workflow_owner")):
                raise CleanroomException(409, "ASSET_OWNER_CONFLICT", "资产归属冲突")
            if (not has_workflow_owner or record.get("project_id") == source_project_id) and record.get("name") == name and record.get("kind") == kind and (not url or record.get("url") == url) and (not task_id or record.get("task_id") == task_id) and all(record.get("metadata", {}).get(key) == value for key, value in (metadata or {}).items()):
                return record
            record["name"] = name
            record["kind"] = kind
            record["updated_at"] = storage.now_iso()
            if url:
                record["url"] = url
            if task_id:
                record["task_id"] = task_id
            if metadata:
                record.setdefault("metadata", {}).update(metadata)
        else:
            record = _plain_asset(
                asset_id,
                name,
                kind,
                url=url,
                task_id=task_id,
                metadata=metadata or {},
            )
            assets[asset_id] = record
            raw.setdefault("sequences", {})[asset_id] = True
        # 来源由工作流入口按主体/生命周期校验；项目投影与该不可变来源同步。
        if has_workflow_owner:
            if source_project_id:
                record["project_id"] = source_project_id
            else:
                record.pop("project_id", None)
        _bump(raw)
        return record

    return state().mutate(mutate)


def delete_asset_search_documents(asset_ids: List[str]) -> None:
    """批量删除 SQLite 全文与向量检索索引中的资产与分镜。"""
    if not asset_ids:
        return
    from gw.core.sqlite_vec_engine import SQLiteVecEngine
    db_path = storage.data_root() / ASSET_SEARCH_INDEX_FILENAME
    if not db_path.exists():
        return
    try:
        with SQLiteVecEngine(db_path) as engine:
            for aid in asset_ids:
                engine.delete_document(aid)
                engine.delete_shots(aid)
    except Exception:
        pass


def purge_root_records(root_id: str) -> Dict[str, Any]:
    """彻底清除属于指定允许根的所有注册表资产与检索索引。绝对不触碰用户源文件。"""
    removed_ids: List[str] = []

    def mutate(raw: Dict[str, Any]) -> None:
        nonlocal removed_ids
        for asset_id, asset in list(raw.get("assets", {}).items()):
            aid = str(asset.get("storage_root_id") or asset.get("root_id") or "")
            if aid == root_id:
                removed_ids.append(asset_id)
                del raw["assets"][asset_id]
        if removed_ids:
            _bump(raw)

    state().mutate(mutate)

    if removed_ids:
        delete_asset_search_documents(removed_ids)

    return {"purged_root_id": root_id, "purged_asset_ids": removed_ids, "purged_asset_count": len(removed_ids)}
