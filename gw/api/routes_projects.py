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

"""项目中心 API 路由实现。

严格对齐 docs/contracts/PROJECTS-HUB-INTERFACE-CATALOG.yaml 定义的 7 个端点，接驳 ProjectsService。
"""

from fastapi import APIRouter, Body, Header, Query, status

from gw.core.auth import (
    require_authenticated,
    require_edit_access,
    require_governance_access,
)
from gw.core.errors import CleanroomException
from gw.projects_hub.models import (
    CasVersionRequest,
    ProjectCreateRequest,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectMutationResponse,
    ProjectUpdateRequest,
)
from gw.projects_hub.service import (
    default_projects_service,
    owner_key_for_context,
    owner_key_for_identity,
    require_project_write_context,
)

router = APIRouter(prefix="/api/asset-registry", tags=["projects-hub"])


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    summary="获取项目列表",
    status_code=status.HTTP_200_OK,
)
def list_projects(
    archived: bool | None = Query(False, description="是否包含归档项目"),
    deleted: bool | None = Query(None, description="是否包含回收站项目"),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """根据查询条件返回项目集合；必须登录，只返回调用方可读的项目（C4-AC7）。"""
    # C4-AC7（用户裁定 2026-10-15）：必须登录，匿名 401；已认证按读授权过滤。
    owner_key, role = _viewer(require_authenticated(authorization, x_user_role))
    projects = default_projects_service.list_projects(
        archived=archived, deleted=deleted, viewer_owner_key=owner_key, viewer_role=role
    )
    try:
        from gw.asset_registry import repository as asset_repo
        asset_counts = asset_repo.project_asset_counts()
        for proj in projects:
            proj.asset_count = asset_counts.get(proj.project_id, 0)
    except Exception:
        pass
    return ProjectListResponse(projects=projects)


@router.post(
    "/projects",
    response_model=ProjectMutationResponse,
    summary="创建项目",
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    payload: ProjectCreateRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header(
        "editor", alias="X-User-Role", description="用户角色权限"
    ),
):
    """创建新项目。"""
    context = require_edit_access(authorization, x_user_role)
    # 认证主体由服务端上下文解析为 owner_key；项目 owner 注册在项目快照提交前完成。
    from gw.video_tasks.service import get_video_service

    video_service = get_video_service()
    result = default_projects_service.create_project(
        payload,
        owner_key=owner_key_for_context(context),
        before_publish=lambda project_id: video_service.register_project_owner(
            project_id, context
        ),
    )
    return ProjectMutationResponse(project=result)


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectMutationResponse,
    summary="编辑项目（CAS）",
    status_code=status.HTTP_200_OK,
)
def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header(
        "editor", alias="X-User-Role", description="用户角色权限"
    ),
):
    """更新项目元信息，要求 expected_version 匹配。"""
    context = require_authenticated(authorization, x_user_role)
    require_project_write_context(project_id, context)
    result = default_projects_service.update_project(project_id, payload)
    return ProjectMutationResponse(project=result)


@router.delete(
    "/projects/{project_id}",
    response_model=ProjectMutationResponse,
    summary="归档项目",
    status_code=status.HTTP_200_OK,
)
def archive_project(
    project_id: str,
    payload: CasVersionRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header(
        "editor", alias="X-User-Role", description="用户角色权限"
    ),
):
    """将项目移入只读归档状态。"""
    context = require_authenticated(authorization, x_user_role)
    require_project_write_context(project_id, context)
    result = default_projects_service.archive_project(
        project_id, payload.expected_version
    )
    return ProjectMutationResponse(project=result)


@router.post(
    "/governance/projects/{project_id}/restore",
    response_model=ProjectMutationResponse,
    summary="解归档项目",
    status_code=status.HTTP_200_OK,
)
def unarchive_project(
    project_id: str,
    payload: CasVersionRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header(
        "editor", alias="X-User-Role", description="用户角色权限"
    ),
):
    """将已归档项目恢复为活跃状态。"""
    require_governance_access(authorization, x_user_role)
    result = default_projects_service.unarchive_project(
        project_id, payload.expected_version
    )
    return ProjectMutationResponse(project=result)


@router.post(
    "/projects/{project_id}/trash",
    response_model=ProjectMutationResponse,
    summary="移入回收站",
    status_code=status.HTTP_200_OK,
)
def move_project_to_trash(
    project_id: str,
    payload: CasVersionRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header(
        "editor", alias="X-User-Role", description="用户角色权限"
    ),
):
    """仅允许已归档项目移入回收站。"""
    require_governance_access(authorization, x_user_role)
    result = default_projects_service.move_to_trash(
        project_id, payload.expected_version
    )
    return ProjectMutationResponse(project=result)


@router.post(
    "/projects/{project_id}/trash/restore",
    response_model=ProjectMutationResponse,
    summary="从回收站恢复",
    status_code=status.HTTP_200_OK,
)
def restore_project_from_trash(
    project_id: str,
    payload: CasVersionRequest,
    authorization: str | None = Header(None),
    x_user_role: str = Header(
        "editor", alias="X-User-Role", description="用户角色权限"
    ),
):
    """从回收站恢复项目至可用集合。"""
    require_governance_access(authorization, x_user_role)
    result = default_projects_service.restore_from_trash(
        project_id, payload.expected_version
    )
    return ProjectMutationResponse(project=result)


# ---------------------------------------------------------------------------
# B2 兼容项目路径
# ---------------------------------------------------------------------------

legacy_router = APIRouter(tags=["projects-compat"])


@legacy_router.get(
    "/api/projects",
    response_model=ProjectListResponse,
    summary="兼容读取项目列表",
    status_code=status.HTTP_200_OK,
)
def list_projects_compat(
    archived: bool | None = Query(False, description="是否包含归档项目"),
    deleted: bool | None = Query(None, description="是否包含回收站项目"),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """兼容旧工作台路径，复用项目中心同一内存真值。"""
    context = require_authenticated(authorization, x_user_role)
    owner_key, role = _viewer(context)
    return ProjectListResponse(
        projects=default_projects_service.list_projects(
            archived=archived,
            deleted=deleted,
            viewer_owner_key=owner_key,
            viewer_role=role,
        )
    )


@legacy_router.get(
    "/api/projects/{project_id}",
    response_model=ProjectDetailResponse,
    summary="兼容读取单个项目",
    status_code=status.HTTP_200_OK,
)
def get_project_compat(
    project_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """读取项目详情；不存在返回标准 PROJECT_NOT_FOUND。

    QIN-543：同步下发制片条目（含已关联素材摘要）、分组汇总与阶段门，
    资产库「项目制片」看板一次读取即可渲染，不再恒为 0 个条目。
    """
    context = require_authenticated(authorization, x_user_role)
    proj = default_projects_service.get_readable_project(project_id, context)
    try:
        from gw.asset_registry import repository as asset_repo
        asset_counts = asset_repo.project_asset_counts()
        proj.asset_count = asset_counts.get(proj.project_id, 0)
        board = asset_repo.project_entities_view(project_id)
        owner_key, role = _viewer(context)
        gates = asset_repo.project_gates(project_id, owner_key=owner_key, role=role)["gates"]
        return ProjectDetailResponse(
            project=proj,
            entities=board["entities"],
            groups=board["groups"],
            gates=gates,
            data_status=board["data_status"],
        )
    except CleanroomException:
        raise
    except Exception:
        # 注册表读失败不阻断项目详情主读取；条目键保持缺省，不伪造条目数据。
        return ProjectDetailResponse(project=proj)


@legacy_router.get(
    "/api/projects/{project_id}/entities",
    summary="读取项目制片条目",
    status_code=status.HTTP_200_OK,
)
def list_project_entities_compat(
    project_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """读取项目制片条目与分组汇总（QIN-543 AC1）；项目不可读按 404/403 失败关闭。"""
    context = require_authenticated(authorization, x_user_role)
    default_projects_service.get_readable_project(project_id, context)
    from gw.asset_registry import repository as asset_repo
    return asset_repo.project_entities_view(project_id)


@legacy_router.get(
    "/api/projects/{project_id}/assets",
    summary="读取项目已关联素材列表",
    status_code=status.HTTP_200_OK,
)
def list_project_assets_compat(
    project_id: str,
    entity_id: str | None = Query(None, description="按制片条目筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """项目已关联素材列表，支持按条目筛选（QIN-543 AC2）；项目不可读按 404/403 失败关闭。"""
    context = require_authenticated(authorization, x_user_role)
    default_projects_service.get_readable_project(project_id, context)
    from gw.asset_registry import repository as asset_repo
    return asset_repo.project_assets(project_id, entity_id=entity_id, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# C4-AC7/AC8 项目显式读授权
# ---------------------------------------------------------------------------


def _viewer(context):
    try:
        owner_key = owner_key_for_context(context)
    except CleanroomException:
        owner_key = None
    return owner_key, str(getattr(context, "role", "") or "")


def _target_key(context, payload) -> str:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("subject"), str)
        or not payload["subject"].strip()
    ):
        raise CleanroomException(400, "INVALID_REQUEST", "subject 必须为非空字符串")
    domain = payload.get("identity_domain") or getattr(context, "identity_domain", None)
    if not isinstance(domain, str):
        raise CleanroomException(400, "INVALID_REQUEST", "identity_domain 不合法")
    return owner_key_for_identity(domain, payload["subject"])


@router.get(
    "/projects/{project_id}/read-grants",
    summary="项目读授权视图",
    status_code=status.HTTP_200_OK,
)
def get_read_grants(
    project_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    owner_key, role = _viewer(require_authenticated(authorization, x_user_role))
    return default_projects_service.list_read_grants(
        project_id, owner_key=owner_key, role=role
    )


@router.post(
    "/projects/{project_id}/read-grants",
    summary="授予项目读权限",
    status_code=status.HTTP_200_OK,
)
def grant_read(
    project_id: str,
    payload: dict = Body(default=None),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = require_authenticated(authorization, x_user_role)
    owner_key, role = _viewer(context)
    return default_projects_service.grant_read(
        project_id,
        _target_key(context, payload),
        actor_owner_key=owner_key,
        actor_role=role,
    )


@router.post(
    "/projects/{project_id}/read-grants/revoke",
    summary="撤销项目读权限",
    status_code=status.HTTP_200_OK,
)
def revoke_read(
    project_id: str,
    payload: dict = Body(default=None),
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    context = require_authenticated(authorization, x_user_role)
    owner_key, role = _viewer(context)
    return default_projects_service.revoke_read(
        project_id,
        _target_key(context, payload),
        actor_owner_key=owner_key,
        actor_role=role,
    )


@router.post(
    "/projects/{project_id}/read-grants/explicit",
    summary="切换为显式读授权",
    status_code=status.HTTP_200_OK,
)
def set_read_explicit(
    project_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    owner_key, role = _viewer(require_authenticated(authorization, x_user_role))
    return default_projects_service.set_read_mode_explicit(
        project_id, actor_owner_key=owner_key, actor_role=role
    )
