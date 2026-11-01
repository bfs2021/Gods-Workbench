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

"""QIN-515 自定义缓存根真实落盘契约测试。

覆盖 AC1（真实写入所选目录）、AC2（命中复用与强制重算）、AC3（越界用缓存专用码
失败关闭且零写入）、AC4（登记允许根后自动重算成功）、AC9（删除精确命中当前根）。
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from gw.asset_registry import repository as asset_repo
from gw.core.errors import CleanroomException
from gw.media import repository as media_repo
from gw.settings.models import StorageAllowedRootRequest
from gw.settings.service import StorageAllowedRootsService


@pytest.fixture(autouse=True)
def media_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """隔离数据目录与允许根目录；返回 root 映射供断言使用。"""
    data_root = tmp_path / "gw-data"
    allowed_root = tmp_path / "roots"
    data_root.mkdir()
    allowed_root.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed_root))
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(tmp_path / "gw-auth" / "auth.sqlite3"))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(tmp_path / "gw-video"))
    # 显式钉死 webp：QIN-513 之后默认格式是 jxl，CI 镜像的 Pillow 无 jxl 编码器，
    # 不钉格式会因环境差异导致「生成 0 张」的假失败。断言本身不锁扩展名。
    media_repo.patch_thumbnail_settings(
        {
            "mode": "source",
            "directory": "",
            "cache_folder": "god-cache",
            "format": "webp",
            "width": 256,
        }
    )
    return {"data": data_root, "roots": allowed_root}


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _three_assets(runtime: dict[str, Path]) -> list[str]:
    ids: list[str] = []
    for index, name in enumerate(("one.png", "two.png", "three.png")):
        path = runtime["roots"] / name
        path.write_bytes(_png(320, 180, (10 * index, 20, 30)))
        created = asset_repo.import_assets([{"name": name, "kind": "image"}], "image", None)
        asset_id = created["asset_ids"][0]
        asset_repo.update_asset(
            asset_id,
            {"display_path": str(path), "expected_version": created["revision"]},
        )
        ids.append(asset_id)
    return ids


# 缩略图产物扩展名由 QIN-513 的 format 设置决定（默认 jxl），断言只锁「每个素材
# 恰好一个产物」，不锁具体格式，避免新增格式时整批返工。
CACHE_EXTENSIONS = (".jpg.png", ".jxl", ".webp")


def _cache_name(path: Path) -> str:
    """去掉格式后缀，只留素材身份部分（ast_0001.jpg.png / ast_0001.jxl → ast_0001）。"""
    for extension in CACHE_EXTENSIONS:
        if path.name.endswith(extension):
            return path.name[: -len(extension)]
    return path.name


def _managed_files(runtime: dict[str, Path], folder: str = "god-cache") -> list[Path]:
    base = runtime["data"] / "media_output" / folder
    if not base.exists():
        return []
    return sorted(
        path
        for path in base.rglob("*")
        if path.is_file() and path.name.endswith(CACHE_EXTENSIONS)
    )


def _custom_files(directory: Path, folder: str = "god-cache") -> list[Path]:
    base = directory / folder
    if not base.exists():
        return []
    return sorted(
        path
        for path in base.glob("*")
        if path.is_file() and path.name.endswith(CACHE_EXTENSIONS)
    )


def test_custom_root_writes_into_selected_directory(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """AC1：custom 模式的产物必须真实落在 <所选目录>/<cache_folder> 下。"""
    ids = _three_assets(media_runtime)
    custom = media_runtime["roots"] / "cache-home"
    custom.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(custom)})

    result = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert result["generated"] == 3, result
    written = _custom_files(custom)
    assert [_cache_name(path) for path in written] == sorted(ids)
    assert {path.parent for path in written} == {custom / "god-cache"}
    assert _managed_files(media_runtime) == []


def test_custom_root_cache_is_reused_and_replace_forces_recompute(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """AC2：同批再生成计入 cached 且 mtime 不变；replace_existing 重算并更新 mtime。"""
    ids = _three_assets(media_runtime)
    custom = media_runtime["roots"] / "cache-home"
    custom.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(custom)})
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    before = {path.name: path.stat().st_mtime_ns for path in _custom_files(custom)}

    cached = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert cached["cached"] == 3 and cached["generated"] == 0, cached
    assert {path.name: path.stat().st_mtime_ns for path in _custom_files(custom)} == before

    time.sleep(0.01)
    replaced = media_repo.generate_thumbnail(
        {"asset_ids": ids, "width": 256, "replace_existing": True}
    )

    assert replaced["generated"] == 3 and replaced["cached"] == 0, replaced
    after = {path.name: path.stat().st_mtime_ns for path in _custom_files(custom)}
    assert all(after[name] > before[name] for name in after)


def test_custom_root_outside_allowed_roots_fails_closed(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """AC3：目录不在允许根内时用缓存专用码 403，磁盘零写入。"""
    ids = _three_assets(media_runtime)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(outside)})

    with pytest.raises(CleanroomException) as error:
        media_repo.generate_thumbnail({"asset_ids": ids[:1], "width": 256})

    assert error.value.status_code == 403
    assert error.value.code == "CACHE_DIRECTORY_NOT_ALLOWED"
    assert list(outside.rglob("*")) == []
    assert _managed_files(media_runtime) == []


def test_custom_root_without_allowed_roots_uses_cache_error(
    media_runtime: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3：系统未配置允许根同样返回缓存专用码，而不是通用未准入码。"""
    ids = _three_assets(media_runtime)
    target = tmp_path / "no-roots-cache"
    target.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(target)})
    settings = media_repo.get_thumbnail_settings()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", "")

    with pytest.raises(CleanroomException) as error:
        media_repo.resolve_cache_directory(settings, media_runtime["roots"] / "one.png")

    assert error.value.status_code == 403
    assert error.value.code == "CACHE_DIRECTORY_NOT_ALLOWED"
    assert list(target.rglob("*")) == []
    assert ids  # 素材本身不是本用例关注点，仅确保注册表已就绪


def test_registering_allowed_root_then_retry_succeeds(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """AC4：管理员登记该目录后 revision 递增，原参数重试即写入成功。"""
    ids = _three_assets(media_runtime)
    granted = tmp_path / "granted-cache"
    granted.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(granted)})
    with pytest.raises(CleanroomException) as error:
        media_repo.generate_thumbnail({"asset_ids": ids[:1], "width": 256})
    assert error.value.code == "CACHE_DIRECTORY_NOT_ALLOWED"

    service = StorageAllowedRootsService()
    before = service.get_snapshot()
    updated = service.add_root(
        StorageAllowedRootRequest(
            path=str(granted), expected_version=int(before.revision)
        )
    )

    assert updated.revision > before.revision
    assert any(root.path and Path(root.path) == granted.resolve() for root in updated.roots)

    result = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert result["generated"] == 3, result
    assert len(_custom_files(granted)) == 3
    assert _managed_files(media_runtime) == []


def test_delete_only_touches_current_cache_root(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """AC9：删除只命中当前模式的根，切换模式后不误删另一根下的同名产物。"""
    ids = _three_assets(media_runtime)
    custom = media_runtime["roots"] / "cache-home"
    custom.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(custom)})
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    assert len(_custom_files(custom)) == 3

    media_repo.patch_thumbnail_settings({"mode": "source", "directory": ""})
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    assert len(_managed_files(media_runtime)) == 3

    managed_removed = media_repo.delete_outputs("thumbnails", {"asset_ids": ids})

    assert managed_removed["removed"] == 3, managed_removed
    assert _managed_files(media_runtime) == []
    # 自定义根下的同名产物必须原样保留。
    assert len(_custom_files(custom)) == 3

    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(custom)})
    custom_removed = media_repo.delete_outputs("thumbnails", {"asset_ids": ids})

    assert custom_removed["removed"] == 3, custom_removed
    assert _custom_files(custom) == []
