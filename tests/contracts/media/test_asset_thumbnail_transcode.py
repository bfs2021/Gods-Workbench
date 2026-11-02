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

"""QIN-516：缩略图批次视频转码接线契约。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gw.asset_registry import repository as asset_repo
from gw.core import storage
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
        {
            "mode": "source",
            "directory": "",
            "cache_folder": "god-cache",
            "width": 256,
            "format": "webp",
        }
    )
    return {"data": data_root, "roots": allowed_root}


def _webp_bytes(width: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, max(1, width // 2)), (12, 34, 56)).save(
        buffer, format="WEBP"
    )
    return buffer.getvalue()


def _register_asset(runtime: dict[str, Path], name: str) -> tuple[str, Path]:
    path = runtime["roots"] / name
    is_video = path.suffix.lower() in media_repo._VIDEO_SUFFIXES
    if not path.exists():
        path.write_bytes(b"video source" if is_video else _webp_bytes())
    created = asset_repo.import_assets(
        [{"name": name, "kind": "video" if is_video else "image"}],
        "video" if is_video else "image",
        None,
    )
    asset_id = created["asset_ids"][0]
    asset_repo.update_asset(
        asset_id,
        {"display_path": str(path), "expected_version": created["revision"]},
    )
    return asset_id, path


def _fake_thumbnails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media_repo,
        "_thumbnail_bytes",
        lambda path, width, image_format="webp": _webp_bytes(width),
    )


def _fake_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], timeout: int = media_repo.FFMPEG_TIMEOUT):
        calls.append(args)
        if fail:
            return subprocess.CompletedProcess(args, 1)
        Path(args[-1]).write_bytes(b"fake mp4 output")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(media_repo, "_tool", lambda name: "ffmpeg-test-double")
    monkeypatch.setattr(media_repo, "_run", run)
    return calls


@pytest.mark.parametrize("mode", ["source", "custom"])
def test_batch_transcode_uses_thumbnail_cache_root_and_existing_filename(
    mode: str,
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_id, video = _register_asset(media_runtime, "clip.mp4")
    image_id, _ = _register_asset(media_runtime, "still.png")
    custom = media_runtime["roots"] / "cache-home"
    if mode == "custom":
        custom.mkdir()
    media_repo.patch_thumbnail_settings(
        {"mode": mode, "directory": str(custom) if mode == "custom" else ""}
    )
    _fake_thumbnails(monkeypatch)
    calls = _fake_ffmpeg(monkeypatch)

    result = media_repo.generate_thumbnail(
        {
            "asset_ids": [video_id, image_id],
            "transcode_videos": True,
            "replace_existing": True,
            "width": 256,
        }
    )

    assert result["generated"] == 2 and result["failed"] == 0
    assert result["transcoded"] == 1
    assert result["transcode_cached"] == 0
    assert result["transcode_failed"] == 0
    video_item, image_item = result["items"]
    assert video_item["transcode_status"] == "generated"
    assert image_item.get("transcode_status") is None
    assert len(calls) == 1

    cache_dir = media_repo.resolve_cache_directory(
        media_repo.get_thumbnail_settings(), video
    )[1]
    assert (cache_dir / f"{video_id}.webp").is_file()
    stat = video.stat()
    identity = "|".join(
        (storage.relative_display(video), str(stat.st_size), str(stat.st_mtime_ns))
    )
    expected_name = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32] + ".mp4"
    transcode_path = cache_dir / expected_name
    assert Path(video_item["transcode_output"]).name == expected_name
    assert transcode_path.is_file()
    assert transcode_path.parent == cache_dir
    assert result["transcode_outputs"] == [video_item["transcode_output"]]


@pytest.mark.parametrize("field", [False, None])
def test_transcode_is_opt_in_and_has_no_hidden_ffmpeg_work(
    field: bool | None,
    media_runtime: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id, video = _register_asset(media_runtime, "clip.mp4")
    _fake_thumbnails(monkeypatch)

    def unexpected(*args: Any, **kwargs: Any):
        raise AssertionError("未勾选时不应调用 ffmpeg")

    monkeypatch.setattr(media_repo, "_tool", unexpected)
    monkeypatch.setattr(media_repo, "_run", unexpected)
    payload: dict[str, Any] = {"asset_ids": [asset_id], "replace_existing": True}
    if field is not None:
        payload["transcode_videos"] = field

    result = media_repo.generate_thumbnail(payload)

    assert result["generated"] == 1
    assert result["transcoded"] == result["transcode_cached"] == 0
    assert result["transcode_failed"] == 0
    assert result["transcode_outputs"] == []
    cache_dir = media_repo.resolve_cache_directory(
        media_repo.get_thumbnail_settings(), video
    )[1]
    assert not list(cache_dir.glob("*.mp4"))


def test_transcode_cache_and_replace_existing_are_independent(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id, _ = _register_asset(media_runtime, "clip.mp4")
    _fake_thumbnails(monkeypatch)
    calls = _fake_ffmpeg(monkeypatch)
    payload = {"asset_ids": [asset_id], "transcode_videos": True}

    first = media_repo.generate_thumbnail({**payload, "replace_existing": True})
    cached = media_repo.generate_thumbnail({**payload, "replace_existing": False})
    replaced = media_repo.generate_thumbnail({**payload, "replace_existing": True})

    assert first["transcoded"] == 1 and first["transcode_cached"] == 0
    assert cached["transcoded"] == 0 and cached["transcode_cached"] == 1
    assert replaced["transcoded"] == 1 and replaced["transcode_cached"] == 0
    assert len(calls) == 2


def test_transcode_failure_does_not_block_thumbnail_or_other_items(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    video_id, _ = _register_asset(media_runtime, "broken.mp4")
    image_id, _ = _register_asset(media_runtime, "still.png")
    _fake_thumbnails(monkeypatch)
    _fake_ffmpeg(monkeypatch, fail=True)

    result = media_repo.generate_thumbnail(
        {
            "asset_ids": [video_id, image_id],
            "transcode_videos": True,
            "replace_existing": True,
        }
    )

    assert result["generated"] == 2 and result["failed"] == 0
    assert result["transcoded"] == 0 and result["transcode_failed"] == 1
    assert result["data_status"] == "partial"
    assert "video_transcode_failures_present" in result["data_gaps"]
    video_item = result["items"][0]
    assert video_item["status"] == "generated"
    assert video_item["transcode_status"] == "failed"
    assert video_item["transcode_error"]["code"] == "MEDIA_PROCESS_FAILED"


def test_thumbnail_failure_does_not_block_transcode(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    video_id, _ = _register_asset(media_runtime, "thumbnail-broken.mp4")

    def fail_thumbnail(path: Path, width: int, image_format: str = "webp") -> bytes:
        raise CleanroomException(503, "THUMBNAIL_FAILED", "视频缩略图失败")

    monkeypatch.setattr(media_repo, "_thumbnail_bytes", fail_thumbnail)
    _fake_ffmpeg(monkeypatch)

    result = media_repo.generate_thumbnail(
        {"asset_ids": [video_id], "transcode_videos": True, "replace_existing": True}
    )

    item = result["items"][0]
    assert result["failed"] == 1 and result["transcoded"] == 1
    assert result["transcode_failed"] == 0
    assert result["data_status"] == "partial"
    assert "thumbnail_failures_present" in result["data_gaps"]
    assert "video_transcode_failures_present" not in result["data_gaps"]
    assert item["status"] == "failed"
    assert item["error"]["code"] == "THUMBNAIL_FAILED"
    assert item["transcode_status"] == "generated"


def test_unknown_media_type_is_not_a_transcode_failure(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id, _ = _register_asset(media_runtime, "unknown.bin")
    _fake_thumbnails(monkeypatch)

    def unexpected(*args: Any, **kwargs: Any):
        raise AssertionError("无法判定为视频的素材不应转码")

    monkeypatch.setattr(media_repo, "_tool", unexpected)
    monkeypatch.setattr(media_repo, "_run", unexpected)
    result = media_repo.generate_thumbnail(
        {"asset_ids": [asset_id], "transcode_videos": True}
    )

    assert result["generated"] == 1
    assert result["transcoded"] == 0
    assert result["transcode_cached"] == 0
    assert result["transcode_failed"] == 0


def test_background_job_reports_two_stage_result_and_asset_progress(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [
        _register_asset(media_runtime, "one.mp4")[0],
        _register_asset(media_runtime, "two.mp4")[0],
    ]
    _fake_thumbnails(monkeypatch)
    _fake_ffmpeg(monkeypatch)
    payload = {"asset_ids": ids, "transcode_videos": True, "replace_existing": True}
    sync_result = media_repo.generate_thumbnail(payload)

    job_id, poll_hint = media_repo.run_background_thumbnail(
        payload
    )

    assert poll_hint == f"/api/asset-thumbnails/jobs/{job_id}"
    job = media_repo.get_job(job_id)
    assert job["progress"] == {"processed": 2, "total": 2}
    assert job["result"]["generated"] == 2
    assert job["result"]["transcoded"] == 2
    assert job["result"]["transcode_failed"] == 0
    assert job["result"] == sync_result


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="未安装 ffmpeg/ffprobe；此环境不能声称真实媒体验收通过",
)
def test_real_transcode_is_playable_h264_mp4(
    media_runtime: dict[str, Path],
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg and ffprobe
    source = media_runtime["roots"] / "source.mov"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=10",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    asset_id, _ = _register_asset(media_runtime, source.name)

    result = media_repo.generate_thumbnail(
        {"asset_ids": [asset_id], "transcode_videos": True, "replace_existing": True}
    )

    assert result["transcoded"] == 1, result
    cache_dir = media_repo.resolve_cache_directory(
        media_repo.get_thumbnail_settings(), source
    )[1]
    output = cache_dir / Path(result["items"][0]["transcode_output"]).name
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_name",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    metadata = json.loads(probe.stdout)
    assert "mp4" in metadata["format"]["format_name"]
    assert any(stream["codec_name"] == "h264" for stream in metadata["streams"])
