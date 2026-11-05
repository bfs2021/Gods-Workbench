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

"""Phase 12 本地素材与存储文件真实接口。

真实数据源：`gw.asset_library.repository`（GW_ALLOWED_ROOTS 锚定 + 单实例 JSON 索引）。
未配置允许根目录时统一失败关闭，不读取请求中的任意路径。
"""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Header, Query, Request, status

from gw.asset_library import repository as repo
from gw.core.auth import require_authenticated, require_edit_access
from gw.core.errors import CleanroomException

router = APIRouter(tags=["local-assets-b4"])


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read(authorization: str | None, x_user_role: str):
    return require_authenticated(authorization, x_user_role)


def _write(authorization: str | None, x_user_role: str):
    return require_edit_access(authorization, x_user_role)


def _root_hint(payload: dict[str, Any], key: str = "root_id") -> str | None:
    """可选的来源根身份；缺省时由仓库按索引记录或单根唯一根解析。"""
    value = payload.get(key)
    text = str(value).strip() if value is not None else ""
    return text or None


def _ids(payload: dict[str, Any], key: str = "paths") -> list[str]:
    value = payload.get(key) or []
    return [str(item) for item in value] if isinstance(value, list) else []


def _local_asset_item(item: dict[str, Any]) -> dict[str, Any]:
    """补齐前端目录树使用的稳定 ID 与相对文件夹字段。"""
    result = dict(item)
    display_path = str(result.get("display_path") or result.get("file") or "")
    normalized_path = display_path.replace("\\", "/")
    parent = PurePosixPath(normalized_path).parent.as_posix()
    result.setdefault("id", str(result.get("asset_id") or normalized_path))
    result.setdefault("file", normalized_path)
    result["folder"] = "" if parent in {"", "."} else parent
    return result


def _all_local_asset_items(kind: str | None) -> list[dict[str, Any]]:
    """读取完整索引用于构建跨分页的目录树。"""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = repo.list_local_assets(kind, limit=500, cursor=cursor)
        items.extend(page.get("items") or [])
        next_cursor = page.get("next_cursor")
        if not page.get("has_more") or not next_cursor or next_cursor == cursor:
            break
        cursor = str(next_cursor)
    return [_local_asset_item(item) for item in items]


def _local_asset_tree(items: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {
        "id": "__root__",
        "path": "",
        "name": "根目录",
        "count": len(items),
        "items": [],
        "children": [],
    }
    nodes = {"": root}
    for item in items:
        parent_path = str(item.get("folder") or "")
        current_parts: list[str] = []
        parent = root
        for part in PurePosixPath(parent_path).parts:
            if part in {"", ".", "/", ".."}:
                continue
            current_parts.append(part)
            path = "/".join(current_parts)
            node = nodes.get(path)
            if node is None:
                node = {
                    "id": path,
                    "path": path,
                    "name": part,
                    "count": 0,
                    "items": [],
                    "children": [],
                }
                nodes[path] = node
                parent["children"].append(node)
            node["count"] += 1
            parent = node
    for node in nodes.values():
        node["children"].sort(key=lambda child: str(child["name"]).casefold())
    return root


@router.get("/api/local-assets", status_code=status.HTTP_200_OK)
def list_local_assets(
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None, max_length=2048),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """列出真实本地素材索引；无条目返回空集合。"""
    _read(authorization, x_user_role)
    result = repo.list_local_assets(kind, limit=limit, cursor=cursor)
    result["items"] = [_local_asset_item(item) for item in result.get("items", [])]
    result["tree"] = _local_asset_tree(_all_local_asset_items(kind))
    return result


@router.post("/api/local-assets/upload", status_code=status.HTTP_201_CREATED)
async def upload_local_assets(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """真实写入允许根目录内的目标文件并登记索引。"""
    _write(authorization, x_user_role)
    if "multipart/form-data" in (request.headers.get("content-type") or "").lower():
        form = await request.form()
        uploads = [item for item in form.getlist("files") if hasattr(item, "read")]
        if not uploads:
            raise CleanroomException(400, "INVALID_REQUEST", "缺少 files 上传字段")
        root_path = str(form.get("path") or "").strip()
        if not root_path:
            raise CleanroomException(400, "INVALID_REQUEST", "缺少素材来源目录")
        raw_folder = str(form.get("folder") or "").strip().replace("\\", "/")
        folder = PurePosixPath(raw_folder)
        if folder.is_absolute() or ".." in folder.parts:
            raise CleanroomException(400, "INVALID_PATH", "上传目录必须是素材来源内的相对路径")
        folder_parts = [part for part in folder.parts if part not in {"", "."}]
        kind = str(form.get("kind") or "auto").strip() or "auto"
        uploaded_paths: list[tuple[str, str]] = []
        for upload in uploads:
            raw_name = str(getattr(upload, "filename", "") or "").replace("\\", "/")
            filename = PurePosixPath(raw_name).name
            if filename in {"", ".", ".."}:
                raise CleanroomException(400, "INVALID_REQUEST", "上传文件名无效")
            content = await upload.read(repo.MAX_UPLOAD_BYTES + 1)
            if len(content) > repo.MAX_UPLOAD_BYTES:
                raise CleanroomException(400, "CONTENT_TOO_LARGE", "上传内容超过上限")
            target = PurePosixPath(root_path, *folder_parts, filename)
            result = repo.upload_and_index_local_asset(
                str(target), base64.b64encode(content).decode("ascii"), kind
            )
            uploaded_paths.append((str(result.get("display_path") or ""), filename))
        all_items = _all_local_asset_items(kind)
        by_path = {str(item.get("display_path") or ""): item for item in all_items}
        files = [
            by_path.get(path, {"name": name, "display_path": path})
            for path, name in uploaded_paths
        ]
        return {"files": files, "count": len(files), "tree": _local_asset_tree(all_items)}
    payload = await _body(request)
    target = str(payload.get("path") or "")
    return repo.upload_and_index_local_asset(
        target,
        str(payload.get("content_base64") or ""),
        str(payload.get("kind") or "auto"),
        root_id=_root_hint(payload),
    )


@router.post("/api/local-assets/delete", status_code=status.HTTP_200_OK)
async def delete_local_assets(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """真实删除允许根目录内的文件。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    return repo.delete_local_files(_ids(payload), root_id=_root_hint(payload))


@router.post("/api/local-assets/move", status_code=status.HTTP_200_OK)
async def move_local_assets(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """在允许根目录内真实移动文件。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    shared = _root_hint(payload)
    return repo.move_local_file(
        str(payload.get("source") or ""),
        str(payload.get("target") or ""),
        source_root_id=_root_hint(payload, "source_root_id") or shared,
        target_root_id=_root_hint(payload, "target_root_id"),
    )


@router.patch("/api/local-assets/items", status_code=status.HTTP_200_OK)
async def update_local_asset(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """更新本地素材索引条目（重命名/改分类）。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    return repo.update_local_entry(str(payload.get("asset_id") or ""), payload)


@router.post("/api/local-assets/folders", status_code=status.HTTP_201_CREATED)
async def create_local_asset_folder(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """在数据目录内创建真实文件夹。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    return repo.create_local_folder(str(payload.get("name") or ""))


@router.patch("/api/local-assets/folders", status_code=status.HTTP_200_OK)
async def update_local_asset_folder(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """重命名数据目录或素材来源内的真实文件夹并返回当前目录树。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    kind = str(payload.get("kind") or "").strip() or None
    result = repo.rename_local_folder(
        str(payload.get("name") or ""),
        str(payload.get("new_name") or ""),
        root_path=str(payload.get("root_path") or "").strip() or None,
    )
    items = _all_local_asset_items(kind)
    result.update(
        {
            "items": items,
            "tree": _local_asset_tree(items),
            "folder": {"path": str(result.get("to") or "")},
        }
    )
    return result


@router.post("/api/local-assets/classify", status_code=status.HTTP_200_OK)
async def classify_local_assets(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """对真实登记条目做确定性分类。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    ids = payload.get("asset_ids") or []
    return repo.classify_local_entries(
        [str(x) for x in ids] if isinstance(ids, list) else []
    )


@router.post("/api/local-assets/caption", status_code=status.HTTP_200_OK)
async def caption_local_assets(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """为真实素材生成确定性说明（基于文件名与元数据，不做语义猜测）。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    ids = payload.get("asset_ids") or []
    return repo.caption_local_entries(
        [str(x) for x in ids] if isinstance(ids, list) else []
    )


@router.patch("/api/local-assets/caption", status_code=status.HTTP_200_OK)
async def save_local_asset_caption(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """保存人工说明文本到真实索引。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    return repo.save_local_caption(
        str(payload.get("asset_id") or ""), str(payload.get("caption") or "")
    )


@router.get("/api/storage-files", status_code=status.HTTP_200_OK)
def list_storage_files(
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """列出数据目录内的真实存储文件与允许根目录配置状态。"""
    _read(authorization, x_user_role)
    return repo.storage_files()


@router.post("/api/storage-files/delete", status_code=status.HTTP_200_OK)
async def delete_storage_files(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """删除数据目录内的真实存储产物。"""
    _write(authorization, x_user_role)
    payload = await _body(request)
    return repo.delete_storage_files(_ids(payload, "names"))
