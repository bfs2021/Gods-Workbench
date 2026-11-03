from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient

from gw.asset_library.repository import _local_root_identity
from gw.api.app import create_app
from gw.core import storage

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
GOVERNOR = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "governor"}


def _client(monkeypatch, tmp_path: Path, allowed_roots: list[Path]) -> TestClient:
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "gw-data"))
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(tmp_path / "gw-auth" / "auth.sqlite3"))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(tmp_path / "gw-video"))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", os.pathsep.join(str(path) for path in allowed_roots))
    return TestClient(create_app())


def _register_local_asset(client: TestClient, root: Path) -> str:
    response = client.post(
        f"{BASE}/assets/import",
        headers=AUTH,
        json={"items": [{"name": "original.txt", "kind": "document", "display_path": "original.txt"}]},
    )
    assert response.status_code == 200, response.text
    asset_id = response.json()["asset_ids"][0]
    revision = client.get(f"{BASE}/status", headers=AUTH).json()["revision"]
    patched = client.patch(
        f"{BASE}/assets/{asset_id}",
        headers=AUTH,
        json={
            "display_path": "original.txt",
            "root_id": storage.root_identity_id(root.resolve()),
            "expected_version": revision,
        },
    )
    assert patched.status_code == 200, patched.text
    return asset_id


def _patch_trash_directory(client: TestClient, path: Path) -> None:
    current = client.get("/api/storage-settings", headers=AUTH)
    assert current.status_code == 200, current.text
    dirs = {**current.json().get("dirs", {}), "trash": str(path)}
    updated = client.patch(
        "/api/storage-settings",
        headers=AUTH,
        json={"dirs": dirs, "expected_version": current.json()["revision"]},
    )
    assert updated.status_code == 200, updated.text


def _registry_revision(client: TestClient) -> int:
    return int(client.get(f"{BASE}/status", headers=AUTH).json()["revision"])


def _delete_asset(
    client: TestClient,
    asset_id: str,
    recycler_mode: str | None = None,
    *,
    mode_header: bool = False,
):
    payload = {"expected_version": _registry_revision(client)}
    headers = dict(AUTH)
    if recycler_mode is not None:
        if mode_header:
            headers["X-GW-Recycle-Mode"] = recycler_mode
        else:
            payload["recycler_mode"] = recycler_mode
    return client.request(
        "DELETE",
        f"{BASE}/assets/{asset_id}",
        headers=headers,
        json=payload,
    )


def test_empty_recycler_setting_keeps_existing_managed_output_path(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    with _client(monkeypatch, tmp_path, [source]) as client:
        asset_id = _register_local_asset(client, source)
        response = _delete_asset(client, asset_id)
        if response.status_code == 200:
            recycler = response.json()["operation"]["recycler"]
            recycled_path = storage.data_root() / Path(
                *PurePosixPath(recycler["original_file"]).parts
            )
            assert recycled_path.read_text(encoding="utf-8") == "original"
            restored = client.post(
                f"{BASE}/recycle-bin/{response.json()['recycle_entry_id']}/restore",
                headers=GOVERNOR,
                json={"expected_version": _registry_revision(client)},
            )

    assert response.status_code == 200, response.text
    recycler = response.json()["operation"]["recycler"]
    assert recycler["original_file"].startswith("registry_uploads/god-recycler/document/")
    assert "recycler_root_id" not in recycler
    assert restored.status_code == 200, restored.text
    assert (source / "original.txt").read_text(encoding="utf-8") == "original"


def test_custom_recycler_directory_round_trips_original_and_derived_files(
    monkeypatch, tmp_path
):
    source = tmp_path / "source"
    trash = tmp_path / "trash"
    source.mkdir()
    trash.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    (source / "preview.png").write_bytes(b"preview")
    with _client(monkeypatch, tmp_path, [source, trash]) as client:
        asset_id = _register_local_asset(client, source)
        derived = client.post(
            f"{BASE}/assets/{asset_id}/derived-assets",
            headers=AUTH,
            json={"name": "preview.png", "path": str(source / "preview.png")},
        )
        assert derived.status_code == 200, derived.text
        _patch_trash_directory(client, trash)

        deleted = _delete_asset(client, asset_id, recycler_mode="source", mode_header=True)
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        entry_id = body["recycle_entry_id"]
        recycler = body["operation"]["recycler"]
        assert recycler["recycler_root_id"]
        original_path = trash.joinpath(*PurePosixPath(recycler["original_file"]).parts)
        manifest_path = trash.joinpath(*PurePosixPath(recycler["manifest"]).parts)
        assert original_path.read_text(encoding="utf-8") == "original"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["recycler_root_id"] == recycler["recycler_root_id"]
        assert len(recycler["derived"]) == 1

        restored = client.post(
            f"{BASE}/recycle-bin/{entry_id}/restore",
            headers=GOVERNOR,
            json={"expected_version": _registry_revision(client)},
        )

    assert restored.status_code == 200, restored.text
    assert (source / "original.txt").read_text(encoding="utf-8") == "original"
    assert (source / "preview.png").read_bytes() == b"preview"


def test_source_mode_recycles_under_asset_root_and_restores(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    with _client(monkeypatch, tmp_path, [source]) as client:
        asset_id = _register_local_asset(client, source)
        deleted = _delete_asset(client, asset_id, recycler_mode="source")
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        recycler = body["operation"]["recycler"]
        assert recycler["recycler_root_id"] == _local_root_identity(source.resolve())
        original_path = source.joinpath(*PurePosixPath(recycler["original_file"]).parts)
        assert original_path.read_text(encoding="utf-8") == "original"
        restored = client.post(
            f"{BASE}/recycle-bin/{body['recycle_entry_id']}/restore",
            headers=GOVERNOR,
            json={"expected_version": _registry_revision(client)},
        )

    assert restored.status_code == 200, restored.text
    assert (source / "original.txt").read_text(encoding="utf-8") == "original"


def test_explicit_custom_recycler_directory_overrides_source_mode(monkeypatch, tmp_path):
    source = tmp_path / "source"
    trash = tmp_path / "trash"
    source.mkdir()
    trash.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    with _client(monkeypatch, tmp_path, [source, trash]) as client:
        asset_id = _register_local_asset(client, source)
        _patch_trash_directory(client, trash)
        deleted = _delete_asset(client, asset_id, recycler_mode="source")

    assert deleted.status_code == 200, deleted.text
    recycler = deleted.json()["operation"]["recycler"]
    assert recycler["recycler_root_id"] == _local_root_identity(trash.resolve())
    assert trash.joinpath(*PurePosixPath(recycler["original_file"]).parts).is_file()


def test_custom_recycler_path_outside_allowed_roots_fails_closed(monkeypatch, tmp_path):
    source = tmp_path / "source"
    trash = tmp_path / "unauthorized-trash"
    source.mkdir()
    trash.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    with _client(monkeypatch, tmp_path, [source]) as client:
        asset_id = _register_local_asset(client, source)
        _patch_trash_directory(client, trash)

        deleted = _delete_asset(client, asset_id)

        assert deleted.status_code == 403, deleted.text
        assert (source / "original.txt").read_text(encoding="utf-8") == "original"
        assert client.get(f"{BASE}/assets/{asset_id}", headers=AUTH).status_code == 200
        assert list(trash.iterdir()) == []
