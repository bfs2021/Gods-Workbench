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

"""素材库服务层实现（Phase 10A 冻结契约 + Phase 12 真实落盘）。

严格遵循 docs/contracts/ASSET-LIBRARY-INTERFACE-CATALOG.yaml：

- 稳定 ID：library_id / category_id / asset_id；
- 库级 CAS：创建分类校验父库 expected_version，创建素材库校验顶层目录版本；
- 重名（trim 后大小写不敏感）返回 409 DUPLICATE_LIBRARY_NAME / DUPLICATE_CATEGORY_NAME；
- 空库返回 libraries: [] 且 active_library_id: null，绝不返回演示或伪造素材；
- 成功后：新对象 version=1，父库 version += 1，顶层目录 version += 1。

Phase 12 变更：默认模式把整棵素材库树落到单实例 JSON（重启可恢复）；
`seed_golden_fixture=True` 仍走内存隔离，供用例互不干扰。两模式**共用同一套业务逻辑**。

证据边界：单实例；多 worker 并发写属部署方职责，未闭环。
"""

from __future__ import annotations

import copy
import threading
from datetime import UTC, datetime
from typing import Any

from gw.asset_library.models import (
    AssetItem,
    AssetLibraryCreateRequest,
    AssetLibrarySnapshot,
    CategoryCreateRequest,
    CategoryItem,
    LibraryItem,
)
from gw.core import storage
from gw.core.errors import CleanroomException, VersionConflictException

NS_LIBRARY = "asset_library"


def _now_iso() -> str:
    """生成标准 ISO 8601 UTC 时间戳字符串。"""
    return datetime.now(UTC).isoformat()


def _name_key(value: str) -> str:
    """重名比较键：trim 后大小写不敏感。"""
    return (value or "").strip().casefold()


def _default_raw() -> dict[str, Any]:
    return {"directory_version": 1, "active_library_id": None, "libraries": {},
            "sequences": {"library": 0, "category": 0, "asset": 0}}


def _seeded_raw() -> dict[str, Any]:
    timestamp = "2026-09-22T06:00:00Z"
    category = {
        "category_id": "category_image", "library_id": "library_default", "name": "图片",
        "type": "image", "version": 1, "created_at": timestamp, "updated_at": timestamp, "items": [],
    }
    library = {
        "library_id": "library_default", "name": "默认资产库", "version": 2,
        "created_at": timestamp, "updated_at": timestamp, "categories": [category],
    }
    return {"directory_version": 2, "active_library_id": "library_default",
            "libraries": {"library_default": library},
            "sequences": {"library": 1, "category": 1, "asset": 0}}


class AssetLibraryService:
    """素材库存储与 CAS 状态服务。默认落盘；夹具模式为内存隔离，业务逻辑共用。"""

    def __init__(self, seed_golden_fixture: bool = False) -> None:
        self._seed_fixture = bool(seed_golden_fixture)
        self._lock = threading.Lock()
        self._memory: dict[str, Any] | None = _seeded_raw() if seed_golden_fixture else None

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------
    def _read_raw(self) -> dict[str, Any]:
        if self._seed_fixture:
            return copy.deepcopy(self._memory)
        return storage.JsonState(NS_LIBRARY, _default_raw).read()

    def _write_raw(self, raw: dict[str, Any]) -> None:
        if self._seed_fixture:
            self._memory = copy.deepcopy(raw)
        else:
            storage.JsonState(NS_LIBRARY, _default_raw).write(raw)

    def _mutate(self, mutator) -> Any:
        """在锁内完成读-改-写；写入前不落盘，异常时不留下半成品。"""
        with self._lock:
            raw = self._read_raw()
            result = mutator(raw)
            self._write_raw(raw)
            return result

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_snapshot(self) -> AssetLibrarySnapshot:
        with self._lock:
            raw = self._read_raw()
            return self._snapshot(raw)

    def _snapshot(self, raw: dict[str, Any]) -> AssetLibrarySnapshot:
        from gw.core.asset_visibility import asset_visible
        raw = copy.deepcopy(raw)
        for library in raw.get("libraries", {}).values():
            for category in library.get("categories", []):
                category["items"] = [item for item in category.get("items", []) if asset_visible(item)]
        libraries = [LibraryItem.model_validate(raw["libraries"][key])
                     for key in sorted(raw.get("libraries", {}))]
        return AssetLibrarySnapshot(
            version=int(raw.get("directory_version", 1)),
            active_library_id=raw.get("active_library_id"),
            libraries=libraries,
        )

    def iter_library_items(self) -> list[dict[str, Any]]:
        """只读枚举当前主体可见的全部条目（附所属库/分类上下文），供收集箱迁移使用。"""
        from gw.core.asset_visibility import asset_visible

        with self._lock:
            raw = copy.deepcopy(self._read_raw())
        items: list[dict[str, Any]] = []
        for library in raw.get("libraries", {}).values():
            for category in library.get("categories", []):
                for item in category.get("items", []):
                    if not asset_visible(item):
                        continue
                    items.append(
                        {
                            **item,
                            "library_id": library.get("library_id"),
                            "category_id": category.get("category_id"),
                            "category_type": category.get("type"),
                        }
                    )
        return items

    @staticmethod
    def _visible_category(category):
        from gw.core.asset_visibility import asset_visible
        return {**category, "items": [item for item in category.get("items", []) if asset_visible(item)]}

    def _visible_library(self, library):
        return {**library, "categories": [self._visible_category(category) for category in library.get("categories", [])]}

    # ------------------------------------------------------------------
    # 内部定位与断言
    # ------------------------------------------------------------------
    @staticmethod
    def _library(raw: dict[str, Any], library_id: str) -> dict[str, Any]:
        library = raw.get("libraries", {}).get(library_id)
        if library is None:
            raise CleanroomException(404, "LIBRARY_NOT_FOUND", f"素材库 {library_id} 不存在")
        return library

    @staticmethod
    def _category(raw: dict[str, Any], category_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for library in raw.get("libraries", {}).values():
            for category in library.get("categories", []):
                if category.get("category_id") == category_id:
                    return library, category
        raise CleanroomException(404, "CATEGORY_NOT_FOUND", f"分类 {category_id} 不存在")

    @staticmethod
    def _locate(raw: dict[str, Any], asset_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """跨全部库/分类按 asset_id 定位条目；找不到返回 (None, None, None)，不抛异常。"""
        for library in raw.get("libraries", {}).values():
            for category in library.get("categories", []):
                for item in category.get("items", []):
                    if item.get("asset_id") == asset_id:
                        return library, category, item
        return None, None, None

    @staticmethod
    def _asset(raw: dict[str, Any], asset_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        for library in raw.get("libraries", {}).values():
            for category in library.get("categories", []):
                for item in category.get("items", []):
                    if item.get("asset_id") == asset_id:
                        from gw.core.asset_visibility import asset_visible
                        if not asset_visible(item):
                            raise CleanroomException(404, "ASSET_NOT_FOUND", "素材不存在")
                        return library, category, item
        raise CleanroomException(404, "ASSET_NOT_FOUND", f"素材 {asset_id} 不存在")

    @staticmethod
    def _check_version(expected: int | None, current: int) -> None:
        if type(expected) is not int or expected < 1:
            raise CleanroomException(400, "INVALID_REQUEST", "expected_version 必须为正整数")
        if expected != int(current):
            raise VersionConflictException(
                expected_version=expected, current_version=int(current),
                message="版本冲突，请重新读取后重试",
            )

    @staticmethod
    def _next_seq(raw: dict[str, Any], key: str) -> int:
        """先持久化预留序号，再更新业务快照，避免写入失败后复用 ID。"""
        prefix = key + "_"
        sequences = raw.setdefault("sequences", {})
        previous = int(sequences.get(key, 0))

        def reserve(reservations: dict[str, Any]) -> int:
            reserved_sequences = reservations.setdefault("sequences", {})
            last = max(previous, int(reserved_sequences.get(prefix, 0)))
            reservation_id = storage.next_sequence(
                prefix, (f"{prefix}{last:04d}",)
            )
            sequence = int(reservation_id[len(prefix):])
            reserved_sequences[prefix] = sequence
            return sequence

        sequence = storage.JsonState(
            "id_reservations", lambda: {"sequences": {}}
        ).mutate(reserve)
        sequences[key] = sequence
        return sequence

    @staticmethod
    def _touch(raw: dict[str, Any]) -> None:
        raw["directory_version"] = int(raw.get("directory_version", 1)) + 1

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------
    def create_library(self, payload: AssetLibraryCreateRequest) -> tuple[LibraryItem, AssetLibrarySnapshot]:
        """创建素材库；重名或 CAS 冲突失败关闭。"""
        def mutator(raw: dict[str, Any]) -> Any:
            self._check_version(payload.expected_version, int(raw.get("directory_version", 1)))
            name = payload.name.strip()
            if not name:
                raise CleanroomException(400, "INVALID_REQUEST", "素材库名称不能为空或全空白")
            for other in raw["libraries"].values():
                if _name_key(other["name"]) == _name_key(name):
                    raise CleanroomException(409, "DUPLICATE_LIBRARY_NAME", f"素材库名称已存在：{name}")
            library_id = f"library_{self._next_seq(raw, 'library'):04d}"
            stamp = _now_iso()
            raw["libraries"][library_id] = {
                "library_id": library_id, "name": name, "version": 1,
                "created_at": stamp, "updated_at": stamp, "categories": [],
            }
            if raw.get("active_library_id") is None:
                raw["active_library_id"] = library_id
            self._touch(raw)
            return raw["libraries"][library_id]
        item = self._mutate(mutator)
        return LibraryItem.model_validate(item), self.get_snapshot()

    def create_category(self, payload: CategoryCreateRequest) -> tuple[CategoryItem, LibraryItem]:
        """在指定素材库下创建分类；父库不存在或版本冲突失败关闭。"""
        def mutator(raw: dict[str, Any]) -> Any:
            parent = self._library(raw, payload.library_id)
            self._check_version(payload.expected_version, parent["version"])
            name = payload.name.strip()
            if not name:
                raise CleanroomException(400, "INVALID_REQUEST", "分类名称不能为空或全空白")
            for other in parent["categories"]:
                if _name_key(other["name"]) == _name_key(name):
                    raise CleanroomException(409, "DUPLICATE_CATEGORY_NAME", f"分类名称已存在：{name}")
            category_id = f"category_{self._next_seq(raw, 'category'):04d}"
            stamp = _now_iso()
            kind = payload.type.value if hasattr(payload.type, "value") else str(payload.type)
            category = {
                "category_id": category_id, "library_id": parent["library_id"], "name": name,
                "type": kind, "version": 1, "created_at": stamp, "updated_at": stamp, "items": [],
            }
            parent["categories"].append(category)
            parent["version"] += 1
            parent["updated_at"] = stamp
            self._touch(raw)
            return {"category": category, "library": parent}
        result = self._mutate(mutator)
        return CategoryItem.model_validate(self._visible_category(result["category"])), LibraryItem.model_validate(self._visible_library(result["library"]))

    # ------------------------------------------------------------------
    # 素材库 / 分类 变更
    # ------------------------------------------------------------------
    def rename_library(self, library_id: str, name: str, expected_version: int | None) -> LibraryItem:
        def mutator(raw: dict[str, Any]) -> Any:
            library = self._library(raw, library_id)
            self._check_version(expected_version, library["version"])
            clean = (name or "").strip()
            if not clean:
                raise CleanroomException(400, "INVALID_REQUEST", "素材库名称不能为空")
            for other in raw["libraries"].values():
                if other["library_id"] != library_id and _name_key(other["name"]) == _name_key(clean):
                    raise CleanroomException(409, "DUPLICATE_LIBRARY_NAME", "素材库名称已存在")
            library["name"] = clean
            library["version"] += 1
            library["updated_at"] = _now_iso()
            self._touch(raw)
            return library
        return LibraryItem.model_validate(self._visible_library(self._mutate(mutator)))

    def delete_library(self, library_id: str, expected_version: int | None) -> dict[str, Any]:
        def mutator(raw: dict[str, Any]) -> Any:
            library = self._library(raw, library_id)
            from gw.core.asset_visibility import asset_visible
            if any(not asset_visible(item) for category in library.get("categories", []) for item in category.get("items", [])):
                raise CleanroomException(403, "PRIVATE_ASSET_BOUNDARY", "素材库包含其他主体私有资产，不能删除")
            self._check_version(expected_version, library["version"])
            del raw["libraries"][library_id]
            if raw.get("active_library_id") == library_id:
                raw["active_library_id"] = sorted(raw["libraries"])[0] if raw["libraries"] else None
            self._touch(raw)
            return {"library_id": library_id, "deleted": True}
        return self._mutate(mutator)

    def rename_category(self, category_id: str, name: str, expected_version: int | None) -> CategoryItem:
        def mutator(raw: dict[str, Any]) -> Any:
            library, category = self._category(raw, category_id)
            self._check_version(expected_version, category["version"])
            clean = (name or "").strip()
            if not clean:
                raise CleanroomException(400, "INVALID_REQUEST", "分类名称不能为空")
            for other in library["categories"]:
                if other["category_id"] != category_id and _name_key(other["name"]) == _name_key(clean):
                    raise CleanroomException(409, "DUPLICATE_CATEGORY_NAME", "分类名称已存在")
            category["name"] = clean
            category["version"] += 1
            category["updated_at"] = _now_iso()
            library["version"] += 1
            self._touch(raw)
            return category
        return CategoryItem.model_validate(self._visible_category(self._mutate(mutator)))

    def delete_category(self, category_id: str, expected_version: int | None) -> dict[str, Any]:
        def mutator(raw: dict[str, Any]) -> Any:
            library, category = self._category(raw, category_id)
            from gw.core.asset_visibility import asset_visible
            if any(not asset_visible(item) for item in category.get("items", [])):
                raise CleanroomException(403, "PRIVATE_ASSET_BOUNDARY", "分类包含其他主体私有资产，不能删除")
            self._check_version(expected_version, category["version"])
            library["categories"] = [c for c in library["categories"] if c["category_id"] != category_id]
            library["version"] += 1
            self._touch(raw)
            return {"category_id": category_id, "deleted": True}
        return self._mutate(mutator)

    # ------------------------------------------------------------------
    # 素材条目
    # ------------------------------------------------------------------
    def create_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        """批量创建素材条目，返回真实稳定 asset_id。"""
        def mutator(raw: dict[str, Any]) -> Any:
            library = self._library(raw, str(payload.get("library_id") or ""))
            self._check_version(payload.get("expected_version"), library["version"])
            category_id = str(payload.get("category_id") or "")
            category = None
            for candidate in library["categories"]:
                if candidate["category_id"] == category_id:
                    category = candidate
                    break
            if category is None:
                raise CleanroomException(404, "CATEGORY_NOT_FOUND", "分类不存在")
            names = payload.get("names") or []
            if not isinstance(names, list) or not names:
                raise CleanroomException(400, "INVALID_REQUEST", "names 必须是非空数组")
            stamp = _now_iso()
            created = []
            for entry in names:
                name = str(entry).strip()
                if not name:
                    raise CleanroomException(400, "INVALID_REQUEST", "素材名称不能为空")
                asset_id = f"asset_{self._next_seq(raw, 'asset'):04d}"
                item = AssetItem(
                    asset_id=asset_id, library_id=library["library_id"], category_id=category_id,
                    name=name, url=None, created_at=stamp,
                ).model_dump()
                category.setdefault("items", []).append(item)
                created.append(item)
            category["version"] += 1
            library["version"] += 1
            self._touch(raw)
            return {"items": created, "asset_ids": [item["asset_id"] for item in created]}
        return self._mutate(mutator)

    def update_item(self, asset_id: str, payload: dict[str, Any]) -> AssetItem:
        def mutator(raw: dict[str, Any]) -> Any:
            library, category, item = self._asset(raw, asset_id)
            self._check_version(payload.get("expected_version"), library["version"])
            if payload.get("name"):
                item["name"] = str(payload["name"]).strip()
            target_id = str(payload.get("category_id") or "")
            if target_id and target_id != category["category_id"]:
                target = None
                for candidate in library["categories"]:
                    if candidate["category_id"] == target_id:
                        target = candidate
                        break
                if target is None:
                    raise CleanroomException(404, "CATEGORY_NOT_FOUND", "目标分类不存在")
                category["items"] = [i for i in category["items"] if i["asset_id"] != asset_id]
                target.setdefault("items", []).append(item)
                item["category_id"] = target_id
                target["version"] += 1
            category["version"] += 1
            library["version"] += 1
            self._touch(raw)
            return item
        return AssetItem.model_validate(self._mutate(mutator))

    def delete_item(self, asset_id: str, expected_version: int | None = None) -> dict[str, Any]:
        """删除单个素材条目（普通条目原语义）。

        同进同出（C4-AC6）：工作流桥接登记条目的删除由 API 层改道注册表删除
        （见 routes_asset_library_b4），本方法只负责非桥接条目。
        """
        def mutator(raw: dict[str, Any]) -> Any:
            library, category, _ = self._asset(raw, asset_id)
            self._check_version(expected_version, library["version"])
            category["items"] = [i for i in category["items"] if i["asset_id"] != asset_id]
            category["version"] += 1
            library["version"] += 1
            self._touch(raw)
            return {"asset_id": asset_id, "deleted": True}
        return self._mutate(mutator)

    def delete_items(
        self,
        asset_ids: list[str],
        expected_version: int | None,
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """批量删除普通素材条目；所有 ID 与父库版本先在同一 mutate 内核验。

        同进同出（C4-AC6）：工作流桥接登记条目的删除由 API 层改道注册表删除
        （见 routes_asset_library_b4），本方法只负责非桥接条目。
        """
        if expected_versions is not None and not isinstance(expected_versions, dict):
            raise CleanroomException(400, "INVALID_REQUEST", "expected_versions 必须是素材库版本映射")

        def mutator(raw: dict[str, Any]) -> Any:
            locations = []
            missing = []
            for asset_id in asset_ids:
                try:
                    locations.append((str(asset_id), *self._asset(raw, str(asset_id))))
                except CleanroomException as exc:
                    if exc.status_code == 404:
                        missing.append(str(asset_id))
                    else:
                        raise
            if missing:
                raise CleanroomException(404, "ASSET_NOT_FOUND", "素材不存在",
                                         extra={"missing_asset_ids": missing},
                                         expose_extra_fields={"missing_asset_ids"})
            for _asset_id, library, _, _ in locations:
                version = (expected_versions or {}).get(str(library["library_id"]), expected_version)
                self._check_version(version, library["version"])
            removed = []
            results = []
            for asset_id, library, category, _ in locations:
                category["items"] = [i for i in category["items"] if i["asset_id"] != asset_id]
                category["version"] += 1
                library["version"] += 1
                removed.append(asset_id)
                results.append({"asset_id": asset_id, "deleted": True})
            if removed:
                self._touch(raw)
            return {"deleted": removed, "results": results, "count": len(removed)}
        return self._mutate(mutator)

    def find_item_snapshot(self, asset_id: str) -> dict[str, Any] | None:
        """只读回传 asset_id 对应条目的完整快照；不存在或对当前主体不可见时返回 None。"""
        from gw.core.asset_visibility import asset_visible
        with self._lock:
            _, _, item = self._locate(self._read_raw(), str(asset_id))
            if item is None or not asset_visible(item):
                return None
            return copy.deepcopy(item)

    def ensure_item_version(self, asset_id: str, expected_version: int | None) -> None:
        """只读校验条目所在素材库的 CAS 版本；条目不存在时静默跳过（由删除路径报 404）。"""
        if expected_version is None:
            return
        with self._lock:
            library, _, _ = self._locate(self._read_raw(), str(asset_id))
        if library is not None:
            self._check_version(expected_version, library["version"])

    def remove_item_snapshot(self, asset_id: str) -> dict[str, Any] | None:
        """原子移除并返回 asset_id 对应条目的完整快照（同进同出暂存用）。

        不存在或对当前主体不可见时返回 None（幂等，不抛异常）；
        移除会按惯例递增分类/库/目录版本。
        """
        from gw.core.asset_visibility import asset_visible

        def mutator(raw: dict[str, Any]) -> dict[str, Any] | None:
            library, category, item = self._locate(raw, str(asset_id))
            if item is None or not asset_visible(item):
                return None
            category["items"] = [i for i in category["items"] if i["asset_id"] != str(asset_id)]
            category["version"] += 1
            library["version"] += 1
            self._touch(raw)
            return copy.deepcopy(item)
        return self._mutate(mutator)

    def insert_item_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """原样插回条目快照（同 id/同库/同分类/同字段），返回是否真的插入。

        同 asset_id 条目已存在时幂等跳过；原库或原分类已被删除时不复活条目
        （资产存在性以注册表为唯一真源，见 registry.repository.delete_asset docstring）。
        """
        if not isinstance(snapshot, dict) or not snapshot.get("asset_id"):
            return False

        def mutator(raw: dict[str, Any]) -> bool:
            asset_id = str(snapshot["asset_id"])
            _, _, existing = self._locate(raw, asset_id)
            if existing is not None:
                return False
            library = raw.get("libraries", {}).get(str(snapshot.get("library_id") or ""))
            if library is None:
                return False
            target = None
            for candidate in library.get("categories", []):
                if candidate.get("category_id") == snapshot.get("category_id"):
                    target = candidate
                    break
            if target is None:
                return False
            target.setdefault("items", []).append(copy.deepcopy(snapshot))
            target["version"] += 1
            library["version"] += 1
            self._touch(raw)
            return True
        return self._mutate(mutator)

    def move_items(
        self,
        asset_ids: list[str],
        category_id: str,
        expected_version: int | None,
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if expected_versions is not None and not isinstance(expected_versions, dict):
            raise CleanroomException(400, "INVALID_REQUEST", "expected_versions 必须是素材库版本映射")

        def mutator(raw: dict[str, Any]) -> Any:
            locations = []
            missing = []
            for asset_id in asset_ids:
                try:
                    locations.append((str(asset_id), *self._asset(raw, str(asset_id))))
                except CleanroomException as exc:
                    if exc.status_code == 404:
                        missing.append(str(asset_id))
                    else:
                        raise
            if missing:
                raise CleanroomException(404, "ASSET_NOT_FOUND", "素材不存在",
                                         extra={"missing_asset_ids": missing},
                                         expose_extra_fields={"missing_asset_ids"})
            destinations = []
            for asset_id, library, category, item in locations:
                version = (expected_versions or {}).get(str(library["library_id"]), expected_version)
                self._check_version(version, library["version"])
                target = None
                for candidate in library["categories"]:
                    if candidate["category_id"] == category_id:
                        target = candidate
                        break
                if target is None:
                    raise CleanroomException(404, "CATEGORY_NOT_FOUND", "目标分类不存在")
                destinations.append((asset_id, library, category, item, target))
            moved = []
            results = []
            for asset_id, library, category, item, target in destinations:
                category["items"] = [i for i in category["items"] if i["asset_id"] != asset_id]
                target.setdefault("items", []).append(item)
                item["category_id"] = category_id
                target["version"] += 1
                library["version"] += 1
                moved.append(asset_id)
                results.append({"asset_id": asset_id, "moved": True, "category_id": category_id})
            if moved:
                self._touch(raw)
            return {"moved": moved, "results": results, "count": len(moved)}
        return self._mutate(mutator)

    def classify_items(self, asset_ids: list[str]) -> dict[str, Any]:
        """对真实条目做确定性扩展名归类并写回。"""
        from gw.asset_library.repository import classify_name

        def mutator(raw: dict[str, Any]) -> Any:
            results = []
            for asset_id in asset_ids:
                try:
                    library, category, item = self._asset(raw, asset_id)
                except CleanroomException:
                    continue
                label = classify_name(str(item.get("name") or ""))
                item["classification"] = label
                category["version"] += 1
                library["version"] += 1
                results.append({"asset_id": asset_id, "classification": label})
            if results:
                self._touch(raw)
            return {"results": results, "count": len(results)}
        return self._mutate(mutator)

    def ensure_default_category(
        self, raw: dict[str, Any], cat_type: str, default_name: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """确保存在激活素材库以及指定类型的分类。返回 (library_dict, category_dict)"""
        if not raw.get("libraries"):
            library_id = "library_default"
            stamp = _now_iso()
            raw["libraries"][library_id] = {
                "library_id": library_id,
                "name": "默认资产库",
                "version": 1,
                "created_at": stamp,
                "updated_at": stamp,
                "categories": [],
            }
            raw["active_library_id"] = library_id
            self._touch(raw)

        active_id = raw.get("active_library_id") or sorted(raw["libraries"])[0]
        library = raw["libraries"][active_id]

        for cat in library.get("categories", []):
            if cat.get("type") == cat_type:
                return library, cat

        cat_id = f"category_{cat_type}"
        stamp = _now_iso()
        category = {
            "category_id": cat_id,
            "library_id": library["library_id"],
            "name": default_name,
            "type": cat_type,
            "version": 1,
            "created_at": stamp,
            "updated_at": stamp,
            "items": [],
        }
        library.setdefault("categories", []).append(category)
        library["version"] += 1
        library["updated_at"] = stamp
        self._touch(raw)
        return library, category

    def remove_bridge_item(self, asset_id: str, owner: str):
        """仅移除指定主体的桥接投影，不删除任何原始文件。"""
        def mutate(raw):
            changed = False
            for library in raw.get("libraries", {}).values():
                for category in library.get("categories", []):
                    before = category.get("items", [])
                    after = [item for item in before if not (item.get("asset_id") == asset_id and item.get("workflow_owner") == owner)]
                    if len(after) != len(before):
                        category["items"] = after
                        category["version"] += 1
                        library["version"] += 1
                        changed = True
            if changed:
                self._touch(raw)
            return {"removed": changed}
        return self._mutate(mutate)

    def register_item(
        self,
        *,
        asset_id: str,
        name: str,
        category_type: str,
        category_name: str,
        url: str | None = None,
        workflow_owner: str | None = None,
    ) -> AssetItem:
        """登记或更新素材条目（保证全局稳定 asset_id 绑定）。"""
        def mutator(raw: dict[str, Any]) -> Any:
            library, category = self.ensure_default_category(raw, category_type, category_name)
            stamp = _now_iso()

            existing = None
            for it in category.get("items", []):
                if it.get("asset_id") == asset_id:
                    existing = it
                    break

            if existing is not None:
                if existing.get("workflow_owner") != workflow_owner:
                    raise CleanroomException(409, "ASSET_OWNER_CONFLICT", "资产归属冲突")
                if existing.get("name") == name and (url is None or existing.get("url") == url):
                    return existing
                existing["name"] = name
                if url is not None:
                    existing["url"] = url
                existing["updated_at"] = stamp
                target_item = existing
            else:
                target_item = {
                    "asset_id": asset_id,
                    "library_id": library["library_id"],
                    "category_id": category["category_id"],
                    "name": name,
                    "url": url,
                    "created_at": stamp,
                    "workflow_owner": workflow_owner,
                }
                category.setdefault("items", []).append(target_item)

            category["version"] += 1
            category["updated_at"] = stamp
            library["version"] += 1
            library["updated_at"] = stamp
            self._touch(raw)
            return target_item

        result = self._mutate(mutator)
        return AssetItem.model_validate(result)


# 全局单例服务实例
default_asset_library_service = AssetLibraryService(seed_golden_fixture=False)
