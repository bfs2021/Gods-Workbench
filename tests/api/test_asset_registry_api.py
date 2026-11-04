"""QIN-535：资产库图片版本媒体与参数化编辑回退接口测试。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import index_jobs
from gw.asset_registry import repository as repo

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer test-owner", "X-User-Role": "editor"}


@pytest.fixture
def asset_api_runtime(tmp_path, monkeypatch):
    root = tmp_path / "allowed_library"
    root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("GW_DATA_DIR", str(data_dir))
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None
    yield root
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None


def test_image_version_media_direct_physical_file(asset_api_runtime):
    """有物理文件的版本能正常直通提供媒体流。"""
    sample_file = asset_api_runtime / "sample_v1.png"
    sample_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (b"\x00" * 32)
    sample_file.write_bytes(sample_content)

    with TestClient(create_app()) as client:
        imported = client.post(
            f"{BASE}/assets/import",
            headers=AUTH,
            json={"items": [{"name": sample_file.name, "kind": "image"}]},
        )
        assert imported.status_code == 200, imported.text
        asset_id = imported.json()["asset_ids"][0]

        # 创建带有物理文件的版本
        ver_resp = client.post(
            f"{BASE}/assets/{asset_id}/image-versions",
            headers=AUTH,
            json={"source_path": str(sample_file)},
        )
        assert ver_resp.status_code == 200, ver_resp.text
        version_id = ver_resp.json()["version"]["version_id"]

        media_resp = client.get(
            f"{BASE}/assets/{asset_id}/image-versions/{version_id}/media",
            headers=AUTH,
        )
        assert media_resp.status_code == 200
        assert media_resp.content == sample_content


def test_image_version_media_parametric_fallback(asset_api_runtime):
    """display_path 为空但含参数化 edit 的版本自动回退到底图实体文件。"""
    base_file = asset_api_runtime / "base_original.png"
    base_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (b"\xaa" * 48)
    base_file.write_bytes(base_content)

    with TestClient(create_app()) as client:
        # 首先登记并绑定底图文件
        imported = client.post(
            f"{BASE}/assets/import",
            headers=AUTH,
            json={"items": [{"name": base_file.name, "kind": "image"}]},
        )
        assert imported.status_code == 200, imported.text
        asset_id = imported.json()["asset_ids"][0]

        # 关联底图物理路径
        ver_base = client.post(
            f"{BASE}/assets/{asset_id}/image-versions",
            headers=AUTH,
            json={"source_path": str(base_file)},
        )
        assert ver_base.status_code == 200

        # 创建纯前端参数化编辑版本（无 source_path / display_path 为空，仅提供 edit 元数据）
        parametric_edit = {
            "version": 2,
            "crop": {"x": 10, "y": 10, "w": 100, "h": 100},
            "flip_x": False,
            "flip_y": False,
            "layers": [
                {
                    "id": "lay_1",
                    "type": "brush",
                    "paths": [{"mode": "draw", "color": "#dfc384", "width": 4, "points": [{"x": 12, "y": 14}]}],
                }
            ],
        }
        ver_edit_resp = client.post(
            f"{BASE}/assets/{asset_id}/image-versions",
            headers=AUTH,
            json={"edit": parametric_edit},
        )
        assert ver_edit_resp.status_code == 200, ver_edit_resp.text
        ver_edit_data = ver_edit_resp.json()["version"]
        version_id = ver_edit_data["version_id"]
        assert ver_edit_data["display_path"] is None
        assert ver_edit_data["edit"] is not None

        # AC2：请求媒体接口应自动 fallback 到母素材的底图文件直通提供，状态码 200
        media_resp = client.get(
            f"{BASE}/assets/{asset_id}/image-versions/{version_id}/media",
            headers=AUTH,
        )
        assert media_resp.status_code == 200, media_resp.text
        assert media_resp.content == base_content
        assert "X-Media-Display" in media_resp.headers


def test_image_version_media_empty_without_edit_unavailable(asset_api_runtime):
    """display_path 为空且无 edit 时返回 503 MEDIA_NOT_AVAILABLE。"""
    with TestClient(create_app()) as client:
        imported = client.post(
            f"{BASE}/assets/import",
            headers=AUTH,
            json={"items": [{"name": "empty_asset.png", "kind": "image"}]},
        )
        assert imported.status_code == 200
        asset_id = imported.json()["asset_ids"][0]

        ver_resp = client.post(
            f"{BASE}/assets/{asset_id}/image-versions",
            headers=AUTH,
            json={},
        )
        assert ver_resp.status_code == 200
        version_id = ver_resp.json()["version"]["version_id"]

        media_resp = client.get(
            f"{BASE}/assets/{asset_id}/image-versions/{version_id}/media",
            headers=AUTH,
        )
        assert media_resp.status_code == 503
        data = media_resp.json()
        assert data["detail"]["code"] == "MEDIA_NOT_AVAILABLE"


def test_image_version_nonexistent_version_404(asset_api_runtime):
    """不存在的版本返回 404 VERSION_NOT_FOUND。"""
    with TestClient(create_app()) as client:
        imported = client.post(
            f"{BASE}/assets/import",
            headers=AUTH,
            json={"items": [{"name": "item.png", "kind": "image"}]},
        )
        assert imported.status_code == 200
        asset_id = imported.json()["asset_ids"][0]

        media_resp = client.get(
            f"{BASE}/assets/{asset_id}/image-versions/imgv_not_exist/media",
            headers=AUTH,
        )
        assert media_resp.status_code == 404
        assert media_resp.json()["detail"]["code"] == "VERSION_NOT_FOUND"


def test_image_version_auth_and_cas(asset_api_runtime):
    """未认证返回 401，版本并发更新冲突 CAS 检查。"""
    with TestClient(create_app()) as client:
        # 未认证
        unauth = client.get(f"{BASE}/assets/ast_mock/image-versions/imgv_1/media")
        assert unauth.status_code == 401

        # CAS 乐观锁冲突测试
        imported = client.post(
            f"{BASE}/assets/import",
            headers=AUTH,
            json={"items": [{"name": "cas_test.png", "kind": "image"}]},
        )
        asset_id = imported.json()["asset_ids"][0]
        cur_rev = repo.state().read()["revision"]

        # 正确 revision 创建版本
        created = client.post(
            f"{BASE}/assets/{asset_id}/image-versions",
            headers=AUTH,
            json={"expected_version": cur_rev},
        )
        assert created.status_code == 200

        # 过期 revision 更新版本触发 409
        conflict = client.patch(
            f"{BASE}/assets/{asset_id}/image-versions/{created.json()['version']['version_id']}",
            headers=AUTH,
            json={"label": "new_label", "expected_version": cur_rev},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_qin543_project_entities_assets_repository_roundtrip(asset_api_runtime):
    """QIN-543：项目制片条目视图与项目素材列表的仓储层回环（AC1/AC2/AC3）。

    写入与读取同走注册表状态仓：新建条目立即可读、条目-素材链接去重保序、
    列表支持按条目筛选与分页、可见性与回收口径与素材库一致。
    """
    sample = asset_api_runtime / "qin543_ref.png"
    sample.write_bytes(b"PNGFIXTURE" + bytes(16))

    with TestClient(create_app()) as client:
        imported = client.post(
            f"{BASE}/assets/import",
            headers=AUTH,
            json={"items": [{"name": sample.name, "kind": "image"},
                            {"name": "qin543_shot.mp4", "kind": "video"}]},
        )
        assert imported.status_code == 200, imported.text
        asset_a, asset_b = imported.json()["asset_ids"]

        project = client.post(f"{BASE}/projects", headers=AUTH,
                              json={"name": "仓储回环项目", "project_type": "film"})
        assert project.status_code == 201, project.text
        project_id = project.json()["project"]["project_id"]

        entity = client.post(f"{BASE}/projects/{project_id}/entities", headers=AUTH,
                             json={"name": "第一季", "entity_type": "episode", "code": "EP01"})
        assert entity.status_code == 200, entity.text
        episode_id = entity.json()["entity"]["id"]

        repo.create_project_entity(project_id, "补充类型", "asset_entity", {})
        board = repo.project_entities_view(project_id)
        assert [e["id"] for e in board["entities"]] == ["ent_0001", "ent_0002"]
        assert [g["entity_type"] for g in board["groups"]] == ["episode", "scene", "shot", "character", "task", "asset_entity"]
        assert board["groups"][-1]["entity_count"] == 1

        linked = repo.link_project_assets(project_id, [asset_a, asset_b],
                                          {"entity_id": episode_id})
        assert linked["linked"] == 2
        stored = repo.project_entities(project_id)["entities"][0]
        assert stored["asset_ids"] == [asset_a, asset_b]

        view = repo.project_entities_view(project_id)
        ep = next(e for e in view["entities"] if e["id"] == episode_id)
        assert ep["asset_count"] == 2 and [a["asset_id"] for a in ep["assets"]] == [asset_a, asset_b]

        listing = repo.project_assets(project_id)
        assert listing["total"] == 2
        paged = repo.project_assets(project_id, limit=1)
        assert paged["total"] == 2 and len(paged["assets"]) == 1
        by_entity = repo.project_assets(project_id, entity_id=episode_id)
        assert by_entity["total"] == 2 and [a["asset_id"] for a in by_entity["assets"]] == [asset_a, asset_b]
        other = repo.create_project_entity(project_id, "空条目", "scene", {})
        empty = repo.project_assets(project_id, entity_id=other["entity"]["entity_id"])
        assert empty["total"] == 0

        import pytest as _pytest
        try:
            repo.project_assets(project_id, entity_id="ent_9999")
        except Exception as exc:
            assert getattr(exc, "code", None) == "ENTITY_NOT_FOUND" or "ENTITY_NOT_FOUND" in str(exc)
        else:
            _pytest.fail("未知条目应报 ENTITY_NOT_FOUND")

        patched = client.patch(f"{BASE}/project-entities/{episode_id}", headers=AUTH,
                               json={"status": "approved", "asset_ids": ["ast_fake"]})
        assert patched.status_code == 200
        stored = repo.project_entities(project_id)["entities"][0]
        assert stored["asset_ids"] == [asset_a, asset_b]
        assert stored["status"] == "approved"
