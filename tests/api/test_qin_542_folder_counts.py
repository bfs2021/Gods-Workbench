"""QIN-542（GitHub #598）：素材目录计数口径测试。

票面 AC3：目录计数改为包含子目录的总数（``total_count``，含子目录递归总数），
同时提供只算本层文件的数（``count``，仅本层直接文件），两种口径并区分。
"""

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import repository as repo

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer test-owner", "X-User-Role": "editor"}


@pytest.fixture
def folders_runtime(tmp_path, monkeypatch):
    """隔离环境：允许根与数据目录均落在 tmp_path，不触碰真实生产/开发数据。"""
    root = tmp_path / "allowed_library"
    root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("GW_DATA_DIR", str(data_dir))
    yield root


def _inject_assets(items):
    """直接注入最小可见素材记录，聚焦 folders 计数口径本身。"""

    def mutate(raw):
        for asset_id, relpath in items:
            raw["assets"][asset_id] = {
                "asset_id": asset_id,
                "name": asset_id,
                "kind": "image",
                "storage_root_id": "root_qin542",
                "relative_path": relpath,
            }
        return

    repo.state().mutate(mutate)


def test_folder_counts_distinguish_recursive_total_and_self_files(folders_runtime):
    """AC3：total_count 为含子目录递归总数；count 仅本层直接文件。"""
    _inject_assets([
        ("asset_output_a", "output/shot_a.png"),
        ("asset_output_b", "output/shot_b.png"),
        ("asset_sub_deep", "output/sub/deep.png"),
        ("asset_sub_deeper", "output/sub/deeper/wide.png"),
        ("asset_top_level", "top.png"),
    ])
    with TestClient(create_app()) as client:
        resp = client.get(f"{BASE}/folders", headers=AUTH)
        assert resp.status_code == 200, resp.text
        entries = {
            item["path"]: item
            for item in resp.json()["indexed_folders"]
            if item["root_id"] == "root_qin542"
        }
        assert entries["output"]["count"] == 2
        assert entries["output"]["total_count"] == 4
        assert entries["output/sub"]["count"] == 1
        assert entries["output/sub"]["total_count"] == 2
        assert entries["output/sub/deeper"]["count"] == 1
        assert entries["output/sub/deeper"]["total_count"] == 1
        assert "top.png" not in entries
        assert len(entries) == 3


def test_folder_counts_respect_root_id_scope(folders_runtime):
    """folders?root_id= 只返回该来源的目录条目（计数口径按来源隔离）。"""
    def mutate(raw):
        for asset_id, root_id, relpath in [
            ("asset_root_a", "root_a", "shared/one.png"),
            ("asset_root_b", "root_b", "shared/two.png"),
        ]:
            raw["assets"][asset_id] = {
                "asset_id": asset_id,
                "name": asset_id,
                "kind": "image",
                "storage_root_id": root_id,
                "relative_path": relpath,
            }
        return

    repo.state().mutate(mutate)

    with TestClient(create_app()) as client:
        resp = client.get(f"{BASE}/folders?root_id=root_a", headers=AUTH)
        assert resp.status_code == 200, resp.text
        entries = resp.json()["indexed_folders"]
        assert entries, "root_id 过滤不得为空"
        assert {item["root_id"] for item in entries} == {"root_a"}
        shared = next(item for item in entries if item["path"] == "shared")
        assert shared["count"] == 1
        assert shared["total_count"] == 1
