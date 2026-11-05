"""QIN-548：本地资源 multipart 上传、目录树与文件夹改名契约。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


@pytest.fixture
def local_assets_runtime(tmp_path, monkeypatch):
    root = tmp_path / "allowed_library"
    root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("GW_DATA_DIR", str(data_dir))
    yield root, data_dir


def _client() -> TestClient:
    from gw.api.app import create_app

    return TestClient(create_app())


def test_multipart_upload_is_written_and_returned_in_asset_list(local_assets_runtime):
    root, _data_dir = local_assets_runtime
    content = b"local-resource-upload"

    with _client() as client:
        response = client.post(
            "/api/local-assets/upload",
            headers=AUTH,
            data={"path": str(root), "folder": "incoming", "kind": "local"},
            files=[("files", ("sample.png", content, "image/png"))],
        )
        listing = client.get("/api/local-assets", headers=AUTH, params={"kind": "local"})

    assert response.status_code == 201, response.text
    assert (root / "incoming" / "sample.png").read_bytes() == content
    assert response.json()["count"] == 1
    assert response.json()["files"][0]["display_path"] == "incoming/sample.png"
    assert response.json()["files"][0]["id"]
    assert listing.status_code == 200, listing.text
    assert any(item["display_path"] == "incoming/sample.png" for item in listing.json()["items"])


def test_local_asset_list_returns_nested_folder_tree(local_assets_runtime):
    root, _data_dir = local_assets_runtime
    nested = root / "characters" / "lead" / "portrait.png"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"portrait")
    from gw.asset_library import repository as repo

    repo.index_local_files([str(nested)], "local")
    with _client() as client:
        response = client.get("/api/local-assets", headers=AUTH, params={"kind": "local"})

    assert response.status_code == 200, response.text
    tree = response.json()["tree"]
    assert tree["count"] == 1
    assert tree["children"][0]["path"] == "characters"
    assert tree["children"][0]["children"][0]["path"] == "characters/lead"
    assert response.json()["items"][0]["folder"] == "characters/lead"


def test_local_asset_folder_rename_uses_server_request_fields(local_assets_runtime):
    _root, data_dir = local_assets_runtime
    with _client() as client:
        created = client.post(
            "/api/local-assets/folders", headers=AUTH, json={"name": "before"}
        )
        renamed = client.patch(
            "/api/local-assets/folders",
            headers=AUTH,
            json={"name": "before", "new_name": "after"},
        )

    assert created.status_code == 201, created.text
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["renamed"] is True
    assert not (data_dir / "local_assets" / "before").exists()
    assert (data_dir / "local_assets" / "after").is_dir()


def test_uploaded_source_folder_rename_updates_items_and_tree(local_assets_runtime):
    root, _data_dir = local_assets_runtime
    with _client() as client:
        uploaded = client.post(
            "/api/local-assets/upload",
            headers=AUTH,
            data={"path": str(root), "folder": "incoming", "kind": "local"},
            files=[("files", ("sample.png", b"sample", "image/png"))],
        )
        renamed = client.patch(
            "/api/local-assets/folders",
            headers=AUTH,
            json={
                "kind": "local",
                "root_path": str(root),
                "name": "incoming",
                "new_name": "renamed",
            },
        )

    assert uploaded.status_code == 201, uploaded.text
    assert renamed.status_code == 200, renamed.text
    assert (root / "renamed" / "sample.png").is_file()
    assert not (root / "incoming").exists()
    payload = renamed.json()
    assert payload["renamed"] is True
    assert payload["folder"]["path"] == "renamed"
    assert any(item["display_path"] == "renamed/sample.png" for item in payload["items"])
    assert payload["tree"]["children"][0]["path"] == "renamed"


def test_uploaded_nested_folder_rename_keeps_source_relative_parent(local_assets_runtime):
    root, _data_dir = local_assets_runtime
    with _client() as client:
        uploaded = client.post(
            "/api/local-assets/upload",
            headers=AUTH,
            data={
                "path": str(root),
                "folder": "incoming/characters/lead",
                "kind": "local",
            },
            files=[("files", ("portrait.png", b"portrait", "image/png"))],
        )
        renamed = client.patch(
            "/api/local-assets/folders",
            headers=AUTH,
            json={
                "kind": "local",
                "root_path": str(root),
                "name": "incoming/characters",
                "new_name": "cast",
            },
        )

    assert uploaded.status_code == 201, uploaded.text
    assert renamed.status_code == 200, renamed.text
    assert (root / "incoming" / "cast" / "lead" / "portrait.png").is_file()
    payload = renamed.json()
    assert payload["folder"]["path"] == "incoming/cast"
    assert any(
        item["display_path"] == "incoming/cast/lead/portrait.png"
        for item in payload["items"]
    )
    incoming = payload["tree"]["children"][0]
    assert incoming["path"] == "incoming"
    assert incoming["children"][0]["path"] == "incoming/cast"
