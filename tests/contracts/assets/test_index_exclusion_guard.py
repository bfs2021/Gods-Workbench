"""素材索引扫描应跳过缓存、回收站与隐藏目录。"""

import time

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import index_jobs, repository
from gw.media import repository as media_repository
from gw.settings.models import StorageSettingsPatchRequest
from gw.settings.service import default_storage_settings_service

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer index-owner", "X-User-Role": "editor"}


@pytest.fixture
def asset_library(tmp_path, monkeypatch):
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "gw-data"))
    root = tmp_path / "asset-library"
    root.mkdir()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None
    yield root
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None


def _write_media(root, relative_path):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic image")


def _wait_for_job(client, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(
            f"{BASE}/workspace-jobs/{job_id}", headers=AUTH
        )
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        if job["status"] in index_jobs.TERMINAL:
            return job
        time.sleep(0.02)
    pytest.fail("后台索引未在期限内终结")


def test_reindex_and_incremental_sync_prune_live_cache_recycler_and_hidden_paths(
    asset_library, tmp_path
):
    root = asset_library
    trash_root = root / "trash"
    trash_root.mkdir()
    _write_media(root, "normal/keep.png")
    excluded_files = [
        "god-cache/cache.png",
        ".cache/cache.png",
        ".trash/cache.png",
        ".git/cache.png",
        ".gods-workbench/cache.png",
        ".hidden/cache.png",
        "god-recycler/recycled.png",
        "trash/god-recycler/recycled.png",
        "custom_cache/cache.png",
        "custom_cache/god-cache/cache.png",
    ]
    for relative_path in excluded_files:
        _write_media(root, relative_path)
    (root / "normal/.DS_Store").write_bytes(b"metadata")

    with TestClient(create_app()) as client:
        # 保持同一个索引服务实例，在其创建后修改缓存配置，验证扫描时热读取。
        index_jobs.service()
        storage_snapshot = default_storage_settings_service.get_snapshot()
        default_storage_settings_service.patch(
            StorageSettingsPatchRequest(
                dirs={"local": str(root), "trash": str(trash_root)},
                expected_version=storage_snapshot.revision,
            )
        )
        media_repository.patch_thumbnail_settings(
            {"mode": "custom", "directory": str(root / "custom_cache")}
        )

        reindex = client.post(
            f"{BASE}/reindex",
            headers={**AUTH, "Idempotency-Key": "qin-523-reindex"},
            json={"background": True},
        )
        assert reindex.status_code == 202, reindex.text
        reindex_job = _wait_for_job(client, reindex.json()["job_id"])
        assert reindex_job["status"] == "succeeded", reindex_job
        assert reindex_job["result"]["indexed"] == 1
        assert reindex_job["result"]["scanned"] == 1
        assert {asset["name"] for asset in repository.state().read()["assets"].values()} == {
            "keep.png"
        }

        sync = client.post(
            f"{BASE}/index/sync",
            headers={**AUTH, "Idempotency-Key": "qin-523-incremental-sync"},
            json={"background": True},
        )
        assert sync.status_code == 202, sync.text
        sync_job = _wait_for_job(client, sync.json()["job_id"])
        assert sync_job["status"] == "succeeded", sync_job
        assert sync_job["result"]["on_disk"] == 1
        assert sync_job["result"]["missing_from_registry"] == 0
        assert sync_job["result"]["missing_from_disk"] == 0

        direct_sync = client.post(f"{BASE}/index/sync", headers=AUTH, json={})
        assert direct_sync.status_code == 200, direct_sync.text
        assert direct_sync.json()["on_disk"] == 1
        assert direct_sync.json()["missing_from_registry"] == []
        assert direct_sync.json()["missing_from_disk"] == []
