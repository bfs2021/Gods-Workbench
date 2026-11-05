"""QIN-552：Cube LUT 头信息、索引类型与失败关闭契约。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import index_jobs
from gw.asset_registry import repository as repo
from gw.asset_registry.lut_cube import (
    LutCubeError,
    parse_lut_cube_bytes,
)

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


def _cube_3d(title: str = "Warm") -> bytes:
    samples = "\n".join(f"{i / 7:g} {i / 7:g} {i / 7:g}" for i in range(8))
    return (
        f'TITLE "{title}"\n'
        "LUT_3D_SIZE 2\n"
        "DOMAIN_MIN 0 0 0\n"
        "DOMAIN_MAX 1 1 1\n"
        f"{samples}\n"
    ).encode()


@pytest.fixture
def lut_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "素材来源"
    root.mkdir()
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "gw-data"))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None
    yield root
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None


def _wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(f"{BASE}/workspace-jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        if job["status"] in index_jobs.TERMINAL:
            return job
        time.sleep(0.02)
    pytest.fail("LUT 索引任务未在期限内终结")


def _submit_reindex(client: TestClient, key: str) -> str:
    response = client.post(
        f"{BASE}/reindex",
        headers={**AUTH, "Idempotency-Key": key},
        json={"background": True, "hash_files": False, "max_files": 100},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def test_parser_extracts_cube_headers_and_accepts_1d_and_3d_samples():
    metadata = parse_lut_cube_bytes(_cube_3d())
    assert metadata == {
        "title": "Warm",
        "lut_3d_size": 2,
        "domain_min": [0.0, 0.0, 0.0],
        "domain_max": [1.0, 1.0, 1.0],
        "sample_count": 8,
    }

    one_d = b"LUT_1D_SIZE 2\n0 0 0\n1 1 1\n"
    assert parse_lut_cube_bytes(one_d) == {
        "lut_1d_size": 2,
        "sample_count": 2,
    }
    mixed = b"LUT_1D_SIZE 2\nLUT_3D_SIZE 2\n" + b"0 0 0\n" * 10
    assert parse_lut_cube_bytes(mixed)["sample_count"] == 10


def test_parser_rejects_bad_rows_and_enforces_size_limit(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(LutCubeError, match="采样行数"):
        parse_lut_cube_bytes(b"LUT_3D_SIZE 2\n0 0 0\n")
    with pytest.raises(LutCubeError, match="三个通道"):
        parse_lut_cube_bytes(b"LUT_1D_SIZE 2\n0 0\n1 1 1\n")

    monkeypatch.setattr("gw.asset_registry.lut_cube.MAX_LUT_CUBE_BYTES", 4)
    with pytest.raises(LutCubeError) as caught:
        parse_lut_cube_bytes(b"12345")
    assert caught.value.code == "LUT_CUBE_TOO_LARGE"


def test_reindex_registers_cube_as_lut_and_refreshes_metadata(lut_runtime: Path):
    cube_path = lut_runtime / "warm.cube"
    cube_path.write_bytes(_cube_3d())
    non_cube_path = lut_runtime / "warm.txt"
    non_cube_path.write_bytes(_cube_3d())

    with TestClient(create_app()) as client:
        job = _wait_job(client, _submit_reindex(client, "qin-552-valid"))
        assert job["status"] == "succeeded", job
        listing = client.get(f"{BASE}/assets", headers=AUTH, params={"kind": "lut"})
        assert listing.status_code == 200, listing.text
        assert [item["name"] for item in listing.json()["assets"]] == ["warm.cube"]
        lut_asset = listing.json()["assets"][0]
        stored = repo.state().read()["assets"][lut_asset["id"]]
        assert stored["kind"] == "lut"
        assert stored["metadata"]["lut"]["title"] == "Warm"

        categories = client.get(f"{BASE}/facets", headers=AUTH).json()["categories"]
        assert {item["name"]: item["count"] for item in categories}["lut"] == 1
        other = next(
            item
            for item in client.get(f"{BASE}/assets", headers=AUTH).json()["assets"]
            if item["name"] == "warm.txt"
        )
        assert other["kind"] != "lut"

        cube_path.write_bytes(_cube_3d("Cool"))
        updated = _wait_job(client, _submit_reindex(client, "qin-552-refresh"))
        assert updated["status"] == "succeeded", updated
        assert updated["result"]["updated"] == 1
        refreshed = repo.state().read()["assets"][lut_asset["id"]]
        assert refreshed["metadata"]["lut"]["title"] == "Cool"


def test_reindex_rejects_malformed_cube_without_registering_it(lut_runtime: Path):
    (lut_runtime / "broken.cube").write_text(
        "TITLE \"Broken\"\nLUT_3D_SIZE 2\n0 0 0\n", encoding="utf-8"
    )
    with TestClient(create_app()) as client:
        job = _wait_job(client, _submit_reindex(client, "qin-552-invalid"))
        assert job["status"] == "failed"
        assert job["error_code"] == "LUT_CUBE_INVALID"
        assert not repo.state().read()["assets"]
