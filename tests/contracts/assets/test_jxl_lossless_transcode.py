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

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.asset_registry import repository as asset_repo
from gw.core.asset_visibility import asset_owner_key
from gw.core.auth import require_authenticated
from gw.core import jxl_tool

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


@pytest.fixture
def jxl_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_root = tmp_path / "gw-data"
    allowed_root = tmp_path / "roots"
    data_root.mkdir()
    allowed_root.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed_root))
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(tmp_path / "gw-auth" / "auth.sqlite3"))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(tmp_path / "gw-video"))
    return {"data": data_root, "roots": allowed_root}


def _register_jpeg(runtime: dict[str, Path], name: str = "source.jpg") -> tuple[str, Path]:
    source = runtime["roots"] / name
    Image.new("RGB", (48, 32), (40, 80, 120)).save(source, format="JPEG", quality=91)
    created = asset_repo.import_assets(
        [{"name": source.name, "kind": "image"}], "image", None
    )
    asset_id = created["asset_ids"][0]
    asset_repo.update_asset(
        asset_id,
        {"display_path": str(source), "expected_version": created["revision"]},
    )
    return asset_id, source


def _install_fake_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transcode_started: threading.Event | None = None,
    release_transcode: threading.Event | None = None,
) -> None:
    monkeypatch.setattr(jxl_tool.shutil, "which", lambda name: f"fake-{name}")

    def run(command, **_kwargs):
        args = [str(value) for value in command]
        if args[-1] == "--version":
            label = "encoder" if "cjxl" in args[0] else "decoder"
            return subprocess.CompletedProcess(args, 0, stdout=f"JPEG XL {label} test")
        if "cjxl" in args[0]:
            if transcode_started is not None:
                transcode_started.set()
            if release_transcode is not None and not release_transcode.wait(5):
                return subprocess.CompletedProcess(args, 1, stderr="test timeout")
            source = Path(args[-2]).read_bytes()
            Path(args[-1]).write_bytes(b"\xff\x0a" + source)
            return subprocess.CompletedProcess(args, 0, stdout="")
        encoded = Path(args[-2]).read_bytes()
        Path(args[-1]).write_bytes(encoded[2:])
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(jxl_tool.subprocess, "run", run)


def _wait_for_status(client: TestClient, job_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 5
    latest = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/assets/transcode-jpeg-lossless/jobs/{job_id}", headers=AUTH
        )
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] == expected:
            return latest
        time.sleep(0.02)
    pytest.fail(f"转码任务未在时限内进入 {expected}：{latest}")


def test_scan_reports_missing_tools_and_registered_jpeg_counts(
    jxl_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_jpeg(jxl_runtime)
    monkeypatch.setattr(jxl_tool.shutil, "which", lambda _name: None)
    monkeypatch.setattr(jxl_tool, "_find_tool", lambda _name: (None, None))

    with TestClient(create_app()) as client:
        response = client.get("/api/assets/transcode-jpeg-lossless/scan", headers=AUTH)
        unavailable = client.post(
            "/api/assets/transcode-jpeg-lossless", headers=AUTH, json={}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool_status"]["status"] == "missing"
    assert body["tool_status"]["cjxl"]["code"] == "CJXL_NOT_FOUND"
    assert body["jpeg_assets"] == 1
    assert body["pending_count"] == 1
    assert body["pending_bytes"] > 0
    assert body["estimated_savings_min_bytes"] == body["pending_bytes"] // 5
    assert body["originals_retained"] is True
    assert body["active_job"] is None

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "CJXL_NOT_FOUND"


def test_transcode_job_is_pollable_and_records_verified_output(
    jxl_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id, source = _register_jpeg(jxl_runtime)
    original = source.read_bytes()
    _install_fake_tools(monkeypatch)
    owner = asset_owner_key(require_authenticated(AUTH["Authorization"], "editor"))

    def make_private(raw):
        raw["assets"][asset_id]["workflow_owner"] = owner

    asset_repo.state().mutate(make_private)

    with TestClient(create_app()) as client:
        accepted = client.post(
            "/api/assets/transcode-jpeg-lossless", headers=AUTH, json={}
        )
        assert accepted.status_code == 202, accepted.text
        payload = accepted.json()
        job_id = payload["job_id"]
        assert payload["poll_hint"]["url"].endswith(job_id)

        job = _wait_for_status(client, job_id, "succeeded")
        scan = client.get("/api/assets/transcode-jpeg-lossless/scan", headers=AUTH)

    assert job["progress"]["total"] == 1
    assert job["progress"]["completed"] == 1
    assert job["result"]["verified"] == 1
    output_relative = job["result"]["items"][0]["output"]
    output = jxl_runtime["data"] / output_relative
    assert output.suffix == ".jxl"
    assert output.read_bytes().startswith(b"\xff\x0a")
    assert source.read_bytes() == original
    metadata = asset_repo.state().read()["assets"][asset_id]["metadata"]["jxl_lossless_transcode"]
    assert metadata["verified_sha256"] == hashlib.sha256(original).hexdigest()
    assert metadata["output"] == output_relative
    assert scan.json()["pending_count"] == 0
    assert scan.json()["active_job"] is None


def test_cancel_finishes_current_file_and_preserves_original(
    jxl_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id, source = _register_jpeg(jxl_runtime)
    original = source.read_bytes()
    started = threading.Event()
    release = threading.Event()
    _install_fake_tools(
        monkeypatch, transcode_started=started, release_transcode=release
    )

    with TestClient(create_app()) as client:
        accepted = client.post(
            "/api/assets/transcode-jpeg-lossless", headers=AUTH, json={}
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        assert started.wait(3), "cjxl 应开始处理当前文件"
        active_scan = client.get("/api/assets/transcode-jpeg-lossless/scan", headers=AUTH)
        assert active_scan.json()["active_job"]["job_id"] == job_id
        cancel = client.post(
            f"/api/assets/transcode-jpeg-lossless/jobs/{job_id}/cancel",
            headers=AUTH,
            json={},
        )
        assert cancel.status_code == 200, cancel.text
        release.set()
        job = _wait_for_status(client, job_id, "cancelled")

    assert job["progress"]["completed"] == 1
    assert source.read_bytes() == original
    assert asset_repo.get_asset(asset_id)["metadata"]["jxl_lossless_transcode"]["verified"] is True
