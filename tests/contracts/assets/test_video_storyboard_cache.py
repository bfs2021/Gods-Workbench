"""QIN-510 视频分镜缩略图：预生成、缓存复用、元数据回写与删除联动。

真实验收口径：合成视频 + ffmpeg 真抽帧；未安装 ffmpeg 时跳过，不声称通过。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.core import storage
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


def _require_ffmpeg() -> str:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("未安装 ffmpeg/ffprobe；此环境不能声称真实分镜验收通过")
    return ffmpeg


def _make_video(root: Path, name: str, *, seconds: int = 4, color: str = "red") -> Path:
    target = root / name
    subprocess.run(
        [
            _require_ffmpeg(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=%s:s=96x72:r=10" % color,
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return target


def _make_image(root: Path, name: str) -> Path:
    from PIL import Image

    target = root / name
    Image.new("RGB", (64, 48), (30, 60, 90)).save(target)
    return target


def _segments() -> list[dict]:
    return [
        {"start_sec": 0, "end_sec": 1.5, "summary": "开场"},
        {"start_sec": 1.5, "end_sec": 3.0, "summary": "主体"},
    ]


def _register(
    client: TestClient,
    root: Path,
    source: Path,
    *,
    name: str = "视频素材",
    kind: str = "video",
    segments: bool = True,
) -> str:
    item: dict = {"name": name, "kind": kind}
    if segments:
        item["video_segments"] = _segments()
    created = client.post(
        "/api/asset-registry/assets/import", headers=AUTH, json={"items": [item]}
    )
    assert created.status_code == 200, created.text
    asset_id = created.json()["asset_ids"][0]
    patched = client.patch(
        "/api/asset-registry/assets/" + asset_id,
        headers=AUTH,
        json={
            "display_path": source.name,
            "root_id": storage.root_identity_id(root.resolve()),
        },
    )
    assert patched.status_code == 200, patched.text
    return asset_id


def _data_root() -> Path:
    return Path(os.environ["GW_DATA_DIR"])


def _asset(client: TestClient, asset_id: str) -> dict:
    response = client.get("/api/asset-registry/assets/" + asset_id, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["asset"]


def _features(client: TestClient) -> dict:
    response = client.get("/api/asset-registry", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["features"]


def _generate(client: TestClient, asset_ids: list[str]) -> dict:
    submitted = client.post(
        "/api/asset-thumbnails/generate-storyboards",
        headers=AUTH,
        json={"asset_ids": asset_ids},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["job_id"].startswith("sjob_")
    assert body["poll_hint"].endswith(body["job_id"])
    job = client.get(body["poll_hint"], headers=AUTH)
    assert job.status_code == 200, job.text
    return job.json()


def test_generation_writes_back_real_segment_thumbnails(tmp_path, monkeypatch):
    """AC1/AC5：分镜生成后每段缩略图都是真实存在文件，任务回报真实计数。"""
    root = tmp_path / "media"
    root.mkdir()
    source = _make_video(root, "clip.mp4", seconds=4)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    with TestClient(create_app()) as client:
        asset_id = _register(client, root, source)
        job = _generate(client, [asset_id])

        assert job["status"] == "succeeded"
        assert job["progress"] == {"processed": 1, "total": 1}
        assert job["result"]["generated"] == 1
        assert job["result"]["cached"] == 0
        assert job["result"]["failed"] == 0

        segments = _asset(client, asset_id)["metadata"]["video_segments"]
        assert len(segments) == 2
        # 缓存位置沿用 cache_folder 口径（与图片缩略图同一套位置语义）。
        prefix = media_repo.cache_root_relative() + "/storyboards/" + asset_id + "/"
        for segment in segments:
            thumb = segment.get("thumbnail")
            assert thumb, segment
            assert thumb.startswith(prefix), thumb
            assert (_data_root() / thumb).is_file(), thumb
        assert segments[0]["thumbnail"] != segments[1]["thumbnail"]
        assert _features(client)["video_storyboard"] is True


def test_storyboard_endpoint_reuses_generated_frames(tmp_path, monkeypatch):
    """AC2：九宫格命中已生成缓存，第二次请求不重抽帧且产物 mtime 不变。"""
    root = tmp_path / "media"
    root.mkdir()
    source = _make_video(root, "clip.mp4", seconds=4)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    with TestClient(create_app()) as client:
        asset_id = _register(client, root, source)
        _generate(client, [asset_id])
        path = "/api/asset-registry/assets/%s/video/storyboard" % asset_id

        first = client.get(path, headers=AUTH).json()
        assert first["count"] >= 1
        targets = [_data_root() / frame["output_name"] for frame in first["frames"]]
        assert all(target.is_file() for target in targets)
        stamps = [target.stat().st_mtime_ns for target in targets]

        second = client.get(path, headers=AUTH).json()
        assert [target.stat().st_mtime_ns for target in targets] == stamps
        assert second["generated"] == 0
        assert second["cached"] == second["count"]
        assert all(frame["status"] == "cached" for frame in second["frames"])


def test_non_video_fails_closed_without_fake_thumbnail(tmp_path, monkeypatch):
    """AC3：非视频素材失败关闭，返回明确错误码且元数据保持原值。"""
    root = tmp_path / "media"
    root.mkdir()
    image = _make_image(root, "poster.png")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    with TestClient(create_app()) as client:
        asset_id = _register(client, root, image, name="海报", kind="image")
        response = client.post(
            "/api/asset-thumbnails/generate-storyboards",
            headers=AUTH,
            json={"asset_ids": [asset_id]},
        )
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "MEDIA_NOT_AVAILABLE"

        segments = _asset(client, asset_id)["metadata"]["video_segments"]
        assert all(segment.get("thumbnail") is None for segment in segments)
        assert _features(client)["video_storyboard"] is False


def test_batch_reports_real_counts_and_keeps_failures_isolated(tmp_path, monkeypatch):
    """AC5：批量逐项计数，单项失败不中断整批也不写假路径。"""
    root = tmp_path / "media"
    root.mkdir()
    first = _make_video(root, "a.mp4", seconds=3, color="green")
    second = _make_video(root, "b.mp4", seconds=3, color="blue")
    poster = _make_image(root, "poster.png")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    with TestClient(create_app()) as client:
        ids = [
            _register(client, root, first, name="A"),
            _register(client, root, second, name="B"),
            _register(client, root, poster, name="P", kind="image"),
        ]
        job = _generate(client, ids)
        result = job["result"]
        assert job["progress"] == {"processed": 3, "total": 3}
        assert (result["generated"], result["cached"], result["failed"]) == (2, 0, 1)
        assert result["data_status"] == "partial"
        assert result["data_gaps"] == ["storyboard_failures_present"]
        failed = [item for item in result["items"] if item["status"] == "failed"]
        assert [item["error"]["code"] for item in failed] == ["MEDIA_NOT_AVAILABLE"]

        again = _generate(client, ids)["result"]
        assert (again["generated"], again["cached"], again["failed"]) == (0, 2, 1)


def test_delete_clears_metadata_files_and_feature_then_regenerates(tmp_path, monkeypatch):
    """AC4/AC6：删除后字段清空、文件消失、开关回假；再次生成可恢复。"""
    root = tmp_path / "media"
    root.mkdir()
    source = _make_video(root, "clip.mp4", seconds=4)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    with TestClient(create_app()) as client:
        asset_id = _register(client, root, source)
        _generate(client, [asset_id])
        thumbnails = [
            segment["thumbnail"]
            for segment in _asset(client, asset_id)["metadata"]["video_segments"]
        ]
        assert all((_data_root() / thumb).is_file() for thumb in thumbnails)

        removed = client.post(
            "/api/asset-thumbnails/delete-storyboards",
            headers=AUTH,
            json={"asset_ids": [asset_id]},
        )
        assert removed.status_code == 200, removed.text
        body = removed.json()
        assert body["removed"] >= len(thumbnails)
        assert body["metadata_cleared"] == 1
        assert all(not (_data_root() / thumb).is_file() for thumb in thumbnails)
        segments = _asset(client, asset_id)["metadata"]["video_segments"]
        assert all(segment.get("thumbnail") is None for segment in segments)
        assert _features(client)["video_storyboard"] is False

        restored = _generate(client, [asset_id])
        assert restored["status"] == "succeeded"
        assert restored["result"]["generated"] == 1
        segments = _asset(client, asset_id)["metadata"]["video_segments"]
        assert all((_data_root() / segment["thumbnail"]).is_file() for segment in segments)
        assert _features(client)["video_storyboard"] is True
