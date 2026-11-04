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

"""项目中心契约模型。

严格对齐 docs/contracts/PROJECTS-HUB-INTERFACE-CATALOG.yaml 及相关黄金夹具。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectType(str, Enum):
    """项目类型枚举。"""

    FILM = "film"
    SERIES = "series"
    OTHER = "other"


class ProjectItem(BaseModel):
    """项目中心实体。"""

    model_config = ConfigDict(extra="ignore")

    project_id: str = Field(..., description="稳定项目标识")
    name: str = Field(..., description="项目名称")
    project_type: ProjectType = Field(..., description="项目类型")
    stage: str = Field(..., description="项目所处生产阶段")
    progress: float = Field(..., ge=0, le=100, description="生产进度 (0-100)")
    scenes: int = Field(0, ge=0, description="场次数")
    shots: int = Field(0, ge=0, description="镜头数")
    description: str | None = Field(None, description="项目摘要")
    start_at: int | None = Field(None, description="排期开始时间戳（毫秒）")
    due_at: int | None = Field(None, description="排期截止时间戳（毫秒）")
    version: int = Field(..., description="并发控制版本号（CAS）")
    archived_at: str | None = Field(None, description="归档时间戳或ISO字符串")
    deleted_at: str | None = Field(None, description="逻辑删除/回收站时间戳或ISO字符串")
    updated_at: str | None = Field(None, description="最近更新时间")
    asset_count: int = Field(0, ge=0, description="关联素材数")


class ProjectDetailResponse(BaseModel):
    """兼容路径单项目读取包装。

    QIN-543：可选下发项目制片条目、分组汇总、阶段门与数据状态；
    旧调用方只读 project 字段，不感知新增可选键。
    """

    project: ProjectItem
    entities: list[dict[str, Any]] | None = Field(None, description="项目制片条目（含已关联素材摘要）")
    groups: list[dict[str, Any]] | None = Field(None, description="分组汇总（按剧集/场景/镜头/角色/任务，含素材数）")
    gates: list[dict[str, Any]] | None = Field(None, description="项目阶段门（按权限过滤）")
    data_status: str | None = Field(None, description="数据状态标识")


class ProjectListResponse(BaseModel):
    """项目列表返回结构。"""

    projects: list[ProjectItem] = Field(default_factory=list, description="项目列表")


class ProjectCreateRequest(BaseModel):
    """创建项目请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="项目名称（必填）")
    project_type: ProjectType = Field(..., description="项目类型")
    description: str | None = Field(None, description="项目描述")
    start_at: int | None = Field(None, description="排期开始时间戳（毫秒）")
    due_at: int | None = Field(None, description="排期截止时间戳（毫秒）")
    client_request_id: str | None = Field(None, description="创建幂等请求键")
    gates: list[dict[str, Any]] | None = Field(
        None, description="明确提交的初始阶段门；省略为空"
    )

    @model_validator(mode="after")
    def validate_schedule(self):
        """阻止结束时间早于开始时间。"""
        if (
            self.start_at is not None
            and self.due_at is not None
            and self.due_at < self.start_at
        ):
            raise ValueError("due_at 不能早于 start_at")
        return self


class ProjectMutationResult(BaseModel):
    """项目变更返回的实体摘要。"""

    project_id: str
    version: int
    archived_at: str | None = None
    deleted_at: str | None = None


class ProjectMutationResponse(BaseModel):
    """项目创建/修改返回外层包装。"""

    project: ProjectMutationResult


class ProjectUpdateRequest(BaseModel):
    """编辑项目请求体（带 CAS expected_version）。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, description="项目名称")
    stage: str | None = Field(None, description="生产阶段")
    scenes: int | None = Field(None, ge=0, description="场次数")
    shots: int | None = Field(None, ge=0, description="镜头数")
    progress: float | None = Field(None, ge=0, le=100, description="生产进度百分比")
    description: str | None = Field(None, description="项目描述")
    start_at: int | None = Field(None, description="排期开始时间戳（毫秒）")
    due_at: int | None = Field(None, description="排期截止时间戳（毫秒）")
    client_request_id: str | None = Field(None, description="创建幂等请求键")
    gates: list[dict[str, Any]] | None = Field(
        None, description="明确提交的初始阶段门；省略为空"
    )
    expected_version: int = Field(..., description="CAS 期望版本号")

    @model_validator(mode="after")
    def validate_schedule(self):
        """校验请求中同时出现的排期字段。"""
        if (
            self.start_at is not None
            and self.due_at is not None
            and self.due_at < self.start_at
        ):
            raise ValueError("due_at 不能早于 start_at")
        return self


class CasVersionRequest(BaseModel):
    """仅携带 expected_version 的状态流转请求体。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., description="CAS 期望版本号")
