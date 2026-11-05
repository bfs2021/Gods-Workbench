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

"""QIN-553：预置 LUT 包的内置登记（幂等 ensure）。

仓库内 ``web/vendor/luts/`` 只读分发合法 ``.cube``；首次进入 LUT 库时前端
调用 ``ensure_builtin_luts``，服务端把文件复制到数据根受管目录
``registry_uploads/builtin_luts/`` 并以稳定 ``asset_id`` 登记为
``metadata.builtin=true`` 的 LUT 资产。重复启动/重复调用不产生重复登记、
不推高注册表版本；数据根副本缺失时按源文件自动补物化。用户可隐藏内置
条目，但不能移入回收站或删除源文件（守卫见 repository 与路由层）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from gw.asset_registry.lut_cube import LutCubeError, parse_lut_cube_bytes
from gw.core import storage
from gw.core.errors import CleanroomException

BUILTIN_LUT_ASSET_ID_PREFIX = "builtin_lut_"
BUILTIN_LUT_RELATIVE_DIR = Path("web") / "vendor" / "luts"
BUILTIN_LUT_UPLOAD_SUBDIR = "builtin_luts"
BUILTIN_LUT_NOTES = (
    "程序生成的演示级基准 LUT（Apache-2.0, 2026 Gods-Workbench Authors）；"
    "来源与规格见 web/vendor/luts/README.md"
)

# (slug, 展示名, 文件名, 通道公式)——与 web/vendor/luts/ 同名一一对应。
BUILTIN_LUT_SPECS: List[Dict[str, str]] = [
    {"slug": "neutral", "name": "中性基准", "file": "builtin_lut_neutral.cube",
     "formula": "identity"},
    {"slug": "warm", "name": "暖调", "file": "builtin_lut_warm.cube",
     "formula": "R*1.06 G*1.01 B*0.93"},
    {"slug": "cool", "name": "冷调", "file": "builtin_lut_cool.cube",
     "formula": "R*0.93 G*1.00 B*1.07"},
    {"slug": "high_contrast", "name": "高对比", "file": "builtin_lut_high_contrast.cube",
     "formula": "smoothstep(0.08,0.92,x)"},
    {"slug": "cinematic", "name": "电影感", "file": "builtin_lut_cinematic.cube",
     "formula": "lifted-blacks + rolled-highlights"},
]


def _package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def source_builtin_cube_path(filename: str) -> Path:
    """仓库内只读分发源路径；文件名必须是登记的单段 .cube 名。"""
    name = str(filename or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."} \
            or not name.lower().endswith(".cube"):
        raise CleanroomException(400, "INVALID_REQUEST", "内置 LUT 文件名无效")
    return _package_root() / BUILTIN_LUT_RELATIVE_DIR / name


def materialized_builtin_lut_dir() -> Path:
    return storage.data_root() / "registry_uploads" / BUILTIN_LUT_UPLOAD_SUBDIR


def is_builtin_lut_display_path(display: Any) -> bool:
    normalized = str(display or "").replace("\\", "/")
    return normalized.startswith(f"registry_uploads/{BUILTIN_LUT_UPLOAD_SUBDIR}/")


def _materialize(spec: Dict[str, str]) -> Dict[str, Any]:
    """把仓库源文件复制到数据根受管目录并解析 Cube 头部；返回可入库字段。

    失败关闭：源缺失或非法 Cube 一律 503，不写半成品；已存在且字节一致的
    副本直接复用。复制在注册表事务前完成，复制本身幂等。
    """
    source = source_builtin_cube_path(spec["file"])
    if not source.is_file():
        raise CleanroomException(503, "BUILTIN_LUT_SOURCE_MISSING",
                                 f"内置 LUT 源文件缺失：{spec['file']}")
    data = source.read_bytes()
    try:
        lut_meta = parse_lut_cube_bytes(data)
    except LutCubeError as exc:
        raise CleanroomException(503, "BUILTIN_LUT_SOURCE_INVALID",
                                 f"内置 LUT 源文件非法：{spec['file']}") from exc
    directory = materialized_builtin_lut_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / spec["file"]
    if not target.is_file() or target.read_bytes() != data:
        tmp = directory / (spec["file"] + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise CleanroomException(503, "BUILTIN_LUT_MATERIALIZE_FAILED",
                                     "内置 LUT 物化到数据根失败")
    return {
        "asset_id": BUILTIN_LUT_ASSET_ID_PREFIX + spec["slug"],
        "name": spec["file"],
        "display_path": f"registry_uploads/{BUILTIN_LUT_UPLOAD_SUBDIR}/{spec['file']}",
        "size_bytes": len(data),
        "metadata": {
            "builtin": True,
            "builtin_slug": spec["slug"],
            "builtin_formula": spec["formula"],
            "notes": BUILTIN_LUT_NOTES,
            "lut": {
                "title": lut_meta["title"],
                "lut_3d_size": lut_meta.get("lut_3d_size"),
                "lut_1d_size": lut_meta.get("lut_1d_size"),
                "sample_count": lut_meta["sample_count"],
            },
            "extension": "cube",
        },
    }


def _validate_existing(existing: Dict[str, Any], item: Dict[str, Any]) -> None:
    """已登记条目的身份一致性守卫：被篡改或 id 冲突时失败关闭，不静默覆盖。"""
    asset_id = item["asset_id"]
    metadata = existing.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("builtin") is not True:
        raise CleanroomException(503, "BUILTIN_LUT_IDENTITY_CONFLICT",
                                 f"内置 LUT 资产 id 已被占用：{asset_id}")
    if existing.get("kind") != "lut" or \
            str(existing.get("display_path") or "") != item["display_path"]:
        raise CleanroomException(503, "BUILTIN_LUT_IDENTITY_CONFLICT",
                                 f"内置 LUT 资产路径或类型漂移：{asset_id}")


def ensure_builtin_luts(reveal: bool = False) -> Dict[str, Any]:
    """幂等登记全部内置 LUT：补缺失条目与缺失副本，不动用户隐藏态。

    - 首次调用逐条登记（``created`` 返回新增 id 列表）；
    - 重复调用全部命中已登记分支：不新增、不推高注册表 revision；
    - 已登记条目只校验身份一致（builtin 标记/kind/路径），保留
      hidden、tags、version 等用户态；
    - ``reveal=True`` 时把全部内置条目重新设为显示（「恢复显示内置 LUT」）。
    """
    from gw.asset_registry import repository as repo

    prepared = [_materialize(spec) for spec in BUILTIN_LUT_SPECS]
    created: List[str] = []
    revealed: List[str] = []
    hidden: List[str] = []

    def mutate(raw: Dict[str, Any]) -> Any:
        assets = raw.setdefault("assets", {})
        for item in prepared:
            asset_id = item["asset_id"]
            existing = assets.get(asset_id)
            if existing is not None:
                _validate_existing(existing, item)
                if reveal and existing.get("hidden"):
                    existing["hidden"] = False
                    existing["version"] = int(existing.get("version", 1)) + 1
                    existing["updated_at"] = storage.now_iso()
                    revealed.append(asset_id)
                elif existing.get("hidden"):
                    hidden.append(asset_id)
                continue
            assets[asset_id] = {
                "id": asset_id,
                "asset_id": asset_id,
                "name": item["name"],
                "kind": "lut",
                "archived": False,
                "hidden": False,
                "builtin": True,
                "version": 1,
                "created_at": storage.now_iso(),
                "updated_at": storage.now_iso(),
                "tags": [],
                "relations": [],
                "display_path": item["display_path"],
                "size_bytes": item["size_bytes"],
                "metadata": item["metadata"],
            }
            raw.setdefault("sequences", {})[asset_id] = True
            created.append(asset_id)
        changed = bool(created) or bool(revealed)
        revision = repo._bump(raw) if changed else int(raw.get("revision", 1))
        return {"revision": revision}

    result = repo.state().mutate(mutate)
    all_ids = [item["asset_id"] for item in prepared]
    return {
        "created": created,
        "existing": [asset_id for asset_id in all_ids if asset_id not in created],
        "revealed": revealed,
        "hidden": hidden,
        "revision": result["revision"],
        "data_status": "ok",
        "data_gaps": [],
    }
