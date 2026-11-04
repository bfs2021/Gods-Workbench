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
