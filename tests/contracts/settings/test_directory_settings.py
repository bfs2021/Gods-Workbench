"""QIN-238 目录设置路径、持久化和版本 CAS 契约。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api import routes_settings as routes
from gw.api.app import create_app
from gw.core import storage
from gw.settings.service import (
    AssetStructureService,
    ProviderService,
    StorageSettingsService,
)

EDITOR = {"X-User-Role": "editor", "Authorization": "Bearer cleanroom-test"}


@pytest.fixture()
def api_client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv(storage.ALLOWED_ROOTS_ENV, str(tmp_path))
    monkeypatch.setattr(routes, "default_storage_settings_service", StorageSettingsService())
    monkeypatch.setattr(routes, "default_provider_service", ProviderService())
    monkeypatch.setattr(routes, "default_asset_structure_service", AssetStructureService())
    with TestClient(create_app()) as client:
        yield client


def test_directory_paths_require_absolute_and_invalid_paths_do_not_write(api_client: TestClient, tmp_path: Path):
    for value in ("素材库", ".\\relative\\素材"):
        response = api_client.patch(
            "/api/storage-settings",
            json={"dirs": {"local": value}, "expected_version": 1},
            headers=EDITOR,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "INVALID_PATH"

    unavailable = api_client.patch(
        "/api/storage-settings",
        json={"dirs": {"local": str(tmp_path / "missing")}, "expected_version": 1},
        headers=EDITOR,
    )
    assert unavailable.status_code == 404, unavailable.text
    assert unavailable.json()["detail"]["code"] == "MOUNT_NOT_FOUND"
    snapshot = api_client.get("/api/storage-settings", headers=EDITOR).json()
    assert snapshot["revision"] == 1
    assert snapshot["configured"] is False


def test_directory_path_normalization_handles_nfc_drive_unc_and_windows_casefold():
    nfd_path = "C:\\素材\\Cafe\u0301"
    assert storage.normalize_directory_path(nfd_path) == "C:\\素材\\Café"
    assert storage.normalize_directory_path(r"Z:\测试素材库\临时\..") == r"Z:\测试素材库"
    assert storage.normalize_directory_path(r"\\nas\media\测试素材库\临时\..") == r"\\nas\media\测试素材库"
    assert storage.directory_path_key(r"Z:\素材库\Media") == storage.directory_path_key(r"z:\素材库\MEDIA")


def test_directory_settings_persist_across_service_restart(api_client: TestClient, monkeypatch, tmp_path: Path):
    directory = tmp_path / "中文素材库"
    directory.mkdir()
    saved = api_client.patch(
        "/api/storage-settings",
        json={"dirs": {"local": str(directory)}, "expected_version": 1},
        headers=EDITOR,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["dirs"]["local"] == storage.normalize_directory_path(str(directory))

    monkeypatch.setattr(routes, "default_storage_settings_service", StorageSettingsService())
    restored = api_client.get("/api/storage-settings", headers=EDITOR)
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 2
    assert restored.json()["dirs"]["local"] == str(directory)


def test_storage_and_structure_writes_require_expected_version(api_client: TestClient, tmp_path: Path):
    missing_storage_version = api_client.patch(
        "/api/storage-settings", json={"dirs": {"local": "C:\\素材"}}, headers=EDITOR
    )
    assert missing_storage_version.status_code == 400
    assert missing_storage_version.json()["detail"]["code"] == "EXPECTED_VERSION_REQUIRED"

    legacy_alias = api_client.patch(
        "/api/storage-settings",
        json={"dirs": {"local": str(tmp_path)}, "expected_revision": 1},
        headers=EDITOR,
    )
    assert legacy_alias.status_code == 400
    assert legacy_alias.json()["detail"]["code"] == "EXPECTED_VERSION_REQUIRED"

    missing_create_version = api_client.post(
        "/api/asset-registry/asset-structures",
        json={"kind": "version", "asset_ids": ["asset_a", "asset_b"]},
        headers=EDITOR,
    )
    assert missing_create_version.status_code == 400
    assert missing_create_version.json()["detail"]["code"] == "EXPECTED_VERSION_REQUIRED"

    created = api_client.post(
        "/api/asset-registry/asset-structures",
        json={"kind": "version", "asset_ids": ["asset_a", "asset_b"], "expected_version": 1},
        headers=EDITOR,
    )
    assert created.status_code == 201, created.text
    structure_id = created.json()["structure"]["structure_id"]

    update = api_client.patch(
        f"/api/asset-registry/asset-structures/{structure_id}",
        json={"current_asset_id": "asset_b"},
        headers=EDITOR,
    )
    set_current = api_client.patch(
        f"/api/asset-registry/asset-structures/{structure_id}/current",
        json={"current_asset_id": "asset_b"},
        headers=EDITOR,
    )
    delete = api_client.delete(f"/api/asset-registry/asset-structures/{structure_id}", headers=EDITOR)
    for response in (update, set_current, delete):
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "EXPECTED_VERSION_REQUIRED"



def test_source_read_only_settings_persist_and_use_cas(api_client: TestClient, monkeypatch):
    saved = api_client.patch("/api/storage-settings", headers=EDITOR, json={
        "local_library_read_only": {"source_a": True}, "expected_version": 1,
    })
    assert saved.status_code == 200
    assert saved.json()["local_library_read_only"] == {"source_a": True}
    conflict = api_client.patch("/api/storage-settings", headers=EDITOR, json={
        "local_library_read_only": {"source_a": False}, "expected_version": 1,
    })
    assert conflict.status_code == 409
    monkeypatch.setattr(routes, "default_storage_settings_service", StorageSettingsService())
    restored = api_client.get("/api/storage-settings", headers=EDITOR).json()
    assert restored["local_library_read_only"] == {"source_a": True}
