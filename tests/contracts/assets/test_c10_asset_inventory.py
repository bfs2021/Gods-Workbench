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

"""C10-T2 资产清单、分页、结构化搜索与缩略图真实读回契约。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.core import storage

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}

# 1x1 PNG：只用于在临时允许根目录中产生可被真实缩略图流程读取的媒体。
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_root = tmp_path / "data"
    allowed_root = tmp_path / "media"
    data_root.mkdir()
    allowed_root.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed_root))
    return TestClient(create_app())


def _import(client: TestClient, items: list[dict[str, object]]) -> list[str]:
    response = client.post(
        "/api/asset-registry/assets/import", headers=AUTH, json={"items": items}
    )
    assert response.status_code == 200, response.text
    return response.json()["asset_ids"]


def test_c10_inventory_cursor_pages_are_stable_and_readable(client: TestClient) -> None:
    items = [
        {"name": "zeta.png", "kind": "image", "size_bytes": 50},
        {"name": "alpha.png", "kind": "image", "size_bytes": 10},
        {"name": "gamma.mp4", "kind": "video", "size_bytes": 40},
        {"name": "beta.png", "kind": "image", "size_bytes": 20},
        {"name": "delta.txt", "kind": "document", "size_bytes": 30},
    ]
    created = _import(client, items)
    path = "/api/asset-registry/assets"

    pages: list[str] = []
    response = client.get(path, headers=AUTH, params={"limit": 2, "sort": "name"})
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["total"] == len(created) and first["has_more"] is True
    pages.extend(item["asset_id"] for item in first["items"])

    cursor = first["next_cursor"]
    while cursor:
        response = client.get(
            path, headers=AUTH, params={"limit": 2, "sort": "name", "cursor": cursor}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        pages.extend(item["asset_id"] for item in body["items"])
        cursor = body["next_cursor"]

    assert pages == [created[1], created[3], created[4], created[2], created[0]]
    assert len(pages) == len(set(pages)) == len(created)
    for asset_id in pages:
        detail = client.get(f"{path}/{asset_id}", headers=AUTH)
        assert detail.status_code == 200, detail.text
        assert detail.json()["asset"]["asset_id"] == asset_id

    empty = client.get(path, headers=AUTH, params={"limit": 2, "offset": 99})
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []
    assert empty.json()["has_more"] is False and empty.json()["next_cursor"] is None

    stale_cursor = first["next_cursor"]
    assert (
        client.patch(
            f"{path}/{created[0]}", headers=AUTH, json={"name": "zeta-renamed.png"}
        ).status_code
        == 200
    )
    stale = client.get(
        path, headers=AUTH, params={"sort": "name", "cursor": stale_cursor}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_c10_inventory_structured_filters_and_bad_queries_are_explicit(
    client: TestClient,
) -> None:
    created = _import(
        client,
        [
            {"name": "hero.png", "kind": "image", "size_bytes": 30},
            {"name": "hero-final.mp4", "kind": "video", "size_bytes": 90},
            {"name": "draft.txt", "kind": "document", "size_bytes": 5},
        ],
    )
    path = "/api/asset-registry/assets"
    tags = client.post(
        path + "/tags",
        headers=AUTH,
        json={"asset_ids": [created[0], created[1]], "names": ["hero"]},
    )
    assert tags.status_code == 200, tags.text
    hero_tag_id = tags.json()["tags"][0]["tag_id"]
    pending = client.post(
        path + "/tags",
        headers=AUTH,
        json={"asset_ids": [created[1]], "names": ["pending"]},
    )
    assert pending.status_code == 200, pending.text

    project = client.post(
        "/api/asset-registry/projects",
        headers=AUTH,
        json={"name": "C10 Inventory", "project_type": "film"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project"]["project_id"]
    bound = client.post(
        f"/api/asset-registry/projects/{project_id}/assets",
        headers=AUTH,
        json={"asset_ids": [created[1]]},
    )
    assert bound.status_code == 200, bound.text
    assert [
        a["asset_id"]
        for a in client.get(
            path, headers=AUTH, params={"project_id": project_id}
        ).json()["items"]
    ] == [created[1]]

    def ids(**params: object) -> list[str]:
        result = client.get(path, headers=AUTH, params=params)
        assert result.status_code == 200, result.text
        return [item["asset_id"] for item in result.json()["items"]]

    assert ids(query="hero") == [created[0], created[1]]
    assert ids(tag=hero_tag_id) == [created[0], created[1]]
    assert ids(include_tags="hero", exclude_tags="pending") == [created[0]]
    assert ids(category="image") == [created[0]]
    assert ids(category="unknown-category") == []
    assert (
        client.patch(
            f"{path}/{created[2]}", headers=AUTH, json={"archived": True}
        ).status_code
        == 200
    )
    assert ids(archived=True) == [created[2]]

    for params, code in (
        ({"sort": "invented"}, "UNSUPPORTED_OPTION"),
        ({"view": "invented"}, "UNSUPPORTED_OPTION"),
        ({"recent_days": 0}, "INVALID_REQUEST"),
    ):
        rejected = client.get(path, headers=AUTH, params=params)
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["detail"]["code"] == code

    unknown_root = client.get(path, headers=AUTH, params={"root_id": "root_0001"})
    assert unknown_root.status_code == 404, unknown_root.text
    assert unknown_root.json()["detail"]["code"] == "ROOT_NOT_FOUND"


def test_c10_inventory_media_thumbnail_and_missing_dependency_are_truthful(
    client: TestClient, tmp_path: Path
) -> None:
    source = tmp_path / "media" / "hero.png"
    source.write_bytes(PNG_BYTES)
    created = _import(
        client,
        [
            {"name": source.name, "kind": "image"},
            {"name": "no-media.png", "kind": "image"},
        ],
    )
    path = "/api/asset-registry/assets"
    assert (
        client.patch(
            f"{path}/{created[0]}",
            headers=AUTH,
            json={
                "display_path": source.name,
                "root_id": storage.root_identity_id(source.parent.resolve()),
            },
        ).status_code
        == 200
    )

    listed = client.get(path, headers=AUTH, params={"query": "hero"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["asset_id"] == created[0]
    assert listed.json()["items"][0]["display_path"] == source.name
    media = client.get(f"{path}/{created[0]}/media", headers=AUTH)
    assert media.status_code == 200 and media.content == PNG_BYTES

    thumbnail = client.post(
        "/api/asset-thumbnails/generate",
        headers=AUTH,
        json={"asset_id": created[0], "width": 64},
    )
    assert thumbnail.status_code == 200, thumbnail.text
    thumbnail_path = thumbnail.json()["output"]
    # 缓存位置由设置决定：cache_folder 子目录 + 素材源根分桶，文件名仍按素材身份。
    assert thumbnail_path.startswith("media_output/god-cache/")
    assert Path(thumbnail_path).name == f"{created[0]}.jpg.png"
    downloaded = client.get(
        "/api/download-output", headers=AUTH, params={"path": thumbnail_path}
    )
    assert downloaded.status_code == 200 and downloaded.content

    removed = client.post(
        "/api/asset-thumbnails/delete", headers=AUTH, json={"asset_ids": [created[0]]}
    )
    assert removed.status_code == 200, removed.text
    missing_thumbnail = client.get(
        "/api/download-output", headers=AUTH, params={"path": thumbnail_path}
    )
    assert missing_thumbnail.status_code == 404
    assert (
        client.get(f"{path}/{created[1]}/media", headers=AUTH).json()["detail"]["code"]
        == "MEDIA_NOT_AVAILABLE"
    )
    assert client.get(path).status_code == 401
    assert client.get(f"{path}/{created[0]}/media").status_code == 401
