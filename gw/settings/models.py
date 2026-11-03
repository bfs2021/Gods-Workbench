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

"""设置页契约模型（Phase 10D，Pydantic v2）。

严格对齐 ``docs/contracts/SETTINGS-INTERFACE-CATALOG.yaml``（version: p10d-frozen-1）。

字段口径：

- 稳定 ID 一律使用 ``structure_id`` / ``provider_id`` / ``revision``，
  禁止 pid / cid / sid 等别名；
- 结构体成员仅登记 ``asset_id`` 与 ``sort_order``；素材注册表未接入时
  **不写** ``asset`` 元数据（宁缺毋滥，绝不编造成员状态）；
- 探测端点无真实出网能力，模型层不存在模型列表 / 延迟数字等可伪造字段。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

#: 数据可用性口径；无真实来源时必须如实标记，不得谎报 ok。
DATA_STATUS_OK = "ok"
DATA_STATUS_NOT_CONFIGURED = "not_configured"

#: 明确的缺口说明文案（data_gaps）。
GAP_STORAGE_SETTINGS_SOURCE = "storage_settings_source_not_integrated"
GAP_PROVIDER_REGISTRY_SOURCE = "provider_registry_source_not_integrated"
GAP_MEMBER_ASSET_METADATA = "member_asset_metadata_not_integrated"

#: 本阶段探测端点统一错误码（fail-closed）。
PROVIDER_PROBE_NOT_INTEGRATED = "PROVIDER_PROBE_NOT_INTEGRATED"


class StorageSettingsSnapshot(BaseModel):
    """``GET/PATCH /api/storage-settings`` 响应：存储设置真值快照。"""

    model_config = ConfigDict(extra="ignore")

    configured: bool = Field(..., description="是否已有真实配置来源")
    revision: int = Field(..., ge=1, description="存储设置 CAS 版本号")
    dirs: dict[str, Any] = Field(default_factory=dict, description="目录映射；未配置时为空对象")
    defaults: dict[str, Any] = Field(default_factory=dict, description="默认目录建议；无来源时为空对象")
    extra_local_dirs: list[str] = Field(default_factory=list, description="附加本地目录路径列表")
    extra_local_entries: list[dict[str, Any]] = Field(default_factory=list, description="附加本地来源条目")
    local_library_names: dict[str, str] = Field(default_factory=dict, description="本地素材库显示名")
    local_library_order: list[str] = Field(default_factory=list, description="本地素材库排序")
    local_library_read_only: dict[str, bool] = Field(default_factory=dict, description="来源内容只读策略")
    local_libraries: list[dict[str, Any]] = Field(default_factory=list, description="本地素材库投影")
    data_status: str = Field(DATA_STATUS_NOT_CONFIGURED, description="ok 或 not_configured")
    data_gaps: list[str] = Field(default_factory=list, description="未接入 / 未配置原因")


class StorageSettingsPatchRequest(BaseModel):
    """``PATCH /api/storage-settings`` 请求体；未给出的字段保持不变。"""

    model_config = ConfigDict(extra="ignore")

    dirs: dict[str, Any] | None = None
    extra_local_entries: list[dict[str, Any]] | None = None
    local_library_names: dict[str, str] | None = None
    local_library_order: list[str] | None = None
    local_library_read_only: dict[str, bool] | None = None
    expected_version: int | None = Field(None, ge=1, description="保存目录设置必须携带的 CAS 期望版本")


class StorageAllowedRoot(BaseModel):
    """允许根管理界面的单个根目录投影。"""

    id: str
    path: str
    source: Literal["system", "persistent"]
    read_only: bool
    active: bool = True
    aliases: list[str] = Field(default_factory=list)
    alias_candidates: list[dict[str, str]] = Field(default_factory=list)
    availability: str = "available"
    availability_reason: str = ""
    availability_message: str = ""


class StorageAllowedRootsSnapshot(BaseModel):
    """环境变量根与持久允许根的 CAS 快照。"""

    revision: int = Field(..., ge=1)
    roots: list[StorageAllowedRoot] = Field(default_factory=list)


class StorageAllowedRootRequest(BaseModel):
    """新增允许根或确认挂载别名的 CAS 请求。"""

    path: str
    expected_version: int = Field(..., ge=1)


class StorageAllowedRootDeleteRequest(BaseModel):
    """移除持久允许根的 CAS 请求。"""

    expected_version: int = Field(..., ge=1)


class ProviderSnapshot(BaseModel):
    """``GET/PUT /api/providers`` 响应：模型平台集合快照。"""

    model_config = ConfigDict(extra="ignore")

    providers: list[dict[str, Any]] = Field(default_factory=list, description="provider 条目；默认必须为空数组")
    revision: int = Field(..., ge=1, description="provider 集合 CAS 版本号")
    configured: bool = Field(..., description="是否已有真实 provider 配置")
    data_status: str = Field(DATA_STATUS_NOT_CONFIGURED, description="ok 或 not_configured")
    data_gaps: list[str] = Field(default_factory=list, description="未接入 / 未配置原因")


ProviderId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
ModelId = Annotated[str, StringConstraints(strict=True, pattern=r"^[^\s\x00-\x1f]{1,256}$")]
Label = Annotated[str, StringConstraints(strict=True, max_length=256)]
ModelList = Annotated[list[ModelId], Field(max_length=256)]
Protocol = Literal["openai", "apimart", "gemini", "grok", "volcengine", "jimeng", "codex", "gemini-cli"]
SecretInput = Annotated[str, StringConstraints(strict=True, max_length=8192)]


class ProviderLora(BaseModel):
    """有界LoRA配置；不允许嵌入任意字典或凭据字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: ModelId
    name: Label = ""
    target_model: ModelId
    strength: float = Field(0.8, ge=0, le=2)
    enabled: bool = True
    note: str = Field("", max_length=2048)


class ProviderEntryRequest(BaseModel):
    """平台元数据与显式凭据更新意图；响应不使用该输入模型。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: ProviderId | None = None
    provider_id: ProviderId | None = None
    name: Label = ""
    base_url: str = Field("", max_length=2048)
    protocol: Protocol = "openai"
    enabled: bool = True
    primary: bool = False
    image_models: ModelList = Field(default_factory=list)
    chat_models: ModelList = Field(default_factory=list)
    video_models: ModelList = Field(default_factory=list)
    model_names: dict[ModelId, Label] = Field(default_factory=dict)
    model_protocols: dict[ModelId, Literal["openai", "gemini"]] = Field(default_factory=dict)
    image_request_mode: Literal["openai", "openai-json", "openai-video-proxy", "openai-responses", "openai-async-image", "tudou-async", "newapi-sync-image"] = "openai"
    image_edit_route: Literal["general", "auto", "chat"] = "general"
    video_protocol: Literal["newapi_video"] | None = None
    image_generation_endpoint: str = Field("", max_length=256, pattern=r"^(/[^?#:\s\x00-\x1f]{0,255})?$")
    image_edit_endpoint: str = Field("", max_length=256, pattern=r"^(/[^?#:\s\x00-\x1f]{0,255})?$")
    ms_loras: list[ProviderLora] = Field(default_factory=list, max_length=256)
    ms_defaults_version: int = Field(0, ge=0)
    volcengine_project_name: Label = ""
    volcengine_region: Label = ""
    api_key: SecretInput | None = Field(None, repr=False)
    wallet_api_key: SecretInput | None = Field(None, repr=False)
    volcengine_access_key_id: SecretInput | None = Field(None, repr=False)
    volcengine_secret_access_key: SecretInput | None = Field(None, repr=False)
    clear_key: bool = False
    clear_wallet_key: bool = False
    clear_volcengine_access_key_id: bool = False
    clear_volcengine_secret_access_key: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if not value:
            return value
        try:
            parts = urlsplit(value)
            valid = (parts.scheme in {"http", "https"} and parts.hostname
                     and not parts.username and not parts.password and not parts.query and not parts.fragment
                     and not any(ord(ch) < 33 for ch in value))
            _ = parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("平台地址必须为不含凭据、查询串或片段的HTTP地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_entry(self):
        from gw.settings.execution_config import normalize_provider_base_url
        self.base_url = normalize_provider_base_url(self.base_url, self.protocol)
        if len(self.base_url) > 2048:
            raise ValueError("规范化后的平台地址过长")
        if not (self.provider_id or self.id) or (self.id and self.provider_id and self.id != self.provider_id):
            raise ValueError("平台ID缺失或相互冲突")
        if (self.provider_id or self.id) == "legacy_environment":
            raise ValueError("旧环境兼容ID不能由页面创建")
        for models in (self.image_models, self.chat_models, self.video_models):
            if len(set(models)) != len(models):
                raise ValueError("模型ID不能重复")
        for secret, clear in ((self.api_key, self.clear_key), (self.wallet_api_key, self.clear_wallet_key),
                              (self.volcengine_access_key_id, self.clear_volcengine_access_key_id),
                              (self.volcengine_secret_access_key, self.clear_volcengine_secret_access_key)):
            if secret and (clear or any(ord(ch) < 32 for ch in secret)):
                raise ValueError("凭据更新意图或格式不合法")
        if len(self.model_names) > 768 or len(self.model_protocols) > 512:
            raise ValueError("模型映射超过上限")
        models = set(self.image_models) | set(self.chat_models) | set(self.video_models)
        if set(self.model_names) - models or set(self.model_protocols) - (set(self.image_models) | set(self.chat_models)):
            raise ValueError("模型映射必须引用该平台已登记的对应模型")
        return self


class ProviderPutRequest(BaseModel):
    """``PUT /api/providers`` 请求体包装。

    前端既有的调用面是「外层直接发送数组」，因此路由层接受裸数组；
    本模型仅用于内部规范化与单测，不要求前端改变调用方式。
    """

    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderEntryRequest] = Field(..., max_length=128)


class AssetStructureMember(BaseModel):
    """结构成员；仅登记稳定 asset_id 与顺序，不编造资源元数据。"""

    model_config = ConfigDict(extra="ignore")

    asset_id: str = Field(..., description="成员素材稳定标识")
    sort_order: int = Field(..., ge=0, description="成员展示顺序")


class AssetStructureItem(BaseModel):
    """版本/组结构实体；``structure_id`` 为确定性前缀序号。"""

    model_config = ConfigDict(extra="ignore")

    structure_id: str = Field(..., description="稳定结构标识（strc_NNNN）")
    kind: str = Field(..., description="结构类型：version 或 group")
    current_asset_id: str = Field(..., description="当前代表素材标识")
    version: int = Field(..., ge=1, description="结构级 CAS 版本号")
    created_at: str = Field(..., description="创建时间（ISO 8601 UTC）")
    updated_at: str = Field(..., description="最近更新时间（ISO 8601 UTC）")
    members: list[AssetStructureMember] = Field(default_factory=list, description="成员集合")


class AssetStructureListResponse(BaseModel):
    """``GET /api/asset-registry/asset-structures`` 响应。"""

    model_config = ConfigDict(extra="ignore")

    items: list[AssetStructureItem] = Field(default_factory=list, description="结构集合")
    total: int = Field(0, ge=0, description="结构总数")
    revision: int = Field(..., ge=1, description="结构集合 CAS 版本号")
    data_status: str = Field(DATA_STATUS_OK, description="ok 或 not_configured")
    data_gaps: list[str] = Field(default_factory=list, description="未接入原因")


class AssetStructureCreateRequest(BaseModel):
    """``POST /api/asset-registry/asset-structures`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, description="version 或 group")
    asset_ids: list[str] = Field(..., description="成员素材标识；至少 2 个且不重复")
    current_asset_id: str | None = Field(None, description="当前代表素材；必须是成员之一")
    expected_version: int | None = Field(None, ge=1, description="创建结构必须携带的集合 CAS 期望版本")


class AssetStructureUpdateRequest(BaseModel):
    """``PATCH /api/asset-registry/asset-structures/{structure_id}`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    asset_ids: list[str] | None = None
    current_asset_id: str | None = None
    expected_version: int | None = Field(None, ge=1, description="更新结构必须携带的结构 CAS 期望版本")


class AssetStructureCurrentRequest(BaseModel):
    """``PATCH .../{structure_id}/current`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    current_asset_id: str = Field(..., min_length=1, description="新的当前代表素材；必须是成员之一")
    expected_version: int | None = Field(None, ge=1, description="切换当前素材必须携带的结构 CAS 期望版本")


class AssetStructureResponse(BaseModel):
    """单结构响应包装（创建 / 读取 / 更新 / 切换当前）。"""

    model_config = ConfigDict(extra="ignore")

    structure: AssetStructureItem


class AssetStructureDeleteResponse(BaseModel):
    """``DELETE .../{structure_id}`` 响应：删除结果与集合最新版本。"""

    model_config = ConfigDict(extra="ignore")

    deleted_structure_id: str
    revision: int = Field(..., ge=1)
