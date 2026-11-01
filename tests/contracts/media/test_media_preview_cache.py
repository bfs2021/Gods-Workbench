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

"""QIN-509/513 预览缓存命中与 JXL/WebP 格式契约测试。

覆盖：预生成产物被预览直接复用（不重新解码源文件）、未命中落盘后二次命中、
源文件替换失效、JXL/WebP 格式切换及旧格式清理、坏缓存降级修复、宽度独立、
metadata descriptor 不受影响、500 项规模命中率探针、视频抽帧缓存与降级。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.asset_registry import repository as asset_repo
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


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
    media_repo.reset_preview_cache_stats()
    return {"data": data_root, "roots": allowed_root}


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
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


def _cache_files(runtime: dict[str, Path], folder: str = "god-cache") -> list[Path]:
    base = runtime["data"] / "media_output" / folder
    return sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []


def _count_decodes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """给源文件解码加计数探针，用来证明命中路径没有重新解码源文件。"""
    counter = {"calls": 0}
    original = media_repo._thumbnail_bytes

    def wrapped(path: Path, width: int, *, image_format: str = "png") -> bytes:
        counter["calls"] += 1
        return original(path, width, image_format=image_format)

    monkeypatch.setattr(media_repo, "_thumbnail_bytes", wrapped)
    return counter


def _stub_jxl_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media_repo, "_encode_jxl_thumbnail", lambda _png: b"\xff\x0aQIN-513-JXL"
    )


def test_preview_serves_pregenerated_thumbnail_without_decoding_source(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1：已生成的缩略图必须被预览直接复用，且不重新解码源文件。"""
    asset_id = _register_asset(media_runtime, "shot.png", _png(320, 180, (12, 34, 56)))
    generated = media_repo.generate_thumbnail({"asset_id": asset_id, "width": 256})
    stored = (media_runtime["data"] / generated["output"]).read_bytes()

    counter = _count_decodes(monkeypatch)
    result = media_repo.preview_bytes({"asset_id": asset_id, "w": 256})

    assert counter["calls"] == 0, "命中预生成缩略图时不得再解码源文件"
    assert result["bytes"] == stored
    assert result["media_type"] == "image/webp"
    assert media_repo.preview_cache_stats()["hit"] == 1


def test_preview_generates_on_miss_then_hits_cache(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2：未命中按需生成并落盘，第二次相同请求直接命中且字节一致。"""
    source = media_runtime["roots"] / "fresh.png"
    source.write_bytes(_png(400, 200, (90, 100, 110)))
    counter = _count_decodes(monkeypatch)

    first = media_repo.preview_bytes({"path": str(source), "w": 256})
    assert counter["calls"] == 1
    assert first["media_type"] == "image/webp"
    assert first["bytes"].startswith(b"RIFF") and first["bytes"][8:12] == b"WEBP"
    files = _cache_files(media_runtime)
    assert len(files) == 1 and files[0].name.startswith("media-")
    assert files[0].name.endswith(".webp")

    second = media_repo.preview_bytes({"path": str(source), "w": 256})
    assert counter["calls"] == 1, "第二次请求不得再解码源文件"
    assert second["bytes"] == first["bytes"]
    assert media_repo.preview_cache_stats()["hit"] == 1


def test_preview_cache_tracks_format_setting_and_prunes_old_format(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = media_runtime["roots"] / "format-switch.png"
    source.write_bytes(_png(400, 200, (90, 100, 110)))
    first = media_repo.preview_bytes({"path": str(source), "w": 256})
    assert first["media_type"] == "image/webp"
    assert [path.suffix for path in _cache_files(media_runtime)] == [".webp"]

    current = media_repo.get_thumbnail_settings()
    with TestClient(create_app()) as client:
        updated = client.patch(
            "/api/asset-thumbnails/settings",
            headers=AUTH,
            json={"format": "jxl", "expected_version": current["revision"]},
        )
    assert updated.status_code == 200, updated.text
    _stub_jxl_encoder(monkeypatch)

    second = media_repo.preview_bytes({"path": str(source), "w": 256})
    files = _cache_files(media_runtime)
    assert second["media_type"] == "image/jxl"
    assert second["bytes"].startswith(b"\xff\x0a")
    assert len(files) == 1 and files[0].suffix == ".jxl"
    assert files[0].read_bytes() == second["bytes"]


def test_source_replacement_invalidates_cached_preview(
    media_runtime: dict[str, Path]
) -> None:
    """AC3：源文件 size/mtime 变化后同一 asset_id + w 返回新图，旧缓存不再命中。"""
    asset_id = _register_asset(media_runtime, "swap.png", _png(240, 120, (10, 10, 10)))
    first = media_repo.preview_bytes({"asset_id": asset_id, "w": 256})
    source = media_runtime["roots"] / "swap.png"

    source.write_bytes(_png(240, 120, (240, 240, 240)))
    stamp = os.stat(source).st_mtime_ns + 2_000_000_000
    os.utime(source, ns=(stamp, stamp))

    second = media_repo.preview_bytes({"asset_id": asset_id, "w": 256})

    assert second["bytes"] != first["bytes"]
    files = [p for p in _cache_files(media_runtime) if p.name.startswith(asset_id)]
    assert len(files) == 1, "同素材同宽度的旧缓存应被清理，只留当前源身份的一份"
    assert files[0].read_bytes() == second["bytes"]


def test_corrupt_cache_falls_back_and_is_repaired(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4：缓存被截断时降级实时生成并返回可用图，同时清掉坏缓存。"""
    source = media_runtime["roots"] / "broken.png"
    source.write_bytes(_png(300, 150, (5, 6, 7)))
    media_repo.preview_bytes({"path": str(source), "w": 256})
    cache_file = _cache_files(media_runtime)[0]
    cache_file.write_bytes(b"trunc")
    stamp = os.stat(source).st_mtime_ns + 5_000_000_000
    os.utime(cache_file, ns=(stamp, stamp))

    counter = _count_decodes(monkeypatch)
    result = media_repo.preview_bytes({"path": str(source), "w": 256})

    assert result["media_type"] == "image/webp"
    assert result["bytes"].startswith(b"RIFF") and result["bytes"][8:12] == b"WEBP"
    assert counter["calls"] == 1, "坏缓存必须触发一次真实重算"
    assert cache_file.stat().st_size > 8, "坏缓存应被重算结果覆盖"
    assert media_repo.preview_cache_stats()["degraded"] == 0


def test_each_width_has_independent_cache_entry(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5：不同宽度各自独立命中，互不覆盖。"""
    source = media_runtime["roots"] / "multi.png"
    source.write_bytes(_png(600, 300, (30, 60, 90)))

    small = media_repo.preview_bytes({"path": str(source), "w": 256})
    large = media_repo.preview_bytes({"path": str(source), "w": 512})

    names = sorted(p.name for p in _cache_files(media_runtime))
    assert len(names) == 2 and any("-w0256-" in n for n in names) and any("-w0512-" in n for n in names)
    assert small["bytes"] != large["bytes"]

    counter = _count_decodes(monkeypatch)
    again_small = media_repo.preview_bytes({"path": str(source), "w": 256})
    again_large = media_repo.preview_bytes({"path": str(source), "w": 512})
    assert counter["calls"] == 0
    assert again_small["bytes"] == small["bytes"]
    assert again_large["bytes"] == large["bytes"]


def test_metadata_descriptor_does_not_enter_preview_cache(
    media_runtime: dict[str, Path]
) -> None:
    """AC6：metadata=1 仍走 descriptor 分支，不因缓存接入改变语义。"""
    source = media_runtime["roots"] / "meta.png"
    source.write_bytes(_png(64, 64, (1, 1, 1)))
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/media-preview",
            headers=AUTH,
            params={"path": str(source), "w": "256", "metadata": "1"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("availability") == "ready" and body.get("artifact_id")
    assert _cache_files(media_runtime) == [], "descriptor 分支不得产生预览缓存文件"


def test_route_preview_reuses_cache_between_requests(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """前端真实调用路径：两次 GET 中第二次命中缓存。"""
    source = media_runtime["roots"] / "route.png"
    source.write_bytes(_png(200, 200, (70, 70, 70)))
    current = media_repo.get_thumbnail_settings()
    _stub_jxl_encoder(monkeypatch)
    with TestClient(create_app()) as client:
        updated = client.patch(
            "/api/asset-thumbnails/settings",
            headers=AUTH,
            json={"format": "jxl", "expected_version": current["revision"]},
        )
        assert updated.status_code == 200, updated.text
        first = client.get("/api/media-preview", headers=AUTH, params={"path": str(source), "w": "256"})
        assert first.status_code == 200, first.text
        assert first.headers["content-type"].startswith("image/jxl")
        assert first.content.startswith(b"\xff\x0a")
        counter = _count_decodes(monkeypatch)
        second = client.get("/api/media-preview", headers=AUTH, params={"path": str(source), "w": "256"})
        assert second.status_code == 200, second.text
    assert second.content == first.content
    assert counter["calls"] == 0, "第二次 GET 必须命中缓存"


def test_preview_cache_hit_ratio_at_scale(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7：500 项规模下第二轮全部命中，命中率可核验。"""
    paths = []
    for index in range(500):
        item = media_runtime["roots"] / ("bulk_%03d.png" % index)
        item.write_bytes(_png(16, 16, (index % 255, 40, 60)))
        paths.append(item)

    media_repo.reset_preview_cache_stats()
    counter = _count_decodes(monkeypatch)
    for item in paths:
        media_repo.preview_bytes({"path": str(item), "w": 256})
    first_pass = counter["calls"]
    assert first_pass == 500

    counter["calls"] = 0
    for item in paths:
        media_repo.preview_bytes({"path": str(item), "w": 256})
    assert counter["calls"] == 0, "第二轮不得再解码源文件"

    stats = media_repo.preview_cache_stats()
    assert stats["degraded"] == 0
    assert stats["hit"] == 500
    assert len(_cache_files(media_runtime)) == 500


def test_video_preview_reuses_extracted_frame(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """视频预览同样走缓存：第二次不再调用 ffmpeg 抽帧。"""
    clip = media_runtime["roots"] / "clip.mp4"
    clip.write_bytes(b"stub-video")
    calls = {"count": 0}

    def fake_frame(path: Path, width: int) -> bytes:
        calls["count"] += 1
        return _jpeg(64, 36, (9, 9, 9))

    monkeypatch.setattr(media_repo, "_video_frame_bytes", fake_frame)

    first = media_repo.preview_bytes({"path": str(clip), "w": 256})
    second = media_repo.preview_bytes({"path": str(clip), "w": 256})

    assert calls["count"] == 1, "第二次预览不得重复抽帧"
    assert first["bytes"] == second["bytes"]
    assert first["media_type"] == "image/jpeg"
    assert [p.name for p in _cache_files(media_runtime) if p.name.endswith(".frame.jpg")]


def test_cache_directory_not_admitted_degrades_to_live_preview(
    media_runtime: dict[str, Path], tmp_path: Path
) -> None:
    """缓存目录越界（未登记允许根）时必须降级实时生成，而不是 403 打挂预览。"""
    source = media_runtime["roots"] / "degrade.png"
    source.write_bytes(_png(120, 80, (44, 55, 66)))
    outside = tmp_path / "outside"
    outside.mkdir()
    media_repo.patch_thumbnail_settings({"mode": "custom", "directory": str(outside)})

    result = media_repo.preview_bytes({"path": str(source), "w": 256})

    assert result["media_type"] == "image/webp"
    assert result["bytes"].startswith(b"RIFF") and result["bytes"][8:12] == b"WEBP"
    assert media_repo.preview_cache_stats()["degraded"] == 1
    assert _cache_files(media_runtime) == []


def test_delete_outputs_clears_preview_cache_too(
    media_runtime: dict[str, Path]
) -> None:
    """删除缩略图缓存时必须一并清掉预览派生缓存，避免磁盘残留。"""
    asset_id = _register_asset(media_runtime, "clean.png", _png(180, 90, (20, 30, 40)))
    media_repo.generate_thumbnail({"asset_id": asset_id, "width": 256})
    media_repo.preview_bytes({"asset_id": asset_id, "w": 256})
    media_repo.preview_bytes({"asset_id": asset_id, "w": 512})
    # 256 命中预生成产物（不再另存），512 另存一份预览缓存。
    assert len(_cache_files(media_runtime)) == 2

    removed = media_repo.delete_outputs("thumbnails", {"asset_ids": [asset_id]})

    assert removed["removed"] == 2, removed
    assert _cache_files(media_runtime) == []


def test_delete_outputs_clears_preview_cache_too(
    media_runtime: dict[str, Path]
) -> None:
    """删除缩略图缓存时必须一并清掉预览派生缓存，避免磁盘残留。"""
    asset_id = _register_asset(media_runtime, "clean.png", _png(180, 90, (20, 30, 40)))
    media_repo.generate_thumbnail({"asset_id": asset_id, "width": 256})
    media_repo.preview_bytes({"asset_id": asset_id, "w": 256})
    media_repo.preview_bytes({"asset_id": asset_id, "w": 512})
    # 256 命中预生成产物（不再另存），512 另存一份预览缓存。
    assert len(_cache_files(media_runtime)) == 2

    removed = media_repo.delete_outputs("thumbnails", {"asset_ids": [asset_id]})

    assert removed["removed"] == 2, removed
    assert _cache_files(media_runtime) == []
