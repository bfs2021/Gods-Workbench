"""QIN-539（GitHub #592）素材来源库名与文件修改时间下发契约。

票面 AC1 要求列表与单条接口为每条资产返回「来源库显示名（与左栏资产库名一致）」
与「文件修改时间（ISO 8601）」。本用例覆盖三条读取路径（分页列表 / 单条详情 /
总览）以及两条不伪造边界：索引期落盘与历史条目兜底 stat、无文件登记时留空。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import index_jobs
from gw.asset_registry import repository as repo

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
# 固定 2023-11-14T22:13:20Z，跨平台按秒取整，避免时区与精度差异造成抖动。
MODIFIED_EPOCH = 1_700_000_000
EXPECTED_MODIFIED = "2023-11-14T22:13:20Z"


@pytest.fixture()
def registry_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    allowed = tmp_path / "允许根"
    library = allowed / "测试素材库"
    library.mkdir(parents=True)
    still = library / "静帧.txt"
    still.write_text("静帧", encoding="utf-8")
    os.utime(still, (MODIFIED_EPOCH, MODIFIED_EPOCH))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed))
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None
    yield allowed, library, still
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None


def _register(client: TestClient, path: Path, root_id: str, *, name: str, key: str):
    return client.post(
        BASE + "/reindex",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "background": True,
            "hash_files": False,
            "max_files": 100,
            "roots": [root_id],
            "register_root": {
                "id": root_id,
                "name": name,
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


def _index_library(client: TestClient, library: Path, root_id: str, *, name: str, key: str) -> str:
    created = _register(client, library, root_id, name=name, key=key)
    assert created.status_code == 202, created.text
    job = _wait(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["indexed"] == 1
    listing = client.get(BASE + "/assets", headers=AUTH).json()
    asset = next(item for item in listing["assets"] if item["name"] == "静帧.txt")
    return asset["id"]


def test_list_detail_and_overview_return_source_name_and_file_modified_time(registry_runtime):
    _, library, _ = registry_runtime
    with TestClient(create_app()) as client:
        asset_id = _index_library(client, library, "local_hosted", name="托管资产", key="qin-539-single")

        listed = next(
            item
            for item in client.get(BASE + "/assets", headers=AUTH).json()["assets"]
            if item["id"] == asset_id
        )
        assert listed["storage_root_name"] == "托管资产"
        assert listed["source_names"] == ["托管资产"]
        assert listed["modified_at"] == EXPECTED_MODIFIED

        detail = client.get(f"{BASE}/assets/{asset_id}", headers=AUTH).json()["asset"]
        assert detail["storage_root_name"] == "托管资产"
        assert detail["source_names"] == ["托管资产"]
        assert detail["modified_at"] == EXPECTED_MODIFIED

        overview = next(
            item
            for item in client.get(BASE, headers=AUTH).json()["assets"]
            if item["id"] == asset_id
        )
        assert overview["storage_root_name"] == "托管资产"
        assert overview["modified_at"] == EXPECTED_MODIFIED


def test_index_persists_modified_at_and_legacy_records_fall_back_to_real_file_time(registry_runtime):
    _, library, _ = registry_runtime
    with TestClient(create_app()) as client:
        asset_id = _index_library(client, library, "local_project", name="项目资产", key="qin-539-legacy")

        # 索引期就把文件修改时间落盘，读取时不必逐条 stat。
        stored = repo.state().read()["assets"][asset_id]
        assert stored["modified_at"] == EXPECTED_MODIFIED

        # 历史条目（本票之前登记的记录）没有该字段：读取时兜底 stat，仍给出真实时间。
        def drop_modified(raw):
            for asset in raw["assets"].values():
                asset.pop("modified_at", None)

        repo.state().mutate(drop_modified)
        assert "modified_at" not in repo.state().read()["assets"][asset_id]

        detail = client.get(f"{BASE}/assets/{asset_id}", headers=AUTH).json()["asset"]
        assert detail["modified_at"] == EXPECTED_MODIFIED
        listed = next(
            item
            for item in client.get(BASE + "/assets", headers=AUTH).json()["assets"]
            if item["id"] == asset_id
        )
        assert listed["modified_at"] == EXPECTED_MODIFIED


def test_registration_without_file_does_not_fabricate_source_or_modified_time(registry_runtime):
    with TestClient(create_app()) as client:
        created = client.post(
            BASE + "/assets/import",
            headers=AUTH,
            json={"name": "仅登记素材.txt", "kind": "document"},
        )
        assert created.status_code == 200, created.text
        asset_id = created.json()["asset_ids"][0]

        detail = client.get(f"{BASE}/assets/{asset_id}", headers=AUTH).json()["asset"]
        # 没有来源登记、也没有可读文件时不编造：来源为空列表，时间缺失（前端回落「—」）。
        assert detail.get("source_names", []) == []
        assert not detail.get("storage_root_name")
        assert not detail.get("modified_at")

        # 登记方明确带上文件修改时间时原样保留，不被读取期兜底覆盖。
        carried = client.post(
            BASE + "/assets/import",
            headers=AUTH,
            json={"name": "带时间素材.txt", "kind": "document", "modified_at": EXPECTED_MODIFIED},
        )
        assert carried.status_code == 200, carried.text
        carried_id = carried.json()["asset_ids"][0]
        stored = client.get(f"{BASE}/assets/{carried_id}", headers=AUTH).json()["asset"]
        assert stored["modified_at"] == EXPECTED_MODIFIED
