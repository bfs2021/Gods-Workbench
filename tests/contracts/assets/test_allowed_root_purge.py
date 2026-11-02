# -*- coding: utf-8 -*-
"""QIN-517 允许根去重、恢复、彻底删除与素材库联动过滤契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api import routes_asset_registry, routes_settings as routes
from gw.api.app import create_app
from gw.asset_registry import index_jobs, repository as asset_repo
from gw.core import storage
from gw.settings.models import (
    StorageAllowedRootDeleteRequest,
    StorageAllowedRootRequest,
)
from gw.settings.service import (
    AssetStructureService,
    ProviderService,
    StorageAllowedRootsService,
    StorageSettingsService,
)

ADMIN = {"X-User-Role": "admin", "Authorization": "Bearer cleanroom-test"}
EDITOR = {"X-User-Role": "editor", "Authorization": "Bearer cleanroom-test"}


@pytest.fixture()
def client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(routes, "default_storage_settings_service", StorageSettingsService())
    monkeypatch.setattr(routes, "default_storage_allowed_roots_service", StorageAllowedRootsService())
    monkeypatch.setattr(routes, "default_provider_service", ProviderService())
    monkeypatch.setattr(routes, "default_asset_structure_service", AssetStructureService())
    with TestClient(create_app()) as test_client:
        yield test_client


def test_allowed_root_deduplication_re为其_active_instead_of_appending_duplicate(
    client: TestClient, tmp_path: Path
):
    """AC1: 同目录重复登记时，自动复用既有记录并激活，禁止追加重复记录。"""
    test_root = tmp_path / "test_media_vault"
    test_root.mkdir()

    # 1. 首次登记
    resp1 = client.post(
        "/api/storage-settings/roots",
        headers=ADMIN,
        json={"path": str(test_root), "expected_version": 1},
    )
    assert resp1.status_code == 200, resp1.text
    data1 = resp1.json()
    assert len(data1["roots"]) == 1
    root_id = data1["roots"][0]["id"]
    assert data1["roots"][0]["active"] is True

    # 2. 移除该允许根
    resp_del = client.request(
        "DELETE",
        f"/api/storage-settings/roots/{root_id}",
        headers=ADMIN,
        json={"expected_version": 2},
    )
    assert resp_del.status_code == 200, resp_del.text
    data_del = resp_del.json()
    assert len(data_del["roots"]) == 1
    assert data_del["roots"][0]["availability"] == "removed"

    # 3. 再次登记相同目录 -> 必须复用，不能变成 2 条
    resp2 = client.post(
        "/api/storage-settings/roots",
        headers=ADMIN,
        json={"path": str(test_root), "expected_version": 3},
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert len(data2["roots"]) == 1
    assert data2["roots"][0]["id"] == root_id
    assert data2["roots"][0]["active"] is True
    assert data2["roots"][0]["availability"] == "available"

    # 磁盘 JSON 文件中也必须仅有一条记录
    allowed_roots_file = tmp_path / "data" / "allowed_roots.json"
    raw_saved = json.loads(allowed_roots_file.read_text(encoding="utf-8"))
    assert len(raw_saved["roots"]) == 1
    assert raw_saved["roots"][0]["id"] == root_id
    assert raw_saved["roots"][0]["active"] is True


def test_allowed_root_restore_endpoint(
    client: TestClient, tmp_path: Path
):
    """AC4 & AC6: 通过恢复端点重新激活已移除根。"""
    test_root = tmp_path / "restore_vault"
    test_root.mkdir()

    # 登记并移除
    client.post(
        "/api/storage-settings/roots",
        headers=ADMIN,
        json={"path": str(test_root), "expected_version": 1},
    )
    root_id = storage.root_identity_id(test_root)
    client.request(
        "DELETE",
        f"/api/storage-settings/roots/{root_id}",
        headers=ADMIN,
        json={"expected_version": 2},
    )

    # 普通编辑员无权恢复
    forbidden = client.post(
        f"/api/storage-settings/roots/{root_id}/restore",
        headers=EDITOR,
        json={"expected_version": 3},
    )
    assert forbidden.status_code == 403

    # 管理员调用恢复端点
    restored = client.post(
        f"/api/storage-settings/roots/{root_id}/restore",
        headers=ADMIN,
        json={"expected_version": 3},
    )
    assert restored.status_code == 200, restored.text
    data = restored.json()
    restored_item = next(item for item in data["roots"] if item["id"] == root_id)
    assert restored_item["active"] is True
    assert restored_item["availability"] == "available"


def test_allowed_root_restore_allows_asset_source_registration(
    client: TestClient, monkeypatch, tmp_path: Path
):
    """恢复旧根后，规范化后的同目录仍可完成新资源库来源登记。"""
    root = tmp_path / "restore_alias_vault"
    library = root / "library"
    library.mkdir(parents=True)

    settings_service = StorageSettingsService()
    monkeypatch.setattr(routes, "default_storage_settings_service", settings_service)
    monkeypatch.setattr(
        routes_asset_registry, "default_storage_settings_service", settings_service
    )

    class Jobs:
        def submit(self, *_args, **_kwargs):
            return {"job_id": "job_restore_source", "status": "queued"}

    monkeypatch.setattr(index_jobs, "service", lambda: Jobs())

    added = client.post(
        "/api/storage-settings/roots",
        headers=ADMIN,
        json={"path": str(root), "expected_version": 1},
    )
    assert added.status_code == 200, added.text
    root_id = added.json()["roots"][0]["id"]
    removed = client.request(
        "DELETE",
        f"/api/storage-settings/roots/{root_id}",
        headers=ADMIN,
        json={"expected_version": 2},
    )
    assert removed.status_code == 200, removed.text
    restored = client.post(
        f"/api/storage-settings/roots/{root_id}/restore",
        headers=ADMIN,
        json={"expected_version": 3},
    )
    assert restored.status_code == 200, restored.text
    assert next(item for item in restored.json()["roots"] if item["id"] == root_id)["active"]

    # 模拟 Windows 对同一目录的路径解析会把素材路径变成另一种规范拼写。
    path_type = type(root)
    original_resolve = path_type.resolve
    canonical_root = tmp_path / "canonical_vault"
    canonical_library = canonical_root / "library"

    def resolve_path_alias(path, *args, **kwargs):
        if path == root:
            return canonical_root
        if path == library:
            return canonical_library
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "resolve", resolve_path_alias)
    registered = client.post(
        "/api/asset-registry/reindex",
        headers=ADMIN,
        json={
            "background": True,
            "hash_files": True,
            "max_files": 250000,
            "roots": ["asset_recovered"],
            "register_root": {
                "id": "asset_recovered",
                "name": "恢复后的素材",
                "path": str(library),
                "expected_version": 1,
            },
        },
    )
    assert registered.status_code == 202, registered.text
    assert registered.json()["registered_root"]["id"] == "asset_recovered"


def test_windows_path_namespace_keeps_unconfirmed_unc_alias_separate():
    """映射盘与 UNC 拼写仍须显式确认，不能因 resolve 结果相同而自动合并。"""
    assert not storage._same_path_namespace(
        r"\\server\share\library", Path(r"Z:\share\library")
    )
    assert storage._same_path_namespace(
        r"Z:\share\library", Path(r"z:/share/library")
    )


def test_allowed_root_purge_cas_and_cascade_cleanup_without_touching_files(
    client: TestClient, tmp_path: Path
):
    """AC5: 彻底删除彻底清空系统记录与缓存，但严禁触碰用户源文件。"""
    test_root = tmp_path / "purge_vault"
    test_root.mkdir()
    sample_file = test_root / "my_precious_video.mp4"
    sample_content = b"ORIGINAL_CREATIVE_CONTENT_DO_NOT_DELETE"
    sample_file.write_bytes(sample_content)

    # 1. 登记允许根
    client.post(
        "/api/storage-settings/roots",
        headers=ADMIN,
        json={"path": str(test_root), "expected_version": 1},
    )
    root_id = storage.root_identity_id(test_root)

    # 2. 模拟注册表中已入库该根下的资产
    asset_id = "ast_purge_test_001"
    asset_repo.state().mutate(
        lambda raw: raw["assets"].update(
            {
                asset_id: asset_repo._plain_asset(
                    asset_id,
                    "my_precious_video.mp4",
                    "video",
                    root_id=root_id,
                    storage_root_id=root_id,
                    canonical_relpath="my_precious_video.mp4",
                    display_path="my_precious_video.mp4",
                )
            }
        )
    )
    # 模拟写入检索索引
    asset_repo._upsert_asset_search_documents(
        [asset_repo.state().read()["assets"][asset_id]]
    )
    assert asset_id in asset_repo.state().read()["assets"]

    # 3. 彻底删除允许根
    purged = client.request(
        "DELETE",
        f"/api/storage-settings/roots/{root_id}/purge",
        headers=ADMIN,
        json={"expected_version": 2},
    )
    assert purged.status_code == 200, purged.text
    roots = purged.json()["roots"]
    assert not any(item["id"] == root_id for item in roots)

    # 4. 验证系统级数据已被清除
    assert asset_id not in asset_repo.state().read()["assets"]

    # 5. 核心安全边界断言：用户原始文件必须完好无损！
    assert test_root.is_dir()
    assert sample_file.is_file()
    assert sample_file.read_bytes() == sample_content


def test_list_assets_filters_removed_roots_by_default(
    client: TestClient, tmp_path: Path
):
    """AC7: 资产看板默认只展示活跃根素材，不展示已移除允许根名下的素材。"""
    active_root = tmp_path / "active_vault"
    removed_root = tmp_path / "removed_vault"
    active_root.mkdir()
    removed_root.mkdir()

    roots_service = routes.default_storage_allowed_roots_service
    roots_service.add_root(StorageAllowedRootRequest(path=str(active_root), expected_version=1))
    roots_service.add_root(StorageAllowedRootRequest(path=str(removed_root), expected_version=2))

    active_root_id = storage.root_identity_id(active_root)
    removed_root_id = storage.root_identity_id(removed_root)

    # 登记资产
    asset_repo.state().mutate(
        lambda raw: raw["assets"].update(
            {
                "ast_active": asset_repo._plain_asset(
                    "ast_active", "active.png", "image",
                    root_id=active_root_id, storage_root_id=active_root_id,
                    canonical_relpath="active.png", display_path="active.png",
                ),
                "ast_removed": asset_repo._plain_asset(
                    "ast_removed", "removed.png", "image",
                    root_id=removed_root_id, storage_root_id=removed_root_id,
                    canonical_relpath="removed.png", display_path="removed.png",
                ),
            }
        )
    )

    # 移除第二个根
    roots_service.remove_root(
        removed_root_id, StorageAllowedRootDeleteRequest(expected_version=3)
    )

    # 默认查询（全部资产）不应包含已移除根下的素材
    resp = client.get("/api/asset-registry/assets", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    item_ids = {x["asset_id"] for x in items}
    assert "ast_active" in item_ids
    assert "ast_removed" not in item_ids

    # 显式查询 include_removed=true 时可保留
    resp_all = client.get("/api/asset-registry/assets?include_removed=true", headers=ADMIN)
    assert resp_all.status_code == 200, resp_all.text
    all_item_ids = {x["asset_id"] for x in resp_all.json()["items"]}
    assert "ast_active" in all_item_ids
    assert "ast_removed" in all_item_ids


def test_unassigned_and_facets_filter_removed_roots(
    client: TestClient, tmp_path: Path
):
    """测试未归属收件箱与分类统计在允许根移除后自动联动过滤。"""
    active_root = tmp_path / "active_vault_2"
    removed_root = tmp_path / "removed_vault_2"
    active_root.mkdir()
    removed_root.mkdir()

    roots_service = routes.default_storage_allowed_roots_service
    roots_service.add_root(StorageAllowedRootRequest(path=str(active_root), expected_version=1))
    roots_service.add_root(StorageAllowedRootRequest(path=str(removed_root), expected_version=2))

    active_root_id = storage.root_identity_id(active_root)
    removed_root_id = storage.root_identity_id(removed_root)

    # 登记资产（无 project_id，属于未归属）
    asset_repo.state().mutate(
        lambda raw: raw["assets"].update(
            {
                "ast_unassigned_active": asset_repo._plain_asset(
                    "ast_unassigned_active", "active.png", "image",
                    root_id=active_root_id, storage_root_id=active_root_id,
                    canonical_relpath="active.png", display_path="active.png",
                ),
                "ast_unassigned_removed": asset_repo._plain_asset(
                    "ast_unassigned_removed", "removed.png", "image",
                    root_id=removed_root_id, storage_root_id=removed_root_id,
                    canonical_relpath="removed.png", display_path="removed.png",
                ),
            }
        )
    )

    # 移除第二个根
    roots_service.remove_root(
        removed_root_id, StorageAllowedRootDeleteRequest(expected_version=3)
    )

    # 1. 验证未归属收件箱
    unassigned_resp = client.get("/api/assets/unassigned", headers=ADMIN)
    assert unassigned_resp.status_code == 200, unassigned_resp.text
    unassigned_ids = {x["asset_id"] for x in unassigned_resp.json()["assets"]}
    assert "ast_unassigned_active" in unassigned_ids
    assert "ast_unassigned_removed" not in unassigned_ids

    # 2. 验证 facets 分类统计
    facets_resp = client.get("/api/asset-registry/facets", headers=ADMIN)
    assert facets_resp.status_code == 200, facets_resp.text
    categories = {c["name"]: c["count"] for c in facets_resp.json()["categories"]}
    assert categories.get("image") == 1  # 只有 active 的那张图片，被移除的不计入

    # 3. 验证结构化查询
    structured_data = asset_repo.list_assets(
        limit=50,
        offset=0,
        query=None,
        kind=None,
        archived=None,
        structured=True,
        scope=None,
        text="",
        filters={"status": {"asset": ["active"]}},
    )
    structured_ids = {x["asset_id"] for x in structured_data["items"]}
    assert "ast_unassigned_active" in structured_ids
    assert "ast_unassigned_removed" not in structured_ids


def test_allowed_root_duplicate_records_cleaned_automatically(
    client: TestClient, tmp_path: Path
):
    """测试历史遗留的多条相同路径记录在读取和操作时被自动去重合并。"""
    roots_file = tmp_path / "data" / "allowed_roots.json"
    roots_file.parent.mkdir(parents=True, exist_ok=True)
    dup_path = str(tmp_path / "dup_folder")
    dup_alias = str(tmp_path / "dup_alias")
    Path(dup_path).mkdir(exist_ok=True)
    Path(dup_alias).mkdir(exist_ok=True)
    root_id = storage.root_identity_id(Path(dup_path))

    # 写入模拟的脏数据：同一路径存在 3 条记录
    dirty_state = {
        "schema_version": 1,
        "revision": 5,
        "system_aliases": {},
        "roots": [
            {"id": root_id, "path": dup_path, "active": False, "aliases": []},
            {"id": root_id, "path": dup_path, "active": False, "aliases": []},
            {"id": root_id, "path": dup_path, "active": False, "aliases": [dup_alias]},
        ],
    }
    roots_file.write_text(json.dumps(dirty_state), encoding="utf-8")

    # 1. 读 snapshot -> 必须自动去重为 1 条
    service = StorageAllowedRootsService()
    snapshot = service.get_snapshot()
    matching = [r for r in snapshot.roots if r.id == root_id]
    assert len(matching) == 1
    assert matching[0].active is False
    assert dup_alias in matching[0].aliases

    # 2. 执行恢复操作 -> 文件内容必须被彻底清洗为单条活跃记录
    service.restore_root(root_id, StorageAllowedRootDeleteRequest(expected_version=5))
    saved = json.loads(roots_file.read_text(encoding="utf-8"))
    assert len(saved["roots"]) == 1
    assert saved["roots"][0]["id"] == root_id
    assert saved["roots"][0]["active"] is True
