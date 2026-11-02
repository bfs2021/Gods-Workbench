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

"""QIN-519 全库预览缓存预热契约测试。

覆盖 AC1 / AC4：
- ``scope="library"`` 复用 ``generate-background``，返回 202 + 稳定 job_id + poll_hint；
- 任务真异步（提交即返回，进度经 ``jobs/{job_id}`` 回读 processed/total）；
- 已有新鲜缓存一律跳过（第二次运行 generated=0、skipped=total），真实落盘不重复；
- 预热范围只含可见、未归档的图片/视频素材。
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.asset_registry import repository as asset_repo
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
CACHE_SUFFIXES = (".webp", ".jpg.png", ".jxl")


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
    # 显式钉死 webp：默认 jxl 依赖 ffmpeg 的 libjxl，CI 镜像缺编码器会造成
    # 「生成 0 张」的假失败；断言本身不锁扩展名。
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


def _png(width: int = 320, height: int = 180, color: tuple[int, int, int] = (30, 40, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _register_asset(
    runtime: dict[str, Path], name: str, kind: str = "image"
) -> tuple[str, int]:
    path = runtime["roots"] / name
    path.write_bytes(_png())
    created = asset_repo.import_assets([{"name": name, "kind": kind}], kind, None)
    asset_id = created["asset_ids"][0]
    updated = asset_repo.update_asset(
        asset_id, {"display_path": str(path), "expected_version": created["revision"]}
    )
    return asset_id, int(updated.get("revision") or created["revision"])


def _wait_job(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = media_repo.get_job(job_id)["task"]
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("预热任务超时未结束：%s" % job_id)


def _cache_files(runtime: dict[str, Path]) -> list[Path]:
    root = runtime["data"] / "media_output" / "god-cache"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith(CACHE_SUFFIXES)
    )


def test_preview_all_endpoint_returns_async_job(media_runtime: dict[str, Path]) -> None:
    asset_ids = [_register_asset(media_runtime, "preview-%d.png" % index)[0] for index in range(3)]

    with TestClient(create_app()) as client:
        started = time.monotonic()
        response = client.post(
            "/api/asset-thumbnails/generate-background",
            headers=AUTH,
            json={"scope": "library", "throttle_ms": 300},
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 202, response.text
        data = response.json()
        assert data["scope"] == "library"
        assert data["total"] == len(asset_ids)
        job_id = data["job_id"]
        assert data["poll_hint"] == "/api/asset-thumbnails/jobs/%s" % job_id
        # 真异步：3 项 × 300ms 限速远未跑完，POST 已返回。
        assert elapsed < 0.7, elapsed

        first = client.get(data["poll_hint"], headers=AUTH)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "running"

        job = _wait_job(job_id)
        assert job["status"] == "succeeded", job
        progress = job["progress"]
        assert progress["total"] == len(asset_ids)
        assert progress["processed"] == len(asset_ids)
        assert job["result"]["generated"] == len(asset_ids)
        assert job["result"]["skipped"] == 0
        assert job["result"]["width"] == 256
        assert job["result"]["elapsed_seconds"] >= 0
        assert len(_cache_files(media_runtime)) == len(asset_ids)

        replay = client.get(data["poll_hint"], headers=AUTH)
        assert replay.status_code == 200
        assert replay.json()["status"] == "succeeded"


def test_preview_all_is_idempotent_across_runs(media_runtime: dict[str, Path]) -> None:
    asset_ids = [_register_asset(media_runtime, "repeat-%d.png" % index)[0] for index in range(2)]

    job_id, poll_hint, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert poll_hint == "/api/asset-thumbnails/jobs/%s" % job_id
    assert total == len(asset_ids)
    first = _wait_job(job_id)
    assert first["status"] == "succeeded", first
    assert first["result"]["generated"] == len(asset_ids)
    assert first["result"]["skipped"] == 0
    first_files = _cache_files(media_runtime)
    assert len(first_files) == len(asset_ids)

    second_id, _, second_total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert second_total == len(asset_ids)
    second = _wait_job(second_id)
    assert second["status"] == "succeeded", second
    # 幂等：已新鲜的同宽缓存直接跳过，不重复解码。
    assert second["result"]["generated"] == 0
    assert second["result"]["skipped"] == len(asset_ids)
    assert _cache_files(media_runtime) == first_files


def test_preview_all_scope_ignores_text_and_archived_assets(
    media_runtime: dict[str, Path],
) -> None:
    _register_asset(media_runtime, "keep.png")
    _register_asset(media_runtime, "notes.txt", kind="text")
    archived_id, revision = _register_asset(media_runtime, "archived.png")
    asset_repo.update_asset(archived_id, {"archived": True, "expected_version": revision})

    job_id, _, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert total == 1
    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 1
    assert len(_cache_files(media_runtime)) == 1


def test_preview_all_reuses_running_job_instead_of_duplicating(
    media_runtime: dict[str, Path],
) -> None:
    for index in range(2):
        _register_asset(media_runtime, "dedupe-%d.png" % index)

    first_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 400})
    second_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 400})
    assert second_id == first_id
    job = _wait_job(first_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 2
