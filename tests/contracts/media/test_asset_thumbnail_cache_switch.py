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

"""QIN-515 缓存目录切换：旧缓存迁移 / 删除 / 保留的契约测试。

覆盖确认弹窗的三个分支（迁移、删除、取消=keep）、跨目录迁移后的相对结构、
只清理缓存产物不动其他文件、越界与嵌套目录失败关闭。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from gw.asset_registry import repository as asset_repo
from gw.core.errors import CleanroomException
from gw.media import repository as media_repo


@pytest.fixture(autouse=True)
def media_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_root = tmp_path / "gw-data"
    allowed_root = tmp_path / "roots"
    data_root.mkdir()
    allowed_root.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed_root))
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(tmp_path / "gw-auth" / "auth.sqlite3"))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(tmp_path / "gw-video"))
    media_repo.patch_thumbnail_settings(
        {"mode": "source", "directory": "", "cache_folder": "god-cache", "width": 256}
    )
    return {"data": data_root, "roots": allowed_root}


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _register_asset(runtime: dict[str, Path], name: str, payload: bytes) -> str:
    path = runtime["roots"] / name
    path.write_bytes(payload)
    created = asset_repo.import_assets([{"name": name, "kind": "image"}], "image", None)
    asset_id = created["asset_ids"][0]
    asset_repo.update_asset(
        asset_id,
        {"display_path": str(path), "expected_version": created["revision"]},
    )
    return asset_id


def _source_cache_root(runtime: dict[str, Path]) -> Path:
    return runtime["data"] / "media_output" / "god-cache"


def _generate(runtime: dict[str, Path], count: int = 2) -> list[str]:
    ids = [
        _register_asset(runtime, f"pic-{index}.png", _png(320, 180, (index * 20, 30, 40)))
        for index in range(count)
    ]
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    return ids


CACHE_EXTENSIONS = (".jpg.png", ".jxl", ".webp")


def _cache_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith(CACHE_EXTENSIONS)
    )


def test_migrate_moves_cache_tree_to_new_directory(
    media_runtime: dict[str, Path],
) -> None:
    """迁移把原缓存按相对路径搬到新目录，并清掉空的旧缓存根。"""
    _generate(media_runtime)
    old_root = _source_cache_root(media_runtime)
    before = _cache_files(old_root)
    assert before, "前置条件：source 模式应有缓存产物"
    target = media_runtime["roots"] / "cache-new"
    target.mkdir()

    result = media_repo.switch_thumbnail_cache(
        {"mode": "custom", "directory": str(target), "cache_action": "migrate"}
    )

    assert result["cache_action"] == "migrate"
    assert result["cache_moved"] == len(before)
    assert result["cache_deleted"] == 0
    new_root = target / "god-cache"
    assert len(_cache_files(new_root)) == len(before)
    # 相对结构（源目录分桶）保持，旧缓存根被清理到不残留。
    assert {path.relative_to(new_root) for path in _cache_files(new_root)} == {
        path.relative_to(old_root) for path in before
    }
    assert not old_root.exists()
    assert media_repo.get_thumbnail_settings()["mode"] == "custom"


def test_delete_removes_only_cache_artifacts(
    media_runtime: dict[str, Path],
) -> None:
    """删除只清缓存产物，同目录里的其他文件原样保留。"""
    _generate(media_runtime)
    old_root = _source_cache_root(media_runtime)
    assert _cache_files(old_root)
    keep = old_root / "keep-me.txt"
    keep.write_text("not a cache artifact", encoding="utf-8")
    target = media_runtime["roots"] / "cache-new"
    target.mkdir()

    result = media_repo.switch_thumbnail_cache(
        {"mode": "custom", "directory": str(target), "cache_action": "delete"}
    )

    assert result["cache_deleted"] > 0
    assert _cache_files(old_root) == []
    assert keep.is_file(), "非缓存文件不能被删除"
    assert _cache_files(target / "god-cache") == []


def test_keep_switches_settings_without_touching_old_cache(
    media_runtime: dict[str, Path],
) -> None:
    """取消（keep）只切设置，旧缓存原地保留，供调用方后续自行处理。"""
    _generate(media_runtime)
    old_root = _source_cache_root(media_runtime)
    before = _cache_files(old_root)

    result = media_repo.switch_thumbnail_cache(
        {
            "mode": "custom",
            "directory": str(media_runtime["roots"]),
            "cache_action": "keep",
        }
    )

    assert result["cache_moved"] == 0 and result["cache_deleted"] == 0
    assert _cache_files(old_root) == before
    assert media_repo.get_thumbnail_settings()["directory"] == str(
        media_runtime["roots"]
    )


def test_switch_rejects_nested_directories(
    media_runtime: dict[str, Path],
) -> None:
    """新旧目录互相包含时拒绝迁移，避免边搬边读自己的工作区。"""
    _generate(media_runtime, count=1)
    nested = media_runtime["data"] / "media_output"
    with pytest.raises(CleanroomException) as error:
        media_repo.switch_thumbnail_cache(
            {"mode": "custom", "directory": str(nested), "cache_action": "migrate"}
        )
    assert error.value.code in {"CACHE_DIRECTORY_OVERLAP", "CACHE_DIRECTORY_NOT_ALLOWED"}


def test_switch_rejects_unknown_action(media_runtime: dict[str, Path]) -> None:
    with pytest.raises(CleanroomException) as error:
        media_repo.switch_thumbnail_cache({"cache_action": "move"})
    assert error.value.status_code == 400
    assert error.value.code == "INVALID_CACHE_ACTION"


def test_switch_outside_allowed_roots_keeps_settings_and_cache(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """新目录未准入：失败关闭，设置与旧缓存都不动。"""
    _generate(media_runtime)
    old_root = _source_cache_root(media_runtime)
    before = _cache_files(old_root)
    outside = tmp_path / "outside-cache"
    outside.mkdir()

    with pytest.raises(CleanroomException) as error:
        media_repo.switch_thumbnail_cache(
            {"mode": "custom", "directory": str(outside), "cache_action": "delete"}
        )

    assert error.value.status_code == 403
    assert error.value.code == "CACHE_DIRECTORY_NOT_ALLOWED"
    assert media_repo.get_thumbnail_settings()["mode"] == "source"
    assert _cache_files(old_root) == before
    assert list(outside.rglob("*")) == []


def test_cache_artifact_name_covers_every_thumbnail_format() -> None:
    """迁移 / 清理只认可识别的产物名，三种缩略图格式与视频预览都必须覆盖。"""
    for name in (
        "ast_0001.jpg.png",
        "ast_0001.jxl",
        "ast_0001.webp",
        "ast_0001-w0256-0123456789abcdef.frame.jpg",
    ):
        assert media_repo.is_cache_artifact_name(name), name
    for name in ("ast_0001.mp4", "ast_0001.jpg", "proxy.mp4"):
        assert not media_repo.is_cache_artifact_name(name), name


def test_cache_artifact_name_covers_every_thumbnail_format() -> None:
    """迁移 / 清理只认可识别的产物名，三种缩略图格式与视频预览都必须覆盖。"""
    for name in (
        "ast_0001.jpg.png",
        "ast_0001.jxl",
        "ast_0001.webp",
        "ast_0001-w0256-0123456789abcdef.frame.jpg",
    ):
        assert media_repo.is_cache_artifact_name(name), name
    for name in ("ast_0001.mp4", "ast_0001.jpg", "proxy.mp4"):
        assert not media_repo.is_cache_artifact_name(name), name
