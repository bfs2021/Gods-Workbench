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

"""QIN-511 素材缓存链路端到端契约：生成 → 命中 → 删除 → 再生成。

守卫目标：接口层「能跑通」不等于用户可见功能「能生效」。本文件把三条链路
（QIN-508 批量生成、QIN-509 预览命中缓存、QIN-510 九宫格回写）串成一条可重放
的闭环，并且按 AC4 做负向验证：把后端改成「只认单个 asset_id」或「不复用缓存」
时，本文件的用例必须变红，而不是又一批恒绿断言。

口径：全部走 HTTP 契约（真实路由 + 真实落盘），不 mock 业务逻辑。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.core import storage
from gw.core.errors import CleanroomException
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
#: 缓存宽度；与前端卡片请求的宽度保持一致，保证预览能命中预生成产物。
WIDTH = 256


@pytest.fixture
def cache_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """隔离数据目录，并把素材源根登记为唯一允许根。"""
    data_root = tmp_path / "gw-data"
    media_root = tmp_path / "media"
    data_root.mkdir(parents=True)
    media_root.mkdir()
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(tmp_path / "gw-auth" / "auth.sqlite3"))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(tmp_path / "gw-video"))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(media_root))
    return {"data": data_root, "media": media_root}


def _require_ffmpeg() -> str:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("未安装 ffmpeg/ffprobe；此环境不能声称九宫格验收通过")
    return ffmpeg


def _make_image(root: Path, name: str, color: tuple[int, int, int]) -> Path:
    target = root / name
    Image.new("RGB", (512, 384), color).save(target)
    return target


def _make_video(root: Path, name: str, *, seconds: int = 3, color: str = "green") -> Path:
    target = root / name
    subprocess.run(
        [
            _require_ffmpeg(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=96x72:r=10",
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


def _register(
    client: TestClient,
    root: Path,
    source: Path,
    *,
    name: str = "素材",
    kind: str = "image",
    segments: bool = False,
) -> str:
    item: dict = {"name": name, "kind": kind}
    if segments:
        item["video_segments"] = [
            {"start_sec": 0, "end_sec": 1.0, "summary": "开场"},
            {"start_sec": 1.0, "end_sec": 2.0, "summary": "主体"},
        ]
    created = client.post(
        "/api/asset-registry/assets/import", headers=AUTH, json={"items": [item]}
    )
    assert created.status_code == 200, created.text
    asset_id = created.json()["asset_ids"][0]
    patched = client.patch(
        f"/api/asset-registry/assets/{asset_id}",
        headers=AUTH,
        json={
            "display_path": source.name,
            "root_id": storage.root_identity_id(root.resolve()),
        },
    )
    assert patched.status_code == 200, patched.text
    return asset_id


def _use_webp(client: TestClient) -> None:
    """钉死 webp：默认 jxl 经 ffmpeg -c:v libjxl，CI 镜像无此编码器会生成 0 张。"""
    settings = client.get("/api/asset-thumbnails/settings", headers=AUTH)
    assert settings.status_code == 200, settings.text
    updated = client.patch(
        "/api/asset-thumbnails/settings",
        headers=AUTH,
        json={"format": "webp", "expected_version": settings.json()["revision"]},
    )
    assert updated.status_code == 200, updated.text


def _three_images(client: TestClient, root: Path) -> list[str]:
    return [
        _register(client, root, _make_image(root, "a.png", (200, 40, 40)), name="A"),
        _register(client, root, _make_image(root, "b.png", (40, 160, 60)), name="B"),
        _register(client, root, _make_image(root, "c.png", (40, 60, 200)), name="C"),
    ]


def _mtimes(data_root: Path, outputs: list[str]) -> dict[str, int]:
    return {name: (data_root / name).stat().st_mtime_ns for name in outputs}


def _generate(client: TestClient, asset_ids: list[str], **extra) -> dict:
    response = client.post(
        "/api/asset-thumbnails/generate",
        headers=AUTH,
        json={"asset_ids": asset_ids, "width": WIDTH, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_batch_generate_and_single_generate_share_one_artifact_path(
    tmp_path: Path, cache_runtime: dict[str, Path]
) -> None:
    """AC1：批量入参与单条入参指向同一产物，批量不得因形状差异被拒。"""
    data_root = cache_runtime["data"]
    with TestClient(create_app()) as client:
        _use_webp(client)
        ids = _three_images(client, cache_runtime["media"])

        batch = _generate(client, ids)
        assert (batch["generated"], batch["cached"], batch["failed"]) == (3, 0, 0), batch
        batch_outputs = sorted(batch["outputs"])
        assert len(batch_outputs) == 3, batch_outputs
        for relative in batch_outputs:
            assert relative.startswith("media_output/god-cache/"), relative
            assert (data_root / relative).is_file(), relative

        removed = client.post(
            "/api/asset-thumbnails/delete", headers=AUTH, json={"asset_ids": ids}
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["removed"] == 3, removed.text

        single_outputs: list[str] = []
        for asset_id in ids:
            one = client.post(
                "/api/asset-thumbnails/generate",
                headers=AUTH,
                json={"asset_id": asset_id, "width": WIDTH},
            )
            assert one.status_code == 200, one.text
            assert one.json()["generated"] == 1, one.text
            single_outputs.extend(one.json()["outputs"])

        # 形状不同，产物路径必须完全一致（否则前端批量永远拿不到卡片缩略图）。
        assert sorted(single_outputs) == batch_outputs


def test_regenerate_reports_cached_and_keeps_mtime_until_replace(
    tmp_path: Path, cache_runtime: dict[str, Path]
) -> None:
    """AC2：再生成计入 cached 且 mtime 不变；replace_existing 才重算并更新 mtime。"""
    data_root = cache_runtime["data"]
    with TestClient(create_app()) as client:
        _use_webp(client)
        ids = _three_images(client, cache_runtime["media"])

        first = _generate(client, ids)
        assert first["generated"] == 3, first
        before = _mtimes(data_root, first["outputs"])

        second = _generate(client, ids)
        assert (second["cached"], second["generated"]) == (3, 0), second
        assert _mtimes(data_root, first["outputs"]) == before

        time.sleep(0.01)
        replaced = _generate(client, ids, replace_existing=True)
        assert (replaced["generated"], replaced["cached"]) == (3, 0), replaced
        after = _mtimes(data_root, first["outputs"])
        assert all(after[name] > before[name] for name in after)


def test_delete_then_regenerate_forms_a_repeatable_loop(
    tmp_path: Path, cache_runtime: dict[str, Path]
) -> None:
    """AC3：删除后 removed == N 且文件消失；再生成重新计入 generated。"""
    data_root = cache_runtime["data"]
    with TestClient(create_app()) as client:
        _use_webp(client)
        ids = _three_images(client, cache_runtime["media"])

        first = _generate(client, ids)
        outputs = first["outputs"]
        assert all((data_root / name).is_file() for name in outputs)

        removed = client.post(
            "/api/asset-thumbnails/delete", headers=AUTH, json={"asset_ids": ids}
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["removed"] == 3, removed.text
        assert not any((data_root / name).is_file() for name in outputs)

        again = _generate(client, ids)
        assert (again["generated"], again["cached"]) == (3, 0), again
        assert sorted(again["outputs"]) == sorted(outputs)
        assert all((data_root / name).is_file() for name in outputs)


def test_media_preview_reuses_generated_cache_without_redecoding(
    tmp_path: Path, cache_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 消费侧（QIN-509）：预览命中预生成产物，命中时不重复解码源文件。"""
    media_repo.reset_preview_cache_stats()
    decodes = {"count": 0}
    live = media_repo._live_preview_bytes

    def counting(*args, **kwargs):
        decodes["count"] += 1
        return live(*args, **kwargs)

    monkeypatch.setattr(media_repo, "_live_preview_bytes", counting)
    with TestClient(create_app()) as client:
        _use_webp(client)
        ids = _three_images(client, cache_runtime["media"])
        _generate(client, ids)

        for asset_id in ids:
            preview = client.get(
                "/api/media-preview",
                headers=AUTH,
                params={"asset_id": asset_id, "w": WIDTH},
            )
            assert preview.status_code == 200, preview.text
            assert preview.content, preview.text

        stats = media_repo.preview_cache_stats()
        assert stats["hit"] == 3, stats
        assert stats["miss"] == 0, stats
        # 命中路径不解码源文件：_live_preview_bytes 是唯一的实时解码出口。
        assert decodes["count"] == 0, decodes

        # 换个宽度就没有对应产物：此时才允许解码并落预览缓存。
        other = client.get(
            "/api/media-preview",
            headers=AUTH,
            params={"asset_id": ids[0], "w": 384},
        )
        assert other.status_code == 200, other.text
        assert media_repo.preview_cache_stats()["miss"] == 1
        assert decodes["count"] == 1, decodes


def test_storyboard_cache_hit_skips_frame_extraction(
    tmp_path: Path, cache_runtime: dict[str, Path]
) -> None:
    """AC3 消费侧（QIN-510）：九宫格命中缓存，不重复抽帧也不重写元数据。"""
    data_root = cache_runtime["data"]
    media_root = cache_runtime["media"]
    source = _make_video(media_root, "clip.mp4", seconds=3)
    with TestClient(create_app()) as client:
        _use_webp(client)
        asset_id = _register(client, media_root, source, name="视频", kind="video", segments=True)

        first = client.post(
            "/api/asset-thumbnails/generate-storyboards",
            headers=AUTH,
            json={"asset_ids": [asset_id]},
        )
        assert first.status_code == 202, first.text
        job_id, poll_hint = first.json()["job_id"], first.json()["poll_hint"]
        done = client.get(poll_hint, headers=AUTH)
        assert done.status_code == 200, done.text
        result = done.json()["result"]
        assert (result["generated"], result["cached"]) == (1, 0), result
        assert result["segments_updated"] == 2, result

        segments = client.get(
            f"/api/asset-registry/assets/{asset_id}", headers=AUTH
        ).json()["asset"]["metadata"]["video_segments"]
        thumbnails = sorted(segment["thumbnail"] for segment in segments)
        assert all(thumb for thumb in thumbnails)
        stamps = _mtimes(data_root, thumbnails)
        grid = data_root / result["outputs"][0]
        assert grid.is_file(), result["outputs"]
        grid_stamp = grid.stat().st_mtime_ns

        # 第二次：命中缓存，计数转为 cached，抽帧产物与合成图一律不动。
        again = client.post(
            "/api/asset-thumbnails/generate-storyboards",
            headers=AUTH,
            json={"asset_ids": [asset_id]},
        )
        assert again.status_code == 202, again.text
        second = client.get(again.json()["poll_hint"], headers=AUTH).json()["result"]
        assert (second["generated"], second["cached"]) == (0, 1), second
        # 元数据回写是幂等的：路径不变，不新增也不清空。
        rewritten = client.get(
            f"/api/asset-registry/assets/{asset_id}", headers=AUTH
        ).json()["asset"]["metadata"]["video_segments"]
        assert sorted(s["thumbnail"] for s in rewritten) == thumbnails
        assert _mtimes(data_root, thumbnails) == stamps
        assert grid.stat().st_mtime_ns == grid_stamp
        assert job_id != again.json()["job_id"]

        # 消费侧：读取九宫格同样命中合成图，不重抽帧也不再删中间帧。
        read = client.get(
            f"/api/asset-registry/assets/{asset_id}/video/storyboard", headers=AUTH
        )
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["image_status"] == "cached", body
        assert body["generated"] == 0, body
        assert body["frames_removed"] == 0, body
        assert (data_root / body["image"]).stat().st_mtime_ns == grid_stamp


def test_payload_shape_guard_covers_single_and_batch(
    tmp_path: Path, cache_runtime: dict[str, Path]
) -> None:
    """契约错位守卫：后端必须同时接受单条 asset_id 与批量 asset_ids。

    任一侧只保留一种形状，本用例都必须变红（AC4 负向验证的靶子）。
    """
    assert media_repo._asset_ids({"asset_id": "ast_0001"}) == ["ast_0001"]
    assert media_repo._asset_ids({"asset_ids": ["ast_0001", "ast_0002"]}) == [
        "ast_0001",
        "ast_0002",
    ]
    # 形状不是数组时失败关闭，不静默按字符串拆字符。
    with pytest.raises(CleanroomException) as error:
        media_repo._asset_ids({"asset_ids": 3})
    assert error.value.status_code == 400

    with TestClient(create_app()) as client:
        _use_webp(client)
        ids = _three_images(client, cache_runtime["media"])
        batch_one = _generate(client, ids[:1])
        single = client.post(
            "/api/asset-thumbnails/generate",
            headers=AUTH,
            json={"asset_id": ids[0], "width": WIDTH},
        )
        assert single.status_code == 200, single.text
        assert sorted(batch_one["outputs"]) == sorted(single.json()["outputs"])
        assert os.environ["GW_ALLOWED_ROOTS"] == str(cache_runtime["media"])
