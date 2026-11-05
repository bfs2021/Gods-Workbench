"""统一资产中枢（asset_registry）磁盘文件夹整理增量索引同步。

为 gw.asset_library.organize 提供增量索引同步监听能力，解除 asset_library 与 asset_registry 的循环依赖。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gw.asset_library.organize import OrganizationSyncListener
from gw.asset_registry import repository as asset_reg_repo
from gw.core import storage
from gw.core.errors import CleanroomException

logger = logging.getLogger(__name__)


def _matches_root(asset: dict[str, Any], target_root_id: str) -> bool:
    """比较条目记录中的根身份与目标根身份（兼顾 SHA-256 与 root_identity_id）。

    若条目未显式登记根身份，尝试从 path/abs_path 物理探测所属根；
    若仍无法明确匹配目标根，坚决返回 False，杜绝在多挂载根环境下误匹配所有根。
    """
    entry_root = asset.get("storage_root_id") or asset.get("root_id") or asset.get("_root_identity")
    if entry_root:
        str_entry = str(entry_root)
        str_target = str(target_root_id)
        if str_entry == str_target:
            return True
        return storage.coerce_root_id(str_entry) == storage.coerce_root_id(str_target)

    file_path = asset.get("path") or asset.get("abs_path") or asset.get("file_path")
    if file_path:
        try:
            detected_rid, _ = storage.location_identity(Path(file_path))
            if detected_rid:
                return storage.coerce_root_id(detected_rid) == storage.coerce_root_id(target_root_id)
        except (CleanroomException, ValueError, OSError) as exc:
            logger.warning("organize_sync: 无法解析条目物理路径归属 %s: %s", file_path, exc)
    return False


class AssetRegistryOrganizeListener(OrganizationSyncListener):
    """统一资产注册表的增量索引同步监听器。"""

    def on_prefix_updated(
        self, root_id: str, old_path: Path, new_path: Path, is_dir: bool = False
    ) -> int:
        try:
            old_canonical = storage.location_identity(old_path)[1]
            new_canonical = storage.location_identity(new_path)[1]
            old_disp = storage.relative_display(old_path)
            new_disp = storage.relative_display(new_path)
        except (CleanroomException, ValueError, OSError) as exc:
            logger.warning("organize_sync on_prefix_updated 获取路径身份失败: %s", exc)
            return 0

        def mutate_registry(raw: dict[str, Any]) -> int:
            assets = raw.get("assets", {})
            changed = False
            count = 0
            for _asset_id, asset in assets.items():
                if not _matches_root(asset, root_id):
                    continue
                canonical = asset.get("canonical_relpath") or ""
                if is_dir:
                    if canonical == old_canonical or canonical.startswith(old_canonical + "/"):
                        tail = canonical[len(old_canonical):]
                        asset["canonical_relpath"] = new_canonical + tail
                        disp = asset.get("display_path") or ""
                        if disp.startswith(old_disp):
                            asset["display_path"] = new_disp + disp[len(old_disp):]
                        changed = True
                        count += 1
                else:
                    if canonical == old_canonical:
                        asset["canonical_relpath"] = new_canonical
                        asset["display_path"] = new_disp
                        asset["name"] = new_path.name
                        changed = True
                        count += 1
            if changed:
                asset_reg_repo._bump(raw)
            return count

        return asset_reg_repo.state().mutate(mutate_registry)

    def on_paths_removed(
        self, root_id: str, target: Path, is_dir: bool = False
    ) -> list[dict[str, Any]]:
        try:
            target_canonical = storage.location_identity(target)[1]
        except (CleanroomException, ValueError, OSError) as exc:
            logger.warning("organize_sync on_paths_removed 获取路径身份失败: %s", exc)
            return []

        stashed_registry_entries: list[dict[str, Any]] = []

        def mutate_registry(raw: dict[str, Any]) -> None:
            assets = raw.get("assets", {})
            to_del = []
            for aid, asset in assets.items():
                if not _matches_root(asset, root_id):
                    continue
                canonical = asset.get("canonical_relpath") or ""
                if is_dir:
                    if canonical == target_canonical or canonical.startswith(target_canonical + "/"):
                        to_del.append(aid)
                        stashed_registry_entries.append(dict(asset))
                else:
                    if canonical == target_canonical:
                        to_del.append(aid)
                        stashed_registry_entries.append(dict(asset))
            for aid in to_del:
                del assets[aid]
            if to_del:
                asset_reg_repo._bump(raw)

        asset_reg_repo.state().mutate(mutate_registry)
        return stashed_registry_entries

    def on_paths_restored(
        self, root_id: str, stashed_data: Any
    ) -> None:
        if not isinstance(stashed_data, list) or not stashed_data:
            return

        def mutate_registry(raw: dict[str, Any]) -> None:
            assets = raw.get("assets", {})
            for asset in stashed_data:
                aid = str(asset.get("asset_id") or "")
                if aid:
                    assets[aid] = asset
            asset_reg_repo._bump(raw)

        asset_reg_repo.state().mutate(mutate_registry)
