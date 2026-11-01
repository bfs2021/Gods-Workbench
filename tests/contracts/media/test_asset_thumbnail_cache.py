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

"""QIN-508 素材缩略图批量生成与缓存目录契约测试。

覆盖前端真实调用的批量形状（asset_ids 数组）、真实计数、命中复用、
设置项 cache_folder 生效、custom 模式失败关闭、删除精确清理与后台任务计数。
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
    media_repo.patch_thumbnail_settings(
        {
            "mode": "source",
            "directory": "",
            "cache_folder": "god-cache",
            "width": 256,
            "format": "webp",
        }
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


def _three_assets(runtime: dict[str, Path]) -> list[str]:
    return [
        _register_asset(runtime, "one.png", _png(320, 180, (10, 20, 30))),
        _register_asset(runtime, "two.png", _png(160, 90, (40, 50, 60))),
        _register_asset(runtime, "three.png", _png(640, 360, (70, 80, 90))),
    ]


def _cache_files(runtime: dict[str, Path], folder: str = "god-cache") -> list[Path]:
    base = runtime["data"] / "media_output" / folder
    if not base.exists():
        return []
    return sorted(
        path
        for path in base.rglob("*")
        if path.is_file() and path.name.endswith((".jpg.png", ".jxl", ".webp"))
    )


def test_batch_generate_accepts_asset_ids_and_reports_true_counts(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """前端真实形状（asset_ids 数组）必须被接受，并逐项返回真实结果。"""
    ids = _three_assets(media_runtime)

    result = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert result["generated"] == 3, result
    assert result["cached"] == 0 and result["failed"] == 0
    assert [item["status"] for item in result["items"]] == ["generated"] * 3
    assert len(result["outputs"]) == 3
    files = _cache_files(media_runtime)
    assert len(files) == 3, files
    for item in result["items"]:
        assert item["output"].startswith("media_output/god-cache/")
        assert (media_runtime["data"] / item["output"]).is_file()


def test_second_generate_hits_cache_without_recomputing(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """缓存已存在且不早于源文件时计入 cached，产物 mtime 不变。"""
    ids = _three_assets(media_runtime)
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    before = {path.name: path.stat().st_mtime_ns for path in _cache_files(media_runtime)}

    result = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert result["generated"] == 0 and result["cached"] == 3, result
    after = {path.name: path.stat().st_mtime_ns for path in _cache_files(media_runtime)}
    assert after == before


def test_replace_existing_forces_recompute(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """replace_existing=true 时必须重算，不能被 cached 短路。"""
    ids = _three_assets(media_runtime)
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    before = {path.name: path.stat().st_mtime_ns for path in _cache_files(media_runtime)}

    result = media_repo.generate_thumbnail(
        {"asset_ids": ids, "width": 256, "replace_existing": True}
    )

    assert result["generated"] == 3 and result["cached"] == 0, result
    after = {path.name: path.stat().st_mtime_ns for path in _cache_files(media_runtime)}
    assert all(after[name] > before[name] for name in after)


def test_partial_failure_keeps_other_items_and_reports_failed(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """单项失败（不存在的素材）不得中断整批，也不得伪装成功。"""
    ids = _three_assets(media_runtime)
    mixed = [ids[0], "ast_does_not_exist", ids[1]]

    result = media_repo.generate_thumbnail({"asset_ids": mixed, "width": 128})

    assert result["generated"] == 2 and result["failed"] == 1, result
    assert result["data_status"] == "partial"
    failed = [item for item in result["items"] if item["status"] == "failed"]
    assert failed and failed[0]["error"]["code"]


def test_cache_folder_setting_changes_real_path(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """设置页 cache_folder 必须真实参与落盘路径，而不是只回显。"""
    ids = _three_assets(media_runtime)
    media_repo.patch_thumbnail_settings({"cache_folder": "custom-cache"})

    result = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert result["generated"] == 3
    assert all(item["output"].startswith("media_output/custom-cache/") for item in result["items"])
    assert len(_cache_files(media_runtime, "custom-cache")) == 3
    assert _cache_files(media_runtime, "god-cache") == []


def test_custom_mode_rejects_directory_outside_allowed_roots(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """指定目录不在允许根内必须失败关闭，且不产生任何缓存文件。"""
    ids = _three_assets(media_runtime)
    outside = tmp_path / "outside"
    outside.mkdir()
    media_repo.patch_thumbnail_settings(
        {"mode": "custom", "directory": str(outside), "cache_folder": "god-cache"}
    )

    with pytest.raises(CleanroomException) as error:
        media_repo.generate_thumbnail({"asset_ids": ids[:1], "width": 256})

    assert error.value.status_code == 403
    assert _cache_files(media_runtime) == []


def test_custom_mode_accepts_registered_directory(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """指定目录在允许根内时按该目录分桶，缓存仍落在受管产物目录内。"""
    ids = _three_assets(media_runtime)
    media_repo.patch_thumbnail_settings(
        {"mode": "custom", "directory": str(media_runtime["roots"])}
    )

    result = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    assert result["generated"] == 3
    bucket = result["items"][0]["output"].split("/")[2]
    assert bucket.startswith("root_")
    assert all(item["output"].split("/")[2] == bucket for item in result["items"])


def test_delete_removes_layered_cache_and_regenerates(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """删除必须精确命中分层缓存，删后再生成 cached 归零。"""
    ids = _three_assets(media_runtime)
    media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})

    removed = media_repo.delete_outputs("thumbnails", {"asset_ids": ids})

    assert removed["removed"] == 3, removed
    assert _cache_files(media_runtime) == []
    again = media_repo.generate_thumbnail({"asset_ids": ids, "width": 256})
    assert again["generated"] == 3 and again["cached"] == 0


def test_background_job_reports_true_total(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """后台任务 progress 与 result 必须反映真实批量数量。"""
    ids = _three_assets(media_runtime)

    job_id, poll_hint = media_repo.run_background_thumbnail(
        {"asset_ids": ids, "width": 256}
    )

    job = media_repo.get_job(job_id)
    assert poll_hint == "/api/asset-thumbnails/jobs/%s" % job_id
    assert job["status"] == "succeeded"
    assert job["progress"] == {"processed": 3, "total": 3}
    assert job["result"]["generated"] == 3


def test_single_path_payload_still_supported(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """无 asset_id 的显式 path 调用保持兼容，并按宽度隔离文件名。"""
    source = media_runtime["roots"] / "lonely.png"
    source.write_bytes(_png(200, 100, (1, 2, 3)))

    small = media_repo.generate_thumbnail({"path": str(source), "width": 128})
    large = media_repo.generate_thumbnail({"path": str(source), "width": 256})

    assert small["generated"] == 1 and small["failed"] == 0
    assert Path(small["output"]).name.endswith("-w0128.webp")
    assert Path(large["output"]).name.endswith("-w0256.webp")
    assert small["output"] != large["output"]
