"""QIN-547（GitHub #603）：收集箱默认目录、迁移幂等与改位置。

覆盖票面 AC：
1. 收集箱是 data_root 下的真实文件夹，位置可改；
2. 托管资产记录并入收集箱（外部 URL 保留为 URL 条目）；
3. 迁移可重复执行，重复执行不多出数据；
4. 改位置支持迁移/不迁移；
5. 新素材角标按「已查看时间点」计算；
6. 路径校验：拒绝相对路径、互为上下级目录与占用文件。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_library.service import default_asset_library_service

BASE = "/api/asset-inbox"
AUTH = {"Authorization": "Bearer test-owner", "X-User-Role": "editor"}
EDIT_AUTH = {**AUTH}


@pytest.fixture
def inbox_runtime(tmp_path, monkeypatch):
    """三态隔离：数据根与允许根都在 tmp_path，不触碰真实数据。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed))
    return {"data": data_dir, "allowed": allowed, "tmp": tmp_path}


def _error_code(response) -> str:
    detail = response.json().get("detail")
    return str(detail.get("code") if isinstance(detail, dict) else detail)


def _register_item(name: str, url: str | None) -> str:
    item = default_asset_library_service.register_item(
        asset_id=f"asset_qin547_{name}",
        name=name,
        category_type="image",
        category_name="图片",
        url=url,
    )
    return item.asset_id


def _upload(client, filename: str, content: bytes) -> dict:
    response = client.post(
        f"{BASE}/upload",
        headers=EDIT_AUTH,
        files={"files": (filename, content, "image/png")},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["count"] == 1, payload
    return payload["items"][0]


def test_inbox_defaults_to_real_folder_under_data_root(inbox_runtime):
    """AC1：收集箱默认落在 data_root 下的真实文件夹，且后端会创建它。"""
    with TestClient(create_app()) as client:
        response = client.get(BASE, headers=AUTH)
        assert response.status_code == 200, response.text
        settings = response.json()["settings"]
        path = Path(settings["path"])
        assert path == (inbox_runtime["data"] / "inbox").resolve()
        assert path.is_dir(), "默认收集箱目录应真实存在"
        assert settings["is_default"] is True
        assert settings["unread_count"] == 0


def test_migrate_moves_local_files_keeps_external_urls_and_is_idempotent(
    inbox_runtime,
):
    """AC2/AC3：本地文件迁入收集箱、外部 URL 保留条目；重复执行不多出数据。"""
    local_file = inbox_runtime["allowed"] / "reference.png"
    local_file.write_bytes(b"local-payload")
    local_id = _register_item("本地参考图.png", local_file.as_uri())
    external_id = _register_item("外部参考图", "https://example.com/reference.png")

    with TestClient(create_app()) as client:
        first = client.post(f"{BASE}/migrate", headers=EDIT_AUTH)
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["moved_files"] == 1, payload
        assert payload["url_entries"] == 1, payload
        assert payload["failed"] == []
        assert not local_file.exists(), "本地文件应已迁入收集箱"
        inbox_files = list((inbox_runtime["data"] / "inbox").rglob("*.png"))
        assert [path.name for path in inbox_files] == ["reference.png"], "磁盘文件名保持原样"

        second = client.post(f"{BASE}/migrate", headers=EDIT_AUTH)
        assert second.status_code == 200, second.text
        again = second.json()
        assert again["moved_files"] == 0, "重复迁移不得再搬文件"
        assert again["url_entries"] == 0, "重复迁移不得新增 URL 条目"
        assert again["skipped"] == 2, "两条记录均应记为已迁移"
        assert again["total_records"] == payload["total_records"] == 2

        listing = client.get(BASE, headers=AUTH).json()
        by_asset = {
            item["asset_id"]: item
            for item in listing["items"]
            if item["source"] == "asset_library"
        }
        assert external_id in by_asset
        assert by_asset[external_id]["url"] == "https://example.com/reference.png"
        assert local_id not in by_asset, "已迁入本地的记录由磁盘文件呈现，不重复列 URL 条目"
        assert listing["settings"]["migrated_asset_count"] == 2


def test_migrate_marks_records_without_url_as_record_kind(inbox_runtime):
    """AC3：没有访问地址的托管记录标为「仅记录」，不伪装成可点击链接。"""
    no_url_id = _register_item("无地址记录", None)
    url_id = _register_item("有地址记录", "https://example.com/a.png")

    with TestClient(create_app()) as client:
        payload = client.post(f"{BASE}/migrate", headers=EDIT_AUTH).json()
        assert payload["moved_files"] == 0, payload
        assert payload["url_entries"] == 2, payload

        listing = client.get(BASE, headers=AUTH).json()
        by_asset = {item["asset_id"]: item for item in listing["items"]}
        assert by_asset[no_url_id]["kind"] == "record", "url 为空的记录应为 record"
        assert by_asset[no_url_id]["url"] is None
        assert by_asset[url_id]["kind"] == "url", "真实地址仍为链接条目"


def test_migrate_never_moves_files_outside_allowed_roots(inbox_runtime):
    """迁移边界：只搬已挂载素材根内的文件；根外本地文件保留为 URL 条目，不碰盘。"""
    outside = inbox_runtime["tmp"] / "outside.png"
    outside.write_bytes(b"outside")
    asset_id = _register_item("根外文件", outside.as_uri())
    with TestClient(create_app()) as client:
        result = client.post(f"{BASE}/migrate", headers=EDIT_AUTH).json()
        assert result["moved_files"] == 0, result
        assert outside.is_file(), "不允许把已挂载根目录之外的文件搬进收集箱"
        listing = client.get(BASE, headers=AUTH).json()
        entry = next(item for item in listing["items"] if item["asset_id"] == asset_id)
        assert entry["url"] == outside.as_uri()


def test_relocate_with_migrate_moves_files_and_repeat_is_noop(inbox_runtime):
    """AC1/AC6：改位置并迁移；同一位置重复提交幂等。"""
    target = inbox_runtime["tmp"] / "新收集箱"
    with TestClient(create_app()) as client:
        _upload(client, "take.png", b"payload")
        response = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(target), "migrate": True}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["migrated"] == ["take.png"], result
        assert (target / "take.png").is_file()
        assert not (inbox_runtime["data"] / "inbox" / "take.png").exists()

        repeated = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(target), "migrate": True}
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["unchanged"] is True
        assert repeated.json()["migrated"] == []
        assert list(target.rglob("take.png")) == [target / "take.png"], "重复改位置不得复制文件"


def test_relocate_without_migrate_keeps_old_files(inbox_runtime):
    """AC1：选择不迁移时，旧位置文件保留在原处，新位置为空。"""
    target = inbox_runtime["tmp"] / "空收集箱"
    with TestClient(create_app()) as client:
        _upload(client, "keep.png", b"payload")
        response = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(target), "migrate": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["migrated"] == []
        assert (inbox_runtime["data"] / "inbox" / "keep.png").is_file()
        assert list(target.iterdir()) == []
        assert client.get(BASE, headers=AUTH).json()["count"] == 0


def test_unread_badge_tracks_new_materials(inbox_runtime):
    """AC5：新素材计入角标，查看后清零，再来新素材重新计数。"""
    with TestClient(create_app()) as client:
        _upload(client, "one.png", b"one")
        assert client.get(BASE, headers=AUTH).json()["unread_count"] == 1

        viewed = client.post(f"{BASE}/viewed", headers=EDIT_AUTH)
        assert viewed.status_code == 200, viewed.text
        assert client.get(BASE, headers=AUTH).json()["unread_count"] == 0

        _upload(client, "two.png", b"two")
        listing = client.get(BASE, headers=AUTH).json()
        assert listing["unread_count"] == 1
        assert listing["count"] == 2


def test_upload_keeps_same_name_without_overwriting(inbox_runtime):
    """手动上传落在收集箱；同名文件改名保存，不覆盖已有素材。"""
    with TestClient(create_app()) as client:
        first = _upload(client, "shot.png", b"first")
        second = _upload(client, "shot.png", b"second")
        assert first["file"] == "shot.png"
        assert second["file"] == "shot (2).png"
        inbox = inbox_runtime["data"] / "inbox"
        assert (inbox / "shot.png").read_bytes() == b"first"
        assert (inbox / "shot (2).png").read_bytes() == b"second"


def test_legacy_collect_files_are_listed_in_inbox(inbox_runtime):
    """AC2：遗留 collect_inbox 收集文件在收集箱里显示（不搬迁，避免破坏受管回读）。"""
    legacy = inbox_runtime["data"] / "registry_uploads" / "collect_inbox" / "owner"
    legacy.mkdir(parents=True)
    (legacy / "collected.png").write_bytes(b"legacy")
    with TestClient(create_app()) as client:
        listing = client.get(BASE, headers=AUTH).json()
        sources = {item["source"] for item in listing["items"]}
        assert "collect_inbox" in sources
        entry = next(item for item in listing["items"] if item["source"] == "collect_inbox")
        assert entry["file"] == "registry_uploads/collect_inbox/owner/collected.png"
        assert (legacy / "collected.png").is_file(), "遗留文件保持原处"


def test_settings_path_validation(inbox_runtime):
    """AC6：拒绝相对路径、互为上下级目录与已被文件占用的位置。"""
    occupied = inbox_runtime["tmp"] / "occupied.txt"
    occupied.write_text("x", encoding="utf-8")
    inbox = inbox_runtime["data"] / "inbox"
    with TestClient(create_app()) as client:
        relative = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": "relative/inbox"}
        )
        assert relative.status_code == 400
        assert _error_code(relative) == "INVALID_PATH"

        nested = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(inbox / "child")}
        )
        assert nested.status_code == 400
        assert _error_code(nested) == "INBOX_PATH_NESTED"

        parent = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(inbox_runtime["data"])}
        )
        assert parent.status_code == 400
        assert _error_code(parent) == "INBOX_PATH_NESTED"

        file_path = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(occupied)}
        )
        assert file_path.status_code == 400
        assert _error_code(file_path) == "INVALID_PATH"

        bad_flag = client.put(
            f"{BASE}/settings", headers=EDIT_AUTH, json={"path": str(occupied), "migrate": "yes"}
        )
        assert bad_flag.status_code == 400
        assert _error_code(bad_flag) == "INVALID_REQUEST"


def test_inbox_requires_authentication(inbox_runtime):
    """未认证请求一律 401，不暴露收集箱位置。"""
    with TestClient(create_app()) as client:
        assert client.get(BASE).status_code == 401
        assert client.post(f"{BASE}/migrate").status_code == 401
