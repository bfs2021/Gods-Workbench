"""DIR-T1 来源根筛选、资产库登记与真实索引契约。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import index_jobs


BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


@pytest.fixture()
def index_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    allowed = tmp_path / "允许根"
    library = allowed / "测试素材库"
    library.mkdir(parents=True)
    (library / "海报-甲.txt").write_text("海报甲", encoding="utf-8")
    (library / "海报-乙.txt").write_text("海报乙", encoding="utf-8")
    (allowed / "根目录外层素材.txt").write_text("不可索引", encoding="utf-8")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed))
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None
    yield allowed, library
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None


def _register(client: TestClient, path: Path, root_id: str, *, key: str = "dir-t1-register"):
    return client.post(
        BASE + "/reindex",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "background": True,
            "hash_files": True,
            "max_files": 100,
            "roots": [root_id],
            "register_root": {
                "id": root_id,
                "name": "中文测试库",
                "path": str(path),
                "expected_version": 1,
            },
        },
    )


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(BASE + f"/workspace-jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        if job["status"] in index_jobs.TERMINAL:
            return job
        time.sleep(0.02)
    pytest.fail("后台索引未在期限内终结")


def test_dir_t1_registers_chinese_subdirectory_indexes_and_filters_with_cursor(index_runtime):
    allowed, library = index_runtime
    with TestClient(create_app()) as client:
        created = _register(client, library, "local_extra_chinese")
        assert created.status_code == 202, created.text
        body = created.json()
        assert body["job_id"]
        assert body["poll_hint"]["url"].endswith(body["job_id"])

        job = _wait(client, body["job_id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["scanned"] == 2
        assert job["result"]["indexed"] == 2

        assets = client.get(BASE + "/assets", headers=AUTH, params={"root_id": "local_extra_chinese"})
        assert assets.status_code == 200, assets.text
        assert {item["name"] for item in assets.json()["assets"]} == {"海报-甲.txt", "海报-乙.txt"}
        assert all(item["storage_root_id"] == "local_extra_chinese" for item in assets.json()["assets"])
        assert all(item["root_id"] != "local_extra_chinese" for item in assets.json()["assets"])
        assert all(item["display_path"].startswith("测试素材库/") for item in assets.json()["assets"])
        assert all(str(allowed) not in item["display_path"] for item in assets.json()["assets"])
        settings = client.get("/api/storage-settings", headers=AUTH)
        source = next(item for item in settings.json()["local_libraries"]
                      if item["id"] == "local_extra_chinese")
        assert Path(source["path"]) == library.resolve()
        assert {item["id"] for item in client.get(BASE + "/facets", headers=AUTH).json()["roots"]} >= {
            "local_extra_chinese",
        }

        project = client.post(BASE + "/projects", headers=AUTH,
                              json={"name": "DIR-T1 游标", "project_type": "film"})
        assert project.status_code == 201, project.text
        project_id = project.json()["project"]["project_id"]
        asset_ids = [item["asset_id"] for item in assets.json()["assets"]]
        bound = client.post(BASE + f"/projects/{project_id}/assets", headers=AUTH,
                            json={"asset_ids": asset_ids})
        assert bound.status_code == 200, bound.text

        from gw.asset_registry import repository as repo
        repo.state().mutate(lambda raw: raw["assets"][asset_ids[0]].update({"collection_id": "collection-a"}))

        first = client.get(BASE + "/assets", headers=AUTH, params={
            "root_id": "local_extra_chinese", "project_id": project_id,
            "category": "document", "sort": "name", "limit": 1,
        })
        assert first.status_code == 200, first.text
        assert first.json()["has_more"] is True
        cursor = first.json()["next_cursor"]
        second = client.get(BASE + "/assets", headers=AUTH, params={
            "root_id": "local_extra_chinese", "project_id": project_id,
            "category": "document", "sort": "name", "limit": 1, "cursor": cursor,
        })
        assert second.status_code == 200, second.text
        assert {item["name"] for item in first.json()["assets"] + second.json()["assets"]} == {
            "海报-甲.txt", "海报-乙.txt",
        }

        collection = client.get(BASE + "/assets", headers=AUTH,
                                params={"root_id": "local_extra_chinese", "collection_id": "collection-a"})
        assert collection.status_code == 200, collection.text
        assert [item["asset_id"] for item in collection.json()["assets"]] == [asset_ids[0]]
        unknown = client.get(BASE + "/assets", headers=AUTH, params={"root_id": "root_unknown"})
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "ROOT_NOT_FOUND"


def test_dir_t1_rejects_registration_outside_allowed_roots_before_persisting(index_runtime, tmp_path):
    outside = tmp_path / "未准入素材库"
    outside.mkdir()
    (outside / "secret.txt").write_text("不应索引", encoding="utf-8")
    with TestClient(create_app()) as client:
        rejected = _register(client, outside, "local_extra_outside", key="dir-t1-outside")
        assert rejected.status_code == 403, rejected.text
        assert "GW_ALLOWED_ROOTS" in rejected.json()["detail"]["message"]

        settings = client.get("/api/storage-settings", headers=AUTH)
        assert settings.status_code == 200, settings.text
        assert settings.json()["extra_local_entries"] == []
        assert client.get(BASE + "/status", headers=AUTH).json()["jobs"] == []
