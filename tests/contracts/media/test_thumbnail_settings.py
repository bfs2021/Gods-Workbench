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

"""QIN-513 缩略图格式设置、CAS 与实际派生产物契约。"""

from __future__ import annotations

import io
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.asset_registry import repository as asset_repo
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


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
            "format": "jxl",
        }
    )
    return {"data": data_root, "roots": allowed_root}


def _register_png(runtime: dict[str, Path]) -> str:
    source = runtime["roots"] / "thumbnail-source.png"
    image = Image.new("RGB", (320, 180), (34, 88, 144))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    source.write_bytes(buffer.getvalue())
    created = asset_repo.import_assets(
        [{"name": source.name, "kind": "image"}], "image", None
    )
    asset_id = created["asset_ids"][0]
    asset_repo.update_asset(
        asset_id,
        {"display_path": str(source), "expected_version": created["revision"]},
    )
    return asset_id


def test_thumbnail_settings_default_and_patch_use_cas() -> None:
    client = TestClient(create_app())
    current = client.get("/api/asset-thumbnails/settings", headers=AUTH)

    assert current.status_code == 200, current.text
    snapshot = current.json()
    assert snapshot["format"] == "jxl"

    updated = client.patch(
        "/api/asset-thumbnails/settings",
        headers=AUTH,
        json={"format": "webp", "expected_version": snapshot["revision"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["format"] == "webp"
    assert updated.json()["revision"] == snapshot["revision"] + 1

    conflict = client.patch(
        "/api/asset-thumbnails/settings",
        headers=AUTH,
        json={"format": "jxl", "expected_version": snapshot["revision"]},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_thumbnail_format_generates_jxl_and_webp_with_matching_extensions(
    media_runtime: dict[str, Path],
) -> None:
    client = TestClient(create_app())
    asset_id = _register_png(media_runtime)

    jxl = media_repo.generate_thumbnail({"asset_id": asset_id, "width": 256})
    jxl_path = media_runtime["data"] / jxl["output"]
    jxl_bytes = jxl_path.read_bytes()
    assert jxl["format"] == "jxl"
    assert jxl_path.suffix == ".jxl"
    assert len(jxl_bytes) <= 15 * 1024
    assert jxl_bytes.startswith((b"\x00\x00\x00\x0cJXL ", b"\xff\x0a"))

    current = client.get("/api/asset-thumbnails/settings", headers=AUTH).json()
    updated = client.patch(
        "/api/asset-thumbnails/settings",
        headers=AUTH,
        json={"format": "webp", "expected_version": current["revision"]},
    )
    assert updated.status_code == 200, updated.text

    webp = media_repo.generate_thumbnail({"asset_id": asset_id, "width": 256})
    webp_path = media_runtime["data"] / webp["output"]
    assert webp["format"] == "webp"
    assert webp_path.suffix == ".webp"
    assert len(webp_path.read_bytes()) <= 15 * 1024
    with Image.open(webp_path) as output:
        assert output.format == "WEBP"


def test_thumbnail_settings_patch_requires_expected_version() -> None:
    client = TestClient(create_app())

    response = client.patch(
        "/api/asset-thumbnails/settings", headers=AUTH, json={"format": "webp"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"


def test_high_entropy_thumbnail_formats_stay_within_15_kib(
    media_runtime: dict[str, Path],
) -> None:
    client = TestClient(create_app())
    source = media_runtime["roots"] / "noisy-thumbnail-source.png"
    randomizer = random.Random(513)
    image = Image.new("RGB", (320, 180))
    image.putdata(
        [
            (
                randomizer.randrange(256),
                randomizer.randrange(256),
                randomizer.randrange(256),
            )
            for _ in range(320 * 180)
        ]
    )
    image.save(source, format="PNG")
    created = asset_repo.import_assets(
        [{"name": source.name, "kind": "image"}], "image", None
    )
    asset_id = created["asset_ids"][0]
    asset_repo.update_asset(
        asset_id,
        {"display_path": str(source), "expected_version": created["revision"]},
    )

    jxl = media_repo.generate_thumbnail({"asset_id": asset_id, "width": 512})
    assert len((media_runtime["data"] / jxl["output"]).read_bytes()) <= 15 * 1024

    current = client.get("/api/asset-thumbnails/settings", headers=AUTH).json()
    updated = client.patch(
        "/api/asset-thumbnails/settings",
        headers=AUTH,
        json={"format": "webp", "expected_version": current["revision"]},
    )
    assert updated.status_code == 200, updated.text
    webp = media_repo.generate_thumbnail({"asset_id": asset_id, "width": 512})
    assert len((media_runtime["data"] / webp["output"]).read_bytes()) <= 15 * 1024
