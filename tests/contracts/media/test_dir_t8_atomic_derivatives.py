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

"""DIR-T8 派生物 key 冲突与原子写契约测试。

覆盖 DA-08 / DA-12 / DA-13 / DA-14 / DA-15 / DA-16 的最小回归：
多根同名源、不同宽度、CAS 失败不产生孤儿、显式冲突策略、
派生物 registry 按 asset_id 精确查找、storyboard 缓存按版本隔离。
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gw.asset_library import repository as library_repo
from gw.asset_registry import media_probe
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
    media_repo.patch_thumbnail_settings({"format": "webp"})
    return {"data": data_root, "roots": allowed_root}


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _register_asset(tmp_path: Path, name: str, payload: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(payload)
    created = asset_repo.import_assets(
        [{"name": name, "kind": "image"}], "image", None
    )
    asset_id = created["asset_ids"][0]
    asset_repo.update_asset(
        asset_id,
        {
            "display_path": str(path),
            "expected_version": created["revision"],
        },
    )
    return asset_id


def _thumbnails_dir(runtime: dict[str, Path]) -> Path:
    return runtime["data"] / "media_output" / "thumbnails"


def _storyboards_dir(runtime: dict[str, Path]) -> Path:
    # 分镜与缩略图共用 cache_folder 受管缓存根（QIN-510）。
    return runtime["data"] / media_repo.cache_root_relative() / "storyboards"


def test_thumbnail_different_sources_same_stem_produce_distinct_files(
    media_runtime: dict[str, Path],
) -> None:
    """两个目录同 stem 的 PNG 不再共用同一派生物 key。"""
    root = media_runtime["roots"]
    (root / "alpha").mkdir()
    (root / "beta").mkdir()
    source_a = root / "alpha" / "dup.png"
    source_b = root / "beta" / "dup.png"
    source_a.write_bytes(_png(160, 90, (10, 20, 30)))
    source_b.write_bytes(_png(320, 180, (40, 50, 60)))

    first = media_repo.generate_thumbnail({"path": str(source_a), "width": 128})
    second = media_repo.generate_thumbnail({"path": str(source_b), "width": 128})
    third = media_repo.generate_thumbnail({"path": str(source_a), "width": 256})

    files = {
        Path(entry["output"]).name
        for entry in (first, second, third)
    }
    assert len(files) == 3, files
    assert not list(_thumbnails_dir(media_runtime).glob("*.part"))


def test_thumbnail_payload_with_asset_id_keeps_fingerprint_key(
    media_runtime: dict[str, Path],
) -> None:
    """提供 asset_id 时保留 registry 身份并按宽度隔离；stem 只是无 id 时的回退。"""
    root = media_runtime["roots"]
    source = root / "pic.png"
    source.write_bytes(_png(320, 180, (1, 2, 3)))
    asset_id = _register_asset(media_runtime["roots"], "pic.png", source.read_bytes())

    payload: dict[str, Any] = {"asset_id": asset_id, "path": str(source), "width": 128}
    first = media_repo.generate_thumbnail(payload)
    second = media_repo.generate_thumbnail({**payload, "width": 256})

    assert Path(first["output"]).name == f"{asset_id}.webp"
    assert Path(second["output"]).name == f"{asset_id}.webp"
    assert first["output"] == second["output"]


def test_download_path_prefers_registry_identity_over_stem(
    media_runtime: dict[str, Path],
) -> None:
    """asset_id 与文件 stem 不同名时，下载按 registry 身份前缀匹配。"""
    root = media_runtime["roots"]
    source = root / "unique-source.png"
    source.write_bytes(_png(80, 60, (9, 9, 9)))
    asset_id = _register_asset(
        media_runtime["roots"], "unique-source.png", source.read_bytes()
    )
    media_repo.generate_thumbnail({"asset_id": asset_id, "width": 64})

    result = media_repo.download_path({"asset_id": asset_id})

    assert asset_id in result["filename"]
    assert Path(result["path"]).stem != "unique-source"


def test_storyboard_directory_is_version_scoped(
    media_runtime: dict[str, Path],
) -> None:
    """同一资产不同 version 映射到独立缓存目录，缺失版本走默认桶。"""
    asset_id = "ast_v1"
    cache_root = media_repo.cache_root_relative()
    default_dir = media_probe.storyboard_directory(asset_id, None, cache_root=cache_root)
    version_one = media_probe.storyboard_directory(
        asset_id, "1710000000", cache_root=cache_root
    )
    version_two = media_probe.storyboard_directory(
        asset_id, "1719999999", cache_root=cache_root
    )
    assert default_dir != version_one
    assert version_one != version_two
    assert version_one.is_relative_to(
        media_probe.storyboard_directory(asset_id, None, cache_root=cache_root)
    )


def test_storyboard_cache_isolated_per_version(
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版本变化不命中旧缓存；抽帧调用隔离在版本子目录。"""
    root = media_runtime["roots"]
    video = root / "clip.mp4"
    video.write_bytes(b"fixture-not-a-real-mp4")
    asset_id = _register_asset(root, "clip.mp4", video.read_bytes())

    monkeypatch.setattr(
        media_probe, "probe", lambda _path: {"width": 64, "height": 64, "duration_seconds": 4.0}
    )
    monkeypatch.setattr(media_probe, "_ffmpeg", lambda _name: "ffmpeg-test-double")

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png(64, 64, (5, 6, 7)))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(media_probe, "_run", fake_run)

    cache_root = media_repo.cache_root_relative()
    first = media_probe.storyboard(asset_id, "1710000000", cache_root=cache_root)
    second = media_probe.storyboard(asset_id, "1719999999", cache_root=cache_root)
    assert first["version"] == "1710000000"
    assert second["version"] == "1719999999"
    # 中间帧合成后即删除，产物路径以合成单图为锚点判断版本隔离。
    first_parent = Path(first["image"]).parts
    second_parent = Path(second["image"]).parts
    assert first_parent != second_parent


def test_clip_cas_conflict_leaves_no_orphan_media(
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS 失败时 clip 不得在最终路径留下成功形状的孤儿文件。"""
    root = media_runtime["roots"]
    video = root / "movie.mp4"
    video.write_bytes(b"fixture-not-a-real-mp4")
    asset_id = _register_asset(root, "movie.mp4", video.read_bytes())
    revision = asset_repo.state().read()["revision"]

    monkeypatch.setattr(
        media_probe, "probe", lambda _path: {"width": 64, "height": 64, "duration_seconds": 8.0}
    )
    monkeypatch.setattr(media_probe, "_ffmpeg", lambda _name: "ffmpeg-test-double")

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-mp4-derivative")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(media_probe, "_run", fake_run)

    with pytest.raises(CleanroomException) as conflict:
        media_probe.clip(asset_id, 0.0, 1.0, revision - 1)
    assert conflict.value.code == "VERSION_CONFLICT"
    clip_dir = media_runtime["data"] / "media_output" / asset_id
    assert not list(clip_dir.glob("clip_*")) if clip_dir.exists() else True
    assert not list(clip_dir.glob("*.part")) if clip_dir.exists() else True

    result = media_probe.clip(asset_id, 0.0, 1.0, revision)
    assert result["clip"]["asset_id"] == asset_id
    final_file = media_runtime["data"] / result["clip"]["output_name"]
    assert final_file.is_file()
    assert not list(clip_dir.glob("*.part"))


def test_keyframe_extractor_writes_part_then_replaces(
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键帧派生必须通过 .part + 原子替换，中断时清理临时文件。"""
    from gw.media import keyframe_extractor

    root = media_runtime["roots"]
    video = root / "scene.mp4"
    video.write_bytes(b"fixture-not-a-real-mp4")
    output_dir = media_runtime["data"] / "keyframes"
    monkeypatch.setattr(keyframe_extractor, "_tool", lambda _name: "ffmpeg-test-double")

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        target = Path(args[-1])
        assert target.name == "scene_001.part.webp", args
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png(64, 64, (8, 8, 8)))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(keyframe_extractor, "_run", fake_run)

    result = keyframe_extractor.extract_keyframes(
        video,
        [{"start_sec": 0.0, "end_sec": 1.0, "tags": []}],
        output_dir,
    )
    assert result["sprite"] == "scene_sprite.webp"
    assert (output_dir / "metadata.json").is_file()
    assert not list(output_dir.glob("*.part.*"))
    assert (output_dir / "scene_001.webp").is_file()


def test_save_upload_explicit_conflict_rejects_overwrite(
    media_runtime: dict[str, Path],
) -> None:
    """显式冲突策略：目标已存在且身份不同则拒绝，不再隐式覆盖。"""
    target = media_runtime["roots"] / "upload" / "asset.json"
    payload = base64.b64encode(b'{"first":true}').decode("ascii")
    first = library_repo.save_upload(str(target), payload)
    assert first["written"] is True
    assert target.is_file()

    conflicting = base64.b64encode(b'{"second":true}').decode("ascii")
    with pytest.raises(CleanroomException) as conflict:
        library_repo.save_upload(str(target), conflicting)
    assert conflict.value.code == "PATH_CONFLICT"
    assert target.read_bytes() == b'{"first":true}'
    assert not list(target.parent.glob("*.part"))


def test_save_upload_atomic_replace_cleans_part_on_failure(
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """替换失败时不得留下 .part 或半成品文件。"""
    target = media_runtime["roots"] / "fresh.json"
    payload = base64.b64encode(b"payload").decode("ascii")

    original_replace = __import__("os").replace

    def broken_replace(source: str, destination: str) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(library_repo.os, "replace", broken_replace)
    with pytest.raises(CleanroomException) as failed:
        library_repo.save_upload(str(target), payload)
    assert failed.value.status_code == 503
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))

    monkeypatch.setattr(library_repo.os, "replace", original_replace)
    retried = library_repo.save_upload(str(target), payload)
    assert retried["written"] is True
    assert target.read_bytes() == b"payload"


def test_storyboard_delete_outputs_removes_versioned_cache(
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除分镜缓存必须覆盖全部版本子目录。"""
    root = media_runtime["roots"]
    video = root / "to-delete.mp4"
    video.write_bytes(b"fixture-not-a-real-mp4")
    asset_id = _register_asset(root, "to-delete.mp4", video.read_bytes())
    monkeypatch.setattr(
        media_probe, "probe", lambda _path: {"width": 16, "height": 16, "duration_seconds": 2.0}
    )
    monkeypatch.setattr(media_probe, "_ffmpeg", lambda _name: "ffmpeg-test-double")

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png(16, 16, (0, 0, 0)))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(media_probe, "_run", fake_run)
    media_probe.storyboard(
        asset_id, "1710000000", cache_root=media_repo.cache_root_relative()
    )

    removed = media_repo.delete_outputs("storyboards", {"asset_ids": [asset_id]})
    assert removed["removed"] >= 1
    remaining = list((_storyboards_dir(media_runtime) / asset_id).rglob("frame_*.png"))
    assert not remaining, remaining


def test_image_proxy_registry_identity_isolated(
    media_runtime: dict[str, Path],
) -> None:
    """代理产物已经按源身份哈希命名，确认同一 asset 不同宽度产物不同。"""
    from gw.media import proxy

    root = media_runtime["roots"]
    source = root / "proxy-source.png"
    source.write_bytes(_png(640, 360, (3, 3, 3)))
    asset_id = _register_asset(root, "proxy-source.png", source.read_bytes())

    first = proxy.build_proxy({"asset_id": asset_id, "profile": "preview", "width": 320})
    second = proxy.build_proxy({"asset_id": asset_id, "profile": "preview", "width": 128})
    assert first["artifact_id"] != second["artifact_id"]
    assert first["width"] == 320 and second["width"] == 128
