"""QIN-244 / PR #149 返工：相对路径 root_id 透传与位置身份回归契约。

覆盖独立审核指出的 11 个生产调用点不再把单根部署的相对路径请求变成
409 ROOT_ID_REQUIRED，同时保持 storage.resolve_within_roots 对无 root_id
相对路径的严格失败关闭（不做 roots[0] 歧义回退）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.asset_library import repository as local_repo
from gw.asset_registry import lifecycle
from gw.asset_registry import repository as registry_repo
from gw.core import storage
from gw.core.errors import CleanroomException

EDITOR = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
BASE = "/api/asset-registry"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _local_identity(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()


@pytest.fixture()
def one_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "唯一根"
    root.mkdir()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    return root.resolve()


@pytest.fixture()
def two_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = tmp_path / "根甲"
    second = tmp_path / "根乙"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", f"{first}{os.pathsep}{second}")
    return first.resolve(), second.resolve()


@pytest.fixture()
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 单根：相对路径不得退化为 409 ROOT_ID_REQUIRED
# ---------------------------------------------------------------------------


def test_storage_strictness_is_preserved_and_unique_root_helper_is_explicit(one_root):
    with pytest.raises(CleanroomException) as strict:
        storage.resolve_within_roots("a.png")
    assert strict.value.code == "ROOT_ID_REQUIRED"
    assert storage.resolve_request_path("a.png") == one_root / "a.png"
    with pytest.raises(CleanroomException) as traversal:
        storage.resolve_request_path("../escape.png")
    assert traversal.value.status_code == 403


def test_unique_root_helper_never_guesses_between_two_roots(two_roots):
    with pytest.raises(CleanroomException) as missing:
        storage.resolve_request_path("a.png")
    assert missing.value.code == "ROOT_ID_REQUIRED"
    second = two_roots[1]
    assert storage.resolve_request_path(
        "a.png", root_id=storage.root_identity_id(second)
    ) == second / "a.png"


def test_single_root_file_info_and_reveal_accept_relative_path(one_root, client):
    Image.new("RGB", (21, 13), (1, 2, 3)).save(one_root / "shot.png")
    info = client.get("/api/asset-file-info", headers=EDITOR, params={"path": "shot.png"})
    assert info.status_code == 200, info.text
    assert info.json()["width"] == 21 and info.json()["display_path"] == "shot.png"
    reveal = client.post("/api/asset-file-reveal", headers=EDITOR, json={"path": "shot.png"})
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["exists"] is True


def test_single_root_workflow_upload_and_avatar_accept_relative_path(one_root, client):
    uploaded = client.post(
        "/api/asset-library/workflows/upload",
        headers=EDITOR,
        json={"path": "flows/a.json", "content_base64": _b64(b"{}")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert (one_root / "flows" / "a.json").read_bytes() == b"{}"
    Image.new("RGB", (8, 8)).save(one_root / "face.png")
    avatar = client.post(
        "/api/asset-library/items/item-1/register-avatar",
        headers=EDITOR,
        json={"avatar_path": "face.png"},
    )
    assert avatar.status_code == 200, avatar.text
    assert avatar.json()["avatar_path"] == "face.png"


def test_single_root_local_asset_upload_move_delete_with_relative_paths(one_root, client):
    uploaded = client.post(
        "/api/local-assets/upload",
        headers=EDITOR,
        json={"path": "clip.bin", "content_base64": _b64(b"relative-upload")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert (one_root / "clip.bin").read_bytes() == b"relative-upload"
    asset_id = client.get("/api/local-assets", headers=EDITOR).json()["items"][0]["asset_id"]

    moved = client.post(
        "/api/local-assets/move",
        headers=EDITOR,
        json={"source": "clip.bin", "target": "sub/clip2.bin"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["asset_id"] == asset_id
    assert (one_root / "sub" / "clip2.bin").exists() and not (one_root / "clip.bin").exists()
    items = client.get("/api/local-assets", headers=EDITOR).json()["items"]
    assert [item["display_path"] for item in items] == ["sub/clip2.bin"]

    deleted = client.post(
        "/api/local-assets/delete", headers=EDITOR, json={"paths": ["sub/clip2.bin"]}
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == ["sub/clip2.bin"]
    assert deleted.json()["partial_failures"] == []
    assert client.get("/api/local-assets", headers=EDITOR).json()["count"] == 0


def test_single_root_index_local_files_accepts_relative_path(one_root):
    (one_root / "indexed.png").write_bytes(b"png")
    result = local_repo.index_local_files(["indexed.png"])
    assert result["count"] == 1
    assert result["items"][0]["display_path"] == "indexed.png"


def test_single_root_media_proxy_path_roundtrip_carries_root_id(one_root, client):
    Image.new("RGB", (400, 300), (9, 9, 9)).save(one_root / "proxy.png")
    descriptor = client.get(
        "/api/media-proxy", headers=EDITOR, params={"path": "proxy.png", "profile": "small"}
    )
    assert descriptor.status_code == 200, descriptor.text
    body = descriptor.json()
    assert body["availability"] == "ready"
    assert "root_id=" + storage.root_identity_id(one_root) in body["proxy_url"]
    content = client.get(body["proxy_url"], headers=EDITOR)
    assert content.status_code == 200, content.text
    assert str(one_root) not in descriptor.text


def test_single_root_media_proxy_and_preview_accept_relative_path_without_root_id(
    one_root, client
):
    Image.new("RGB", (300, 200), (4, 4, 4)).save(one_root / "p2.png")
    content = client.get(
        "/api/media-proxy/content", headers=EDITOR, params={"path": "p2.png"}
    )
    assert content.status_code == 200, content.text
    thumbnail_settings = client.get("/api/asset-thumbnails/settings", headers=EDITOR)
    assert thumbnail_settings.status_code == 200, thumbnail_settings.text
    updated = client.patch(
        "/api/asset-thumbnails/settings",
        headers=EDITOR,
        json={
            "format": "webp",
            "expected_version": thumbnail_settings.json()["revision"],
        },
    )
    assert updated.status_code == 200, updated.text
    preview = client.get("/api/media-preview", headers=EDITOR, params={"path": "p2.png"})
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("image/webp")
    assert preview.content.startswith(b"RIFF") and preview.content[8:12] == b"WEBP"


# ---------------------------------------------------------------------------
# 多根：root_id 从请求或唯一索引记录透传，歧义仍失败关闭
# ---------------------------------------------------------------------------


def test_multi_root_file_info_requires_or_uses_root_id(two_roots, client):
    first, second = two_roots
    (first / "same.png").write_bytes(b"first")
    Image.new("RGB", (5, 7)).save(second / "same.png")
    missing = client.get("/api/asset-file-info", headers=EDITOR, params={"path": "same.png"})
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "ROOT_ID_REQUIRED"
    picked = client.get(
        "/api/asset-file-info",
        headers=EDITOR,
        params={"path": "same.png", "root_id": storage.root_identity_id(second)},
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["width"] == 5
    by_local_identity = client.get(
        "/api/asset-file-info",
        headers=EDITOR,
        params={"path": "same.png", "root_id": _local_identity(second)},
    )
    assert by_local_identity.status_code == 200, by_local_identity.text
    assert by_local_identity.json()["width"] == 5
    unknown = client.get(
        "/api/asset-file-info",
        headers=EDITOR,
        params={"path": "same.png", "root_id": "root_" + "0" * 24},
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "ROOT_NOT_FOUND"


def test_multi_root_relative_delete_uses_unique_index_record_root(two_roots, client):
    first, second = two_roots
    uploaded = client.post(
        "/api/local-assets/upload",
        headers=EDITOR,
        json={"path": str(second / "only.bin"), "content_base64": _b64(b"b")},
    )
    assert uploaded.status_code == 201, uploaded.text
    deleted = client.post("/api/local-assets/delete", headers=EDITOR, json={"paths": ["only.bin"]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == ["only.bin"]
    assert not (second / "only.bin").exists()
    assert client.get("/api/local-assets", headers=EDITOR).json()["count"] == 0


def test_multi_root_duplicate_relative_paths_stay_fail_closed_until_root_id(two_roots, client):
    first, second = two_roots
    for root in (first, second):
        uploaded = client.post(
            "/api/local-assets/upload",
            headers=EDITOR,
            json={"path": str(root / "dup.bin"), "content_base64": _b64(root.name.encode())},
        )
        assert uploaded.status_code == 201, uploaded.text
    ambiguous = client.post("/api/local-assets/delete", headers=EDITOR, json={"paths": ["dup.bin"]})
    assert ambiguous.status_code == 409
    assert ambiguous.json()["detail"]["code"] == "ROOT_ID_REQUIRED"
    assert (first / "dup.bin").exists() and (second / "dup.bin").exists()
    ok = client.post(
        "/api/local-assets/delete",
        headers=EDITOR,
        json={"paths": ["dup.bin"], "root_id": storage.root_identity_id(second)},
    )
    assert ok.status_code == 200, ok.text
    assert not (second / "dup.bin").exists() and (first / "dup.bin").exists()
    remaining = client.get("/api/local-assets", headers=EDITOR).json()["items"]
    assert len(remaining) == 1 and remaining[0]["root_id"] == _local_identity(first)


def test_multi_root_move_defaults_target_to_source_root(two_roots, client):
    first, second = two_roots
    for root in (first, second):
        (root / "m.bin").write_bytes(root.name.encode())
    second_id = storage.root_identity_id(second)
    moved = client.post(
        "/api/local-assets/move",
        headers=EDITOR,
        json={"source": "m.bin", "target": "moved.bin", "source_root_id": second_id},
    )
    assert moved.status_code == 200, moved.text
    assert (second / "moved.bin").exists() and not (second / "m.bin").exists()
    assert (first / "m.bin").exists() and not (first / "moved.bin").exists()
    unresolved = client.post(
        "/api/local-assets/move",
        headers=EDITOR,
        json={"source": "m.bin", "target": "again.bin"},
    )
    assert unresolved.status_code == 409
    assert unresolved.json()["detail"]["code"] == "ROOT_ID_REQUIRED"


def test_multi_root_media_proxy_descriptor_embeds_requested_root_id(two_roots, client):
    first, second = two_roots
    Image.new("RGB", (320, 240), (1, 1, 1)).save(first / "same.png")
    Image.new("RGB", (640, 480), (200, 1, 1)).save(second / "same.png")
    second_id = storage.root_identity_id(second)
    descriptor = client.get(
        "/api/media-proxy",
        headers=EDITOR,
        params={"path": "same.png", "root_id": second_id, "profile": "preview", "width": 640},
    )
    assert descriptor.status_code == 200, descriptor.text
    assert "root_id=" + second_id in descriptor.json()["proxy_url"]
    assert client.get(descriptor.json()["proxy_url"], headers=EDITOR).status_code == 200
    refused = client.get("/api/media-proxy", headers=EDITOR, params={"path": "same.png"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "ROOT_ID_REQUIRED"


# ---------------------------------------------------------------------------
# 重要项：位置身份使用 location_identity / canonical_relpath
# ---------------------------------------------------------------------------


def test_move_uses_canonical_identity_so_nfd_to_nfc_rename_is_the_same_record(one_root):
    nfd = unicodedata.normalize("NFD", "café") + ".txt"
    nfc = unicodedata.normalize("NFC", "café") + ".txt"
    assert nfd != nfc
    (one_root / nfd).write_text("x", encoding="utf-8")
    local_repo.index_local_files([str(one_root / nfd)])
    result = local_repo.move_local_file(str(one_root / nfd), str(one_root / nfc))
    assert result["asset_id"]
    items = local_repo.list_local_assets(None, limit=10, cursor=None)["items"]
    assert len(items) == 1
    assert items[0]["canonical_relpath"] == nfc
    assert items[0]["root_id"] == _local_identity(one_root)
    assert items[0]["asset_id"] == result["asset_id"]
    names = [path.name for path in one_root.iterdir()]
    assert names == [nfc]


def test_lifecycle_identity_canonical_relpath_matches_location_identity(one_root):
    nfd_name = unicodedata.normalize("NFD", "é") + ".mp4"
    target = one_root / nfd_name
    target.write_bytes(b"v")
    identity = lifecycle._identity(target)
    assert identity["canonical_relpath"] == storage.location_identity(target)[1]
    assert identity["canonical_relpath"] == unicodedata.normalize("NFC", nfd_name)
    assert identity["root_id"] == _local_identity(one_root)
    # 展示用相对路径保持磁盘原名，恢复时不因规范化丢失原始文件名。
    assert identity["display_path"] == nfd_name


def test_recycle_records_root_resolution_failures_instead_of_silently_skipping(
    two_roots, monkeypatch
):
    first, second = two_roots
    source = first / "原文件.txt"
    source.write_text("single-file", encoding="utf-8")
    keep_a = second / "left-behind-a.png"
    keep_b = second / "left-behind-b.png"
    keep_a.write_bytes(b"a")
    keep_b.write_bytes(b"b")
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    with TestClient(create_app()) as client:
        imported = client.post(
            f"{BASE}/assets/import",
            headers=EDITOR,
            json={"items": [{"name": source.name, "kind": "document", "display_path": str(source)}]},
        )
        assert imported.status_code == 200, imported.text
        asset_id = imported.json()["asset_ids"][0]

        def tamper(raw):
            asset = raw["assets"][asset_id]
            asset["display_path"] = str(source)
            asset.pop("root_id", None)
            asset["metadata"] = {
                "derived_assets": {
                    "needs-root.png": {"path": "left-behind-a.png", "root_id": ""},
                    "bad-root.png": {"path": "left-behind-b.png", "root_id": "root_" + "f" * 24},
                }
            }
            registry_repo._bump(raw)

        registry_repo.state().mutate(tamper)
        revision = client.get(f"{BASE}/status", headers=EDITOR).json()["revision"]
        deleted = client.request(
            "DELETE", f"{BASE}/assets/{asset_id}", headers=EDITOR, json={"expected_version": revision}
        )
        assert deleted.status_code == 200, deleted.text
        operation = deleted.json()["operation"]
    failures = operation["partial_failures"]
    assert {(item["name"], item["code"]) for item in failures} == {
        ("needs-root.png", "ROOT_ID_REQUIRED"),
        ("bad-root.png", "ROOT_NOT_FOUND"),
    }
    assert operation["state"] == "done"
    assert keep_a.exists() and keep_b.exists()
    assert not source.exists()
    # 日志读回同样保留部分失败，而不是只存在于一次响应里。
    persisted = lifecycle.get_operation(operation["op_id"])
    assert persisted["partial_failures"] == failures
