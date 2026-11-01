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

"""设置页 API 路由实现（Phase 10D）。

严格对齐 ``docs/contracts/SETTINGS-INTERFACE-CATALOG.yaml``（version: p10d-frozen-1）的
8 条归一化路径 / 13 个方法，并承接 C16-T2 的服务端凭据边界：

- ``GET/PATCH  /api/storage-settings``              存储设置读取 / 保存（CAS revision）
- ``GET/PUT    /api/providers``                     平台集合读取 / 整体替换（CAS revision）
- ``POST       /api/providers/fetch-models``        拉取模型（本阶段 fail-closed）
- ``POST       /api/providers/probe-async``         协议探测（本阶段 fail-closed）
- ``POST       /api/providers/test-connection``     连通性验证（本阶段 fail-closed）
- ``GET        /api/providers/credentials``         服务端凭据状态（仅布尔字段）
- ``POST       /api/providers/{provider_id}/gateway/chat`` 单向出口鉴权（客户端零凭据）
- ``GET/POST   /api/asset-registry/asset-structures``        结构集合读取 / 创建
- ``GET/PATCH/DELETE /api/asset-registry/asset-structures/{structure_id}``  单结构读写删
- ``PATCH      /api/asset-registry/asset-structures/{structure_id}/current`` 切换当前

C16 路由只允许稳定 ``provider_id`` 与消息进入网关；Provider 密钥由服务端密文快照
读取并注入上游请求，绝不进入响应、日志或客户端请求体。

边界：其余相邻端点（``/api/asset-registry/assets*``、``/api/local-assets*``、
``/api/storage-files*``、``/api/asset-registry/reindex``、``/api/asset-registry/project-directory*``
等）仍属 KNOWN_UNIMPLEMENTED，未获契约授权，本模块**不得**顺带实现。
"""

import asyncio
from typing import Any, Union

from fastapi import APIRouter, Body, Header, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from gw.core.auth import require_authenticated, require_edit_access
from gw.core.credentials_vault import CredentialVault
from gw.core.errors import CleanroomException, ForbiddenException
from gw.settings import chat as chat_runtime
from gw.settings import native_picker
from gw.settings import probes as provider_probes
from gw.settings.models import (
    AssetStructureCreateRequest,
    AssetStructureCurrentRequest,
    AssetStructureDeleteResponse,
    AssetStructureListResponse,
    AssetStructureResponse,
    AssetStructureUpdateRequest,
    ProviderSnapshot,
    StorageAllowedRootDeleteRequest,
    StorageAllowedRootRequest,
    StorageAllowedRootsSnapshot,
    StorageSettingsPatchRequest,
    StorageSettingsSnapshot,
)
from gw.settings.service import (
    default_asset_structure_service,
    default_provider_service,
    default_storage_allowed_roots_service,
    default_storage_settings_service,
)

router = APIRouter(tags=["settings"])


class ProviderGatewayChatRequest(BaseModel):
    """只允许消息与服务端已登记模型；客户端不得提交地址或凭据。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(..., min_length=1, max_length=200_000)
    model: str | None = Field(None, max_length=256)
    mode: str | None = Field(None, max_length=64)


class NativeDirectoryPickRequest(BaseModel):
    """服务端原生目录选择请求；仅允许标题、初始目录与超时。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(None, max_length=120)
    initial_dir: str | None = Field(None, max_length=4096)
    timeout_seconds: int = Field(60, ge=5, le=300)


class NativeDirectoryPickResponse(BaseModel):
    """原生选择结果：selected / cancelled / unavailable 三态，不编造路径。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    path: str | None = None
    reason: str | None = None


def _is_loopback_request(request: Request) -> bool:
    """仅服务器本机回环会话允许弹出服务端对话框；远程访问一律拒绝。"""
    client = request.client
    host = str(client.host or "").lower() if client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _credential_vault() -> CredentialVault:
    """每次按当前设置服务构造边界，兼容测试态的依赖注入。"""
    return CredentialVault(default_provider_service)


# ---------------------------------------------------------------------------
# 存储设置
# ---------------------------------------------------------------------------


@router.get(
    "/api/storage-settings",
    response_model=StorageSettingsSnapshot,
    summary="读取存储设置",
    status_code=status.HTTP_200_OK,
)
def get_storage_settings(
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """返回存储设置真值；无真实来源时如实标记未配置，不编造根目录/容量/路径。"""
    require_authenticated(authorization, x_user_role)
    return default_storage_settings_service.get_snapshot()


@router.get(
    "/api/storage-settings/browse",
    summary="只读浏览服务器目录",
    status_code=status.HTTP_200_OK,
)
def browse_server_directories(
    path: str | None = Query(None, max_length=4096),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """仅管理员可读取受限目录列表；响应不包含文件，服务层负责路径约束。"""
    context = require_authenticated(authorization, x_user_role)
    if context.role != "admin":
        raise ForbiddenException(message="仅管理员可浏览服务器目录")
    return default_storage_settings_service.browse_directories(path)


@router.post(
    "/api/settings/directories/pick-native",
    response_model=NativeDirectoryPickResponse,
    summary="弹出服务器系统原生选择文件夹对话框",
    status_code=status.HTTP_200_OK,
)
async def pick_native_directory(
    request: Request,
    payload: NativeDirectoryPickRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """仅服务器本机回环的管理员可触发；子进程弹窗带超时，绝不阻塞事件循环。

    远程访问、非管理员一律拒绝；无图形环境/超时如实返回 ``unavailable``，
    由前端降级到页面内服务器目录浏览器，不做任何静默兜底。
    """
    context = require_authenticated(authorization, x_user_role)
    if context.role != "admin":
        raise ForbiddenException(message="仅管理员可调用服务端目录选择器")
    if not _is_loopback_request(request):
        raise CleanroomException(
            status_code=403,
            code="PICKER_NON_LOOPBACK",
            message="服务端目录选择器仅限服务器本机会话使用；远程访问请使用页面内目录浏览器",
        )
    result = await asyncio.to_thread(
        native_picker.pick_directory,
        payload.title or "选择服务器目录",
        payload.initial_dir or "",
        payload.timeout_seconds,
    )
    return NativeDirectoryPickResponse(**result)


@router.patch(
    "/api/storage-settings",
    response_model=StorageSettingsSnapshot,
    summary="保存存储设置",
    status_code=status.HTTP_200_OK,
)
def patch_storage_settings(
    payload: StorageSettingsPatchRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """保存存储设置；要求 expected_version，版本不一致返回 409 VERSION_CONFLICT。"""
    require_edit_access(authorization, x_user_role)
    return default_storage_settings_service.patch(payload)


def _require_storage_admin(authorization: str | None, x_user_role: str):
    context = require_authenticated(authorization, x_user_role)
    if context.role != "admin":
        raise ForbiddenException(message="仅管理员可管理允许根目录")
    return context


@router.get(
    "/api/storage-settings/roots",
    response_model=StorageAllowedRootsSnapshot,
    summary="读取系统根与持久允许根",
    status_code=status.HTTP_200_OK,
)
def get_storage_allowed_roots(
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    _require_storage_admin(authorization, x_user_role)
    return default_storage_allowed_roots_service.get_snapshot()


@router.post(
    "/api/storage-settings/roots",
    response_model=StorageAllowedRootsSnapshot,
    summary="登记持久允许根",
    status_code=status.HTTP_200_OK,
)
def add_storage_allowed_root(
    payload: StorageAllowedRootRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    _require_storage_admin(authorization, x_user_role)
    return default_storage_allowed_roots_service.add_root(payload)


@router.delete(
    "/api/storage-settings/roots/{root_id}",
    response_model=StorageAllowedRootsSnapshot,
    summary="移除持久允许根",
    status_code=status.HTTP_200_OK,
)
def remove_storage_allowed_root(
    root_id: str = Path(..., min_length=1, max_length=128),
    payload: StorageAllowedRootDeleteRequest = Body(...),  # noqa: B008
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    _require_storage_admin(authorization, x_user_role)
    return default_storage_allowed_roots_service.remove_root(root_id, payload)


@router.post(
    "/api/storage-settings/roots/{root_id}/restore",
    response_model=StorageAllowedRootsSnapshot,
    summary="恢复已移除的允许根",
    status_code=status.HTTP_200_OK,
)
def restore_storage_allowed_root(
    root_id: str = Path(..., min_length=1, max_length=128),
    payload: StorageAllowedRootDeleteRequest = Body(...),  # noqa: B008
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    _require_storage_admin(authorization, x_user_role)
    return default_storage_allowed_roots_service.restore_root(root_id, payload)


@router.delete(
    "/api/storage-settings/roots/{root_id}/purge",
    response_model=StorageAllowedRootsSnapshot,
    summary="彻底删除允许根及其系统索引与缓存",
    status_code=status.HTTP_200_OK,
)
def purge_storage_allowed_root(
    root_id: str = Path(..., min_length=1, max_length=128),
    payload: StorageAllowedRootDeleteRequest = Body(...),  # noqa: B008
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    _require_storage_admin(authorization, x_user_role)
    return default_storage_allowed_roots_service.purge_root(root_id, payload)


@router.post(
    "/api/storage-settings/roots/{root_id}/aliases/confirm",
    response_model=StorageAllowedRootsSnapshot,
    summary="确认允许根的路径别名",
    status_code=status.HTTP_200_OK,
)
def confirm_storage_allowed_root_alias(
    root_id: str = Path(..., min_length=1, max_length=128),
    payload: StorageAllowedRootRequest = Body(...),  # noqa: B008
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    _require_storage_admin(authorization, x_user_role)
    return default_storage_allowed_roots_service.confirm_alias(root_id, payload)


# ---------------------------------------------------------------------------
# 模型平台（provider）
# ---------------------------------------------------------------------------


@router.get(
    "/api/providers",
    response_model=ProviderSnapshot,
    summary="读取模型平台列表",
    status_code=status.HTTP_200_OK,
)
def list_providers(
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """返回平台集合真值；默认必须为空数组，绝不预置任何厂商条目。"""
    require_authenticated(authorization, x_user_role)
    return default_provider_service.get_snapshot()


@router.put(
    "/api/providers",
    response_model=ProviderSnapshot,
    summary="整体保存模型平台列表",
    status_code=status.HTTP_200_OK,
)
def replace_providers(
    payload: Union[list[Any], dict[str, Any], None] = Body(
        None, description="provider 数组（前端既有调用面）或 {\"providers\": [...]} 包装"
    ),
    expected_version: int | None = Query(None, ge=1, description="provider 集合 CAS 期望版本；保存时必填"),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """严格验证平台集合并原子保存元数据与密文；必填CAS，凭据原文不回显。"""
    require_edit_access(authorization, x_user_role)
    # 兼容 ``{"providers": [...]}`` 包装；裸数组仍是前端既有调用面。
    if isinstance(payload, dict):
        if set(payload) != {"providers"}:
            raise CleanroomException(400, "INVALID_REQUEST", "平台请求包装必须且只能包含providers数组")
        raw: Any = payload["providers"]
    else:
        raw = payload
    return _credential_vault().replace(raw, expected_version)


@router.post(
    "/api/providers/fetch-models",
    summary="真实拉取上游模型列表",
    status_code=status.HTTP_200_OK,
)
def fetch_provider_models(
    payload: dict | None = Body(None),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """按实际目录协议请求；根地址/版本地址均支持，不能据目录宣称生成能力。"""
    require_edit_access(authorization, x_user_role)
    return provider_probes.fetch_models(payload or {})


@router.post(
    "/api/providers/probe-async",
    summary="同步探测模型目录（旧地址兼容）",
    status_code=status.HTTP_200_OK,
)
def probe_provider_async(
    payload: dict | None = Body(None),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """请求完成后保存历史结果；兼容job_id/poll_hint，不存在后台排队。"""
    require_edit_access(authorization, x_user_role)
    return provider_probes.probe_async(payload or {})


@router.get(
    "/api/providers/probe-async/jobs/{job_id}",
    summary="读取已完成的目录探测记录",
    status_code=status.HTTP_200_OK,
)
def get_provider_probe_job(
    job_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """读取旧地址下的已完成探测记录；不存在404，不承诺后台任务。"""
    require_authenticated(authorization, x_user_role)
    return provider_probes.get_probe_job(job_id)


@router.post(
    "/api/providers/test-connection",
    summary="真实验证上游连通性",
    status_code=status.HTTP_200_OK,
)
def test_provider_connection(
    payload: dict | None = Body(None),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """真实往返并记录实测延迟；失败 503，绝不伪造连通性或延迟数字。"""
    require_edit_access(authorization, x_user_role)
    return provider_probes.test_connection(payload or {})


@router.get(
    "/api/providers/credentials",
    summary="读取服务端凭据状态",
    status_code=status.HTTP_200_OK,
)
def list_provider_credentials(
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """只返回已配置字段的布尔状态，不返回密文、原文、URL 或环境变量名。"""
    require_authenticated(authorization, x_user_role)
    return _credential_vault().public_snapshot()


@router.post(
    "/api/providers/{provider_id}/gateway/chat",
    summary="通过服务端凭据网关发起 Chat 请求",
    status_code=status.HTTP_200_OK,
)
def provider_gateway_chat(
    provider_id: str,
    payload: ProviderGatewayChatRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """客户端只提交消息与稳定 provider_id；服务端读取密钥并向上游注入鉴权。"""
    require_edit_access(authorization, x_user_role)
    vault = _credential_vault()
    vault.require(provider_id)
    request_payload = payload.model_dump(exclude_none=True)
    request_payload["provider_id"] = provider_id
    vault.reject_client_credentials(request_payload)
    try:
        return chat_runtime.complete(request_payload, agent=False)
    except CleanroomException as exc:
        return chat_runtime.error_response(exc)


# ---------------------------------------------------------------------------
# 素材版本/组结构
# ---------------------------------------------------------------------------


@router.get(
    "/api/asset-registry/asset-structures",
    response_model=AssetStructureListResponse,
    summary="读取素材结构集合",
    status_code=status.HTTP_200_OK,
)
def list_asset_structures(
    asset_ids: list[str] | None = Query(None, description="按成员过滤（可重复）"),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """返回结构集合；无结构时 items: []，不编造成员素材元数据。"""
    require_authenticated(authorization, x_user_role)
    return default_asset_structure_service.list_structures(asset_ids)


@router.post(
    "/api/asset-registry/asset-structures",
    response_model=AssetStructureResponse,
    summary="创建素材结构",
    status_code=status.HTTP_201_CREATED,
)
def create_asset_structure(
    payload: AssetStructureCreateRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """创建版本/组结构；要求写权限，集合 CAS 冲突返回 409 STRUCTURE_VERSION_CONFLICT。"""
    require_edit_access(authorization, x_user_role)
    structure = default_asset_structure_service.create_structure(payload)
    return AssetStructureResponse(structure=structure)


@router.get(
    "/api/asset-registry/asset-structures/{structure_id}",
    response_model=AssetStructureResponse,
    summary="读取单个素材结构",
    status_code=status.HTTP_200_OK,
)
def get_asset_structure(
    structure_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """返回目标结构真值；不存在必须 404 STRUCTURE_NOT_FOUND。"""
    require_authenticated(authorization, x_user_role)
    return AssetStructureResponse(structure=default_asset_structure_service.get_structure(structure_id))


@router.patch(
    "/api/asset-registry/asset-structures/{structure_id}",
    response_model=AssetStructureResponse,
    summary="更新素材结构",
    status_code=status.HTTP_200_OK,
)
def update_asset_structure(
    structure_id: str,
    payload: AssetStructureUpdateRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """更新结构成员或当前素材；要求写权限，结构 CAS 冲突返回 409 STRUCTURE_VERSION_CONFLICT。"""
    require_edit_access(authorization, x_user_role)
    structure = default_asset_structure_service.update_structure(structure_id, payload)
    return AssetStructureResponse(structure=structure)


@router.patch(
    "/api/asset-registry/asset-structures/{structure_id}/current",
    response_model=AssetStructureResponse,
    summary="切换结构当前素材",
    status_code=status.HTTP_200_OK,
)
def set_asset_structure_current(
    structure_id: str,
    payload: AssetStructureCurrentRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """切换当前代表素材；要求写权限，结构 CAS 冲突返回 409 STRUCTURE_VERSION_CONFLICT。"""
    require_edit_access(authorization, x_user_role)
    structure = default_asset_structure_service.set_current(structure_id, payload)
    return AssetStructureResponse(structure=structure)


@router.delete(
    "/api/asset-registry/asset-structures/{structure_id}",
    response_model=AssetStructureDeleteResponse,
    summary="删除素材结构",
    status_code=status.HTTP_200_OK,
)
def delete_asset_structure(
    structure_id: str,
    expected_version: int | None = Query(None, ge=1, description="删除结构必须携带的 CAS 期望版本"),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role", description="用户角色权限"),
):
    """删除结构；要求写权限，结构 CAS 冲突返回 409 STRUCTURE_VERSION_CONFLICT。"""
    require_edit_access(authorization, x_user_role)
    deleted_id, revision = default_asset_structure_service.delete_structure(structure_id, expected_version)
    return AssetStructureDeleteResponse(deleted_structure_id=deleted_id, revision=revision)
