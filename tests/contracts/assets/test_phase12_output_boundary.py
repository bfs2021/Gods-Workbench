"""通用产物接口不得导出或删除内部持久化状态，全部使用隔离数据根。"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.core import storage

AUTH = {"Authorization": "Bearer output-boundary-fixture", "X-User-Role": "editor"}
READ = {**AUTH, "X-User-Role": "readonly"}
NAMESPACES = ("ai_uploads", "media_output", "registry_uploads", "local_assets")


def write_fixture(relative, payload=b"fixture-private-state"):
    path = Path(os.environ["GW_DATA_DIR"]) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.mark.parametrize(
    "name",
    [
        "internal-state.json",
        "team_messages.sqlite3",
        "video/private.mp4",
        "private/config.json",
    ],
)
def test_internal_state_is_not_a_downloadable_or_deletable_output(name):
    source = write_fixture(name)
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/download-output", headers=READ, params={"path": name}
        )
        assert response.status_code == 403, response.text
        assert "fixture-private-state" not in response.text
        assert str(source) not in response.text
        listed = client.get("/api/storage-files", headers=READ)
        assert listed.status_code == 200
        assert name not in [item["name"] for item in listed.json()["items"]]
        deleted = client.post(
            "/api/storage-files/delete", headers=AUTH, json={"names": [name]}
        )
        assert deleted.status_code == 403, deleted.text
        assert source.read_bytes() == b"fixture-private-state"


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_known_output_namespaces_remain_downloadable_and_deletable(namespace):
    name = namespace + "/fixture.txt"
    source = write_fixture(name, b"public-fixture-output")
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/download-output", headers=READ, params={"path": name}
        )
        assert (
            response.status_code == 200 and response.content == b"public-fixture-output"
        )
        assert name in [
            item["name"]
            for item in client.get("/api/storage-files", headers=READ).json()["items"]
        ]
        rejected = client.post(
            "/api/storage-files/delete", headers=READ, json={"names": [name]}
        )
        assert rejected.status_code == 403 and source.exists()
        deleted = client.post(
            "/api/storage-files/delete", headers=AUTH, json={"names": [name]}
        )
        assert deleted.status_code == 200 and deleted.json()["count"] == 1
        assert not source.exists()
        assert (
            client.get(
                "/api/download-output", headers=READ, params={"path": name}
            ).status_code
            == 404
        )


def test_mixed_batch_is_fully_prevalidated_before_any_file_removal():
    output = write_fixture("media_output/keep.txt")
    internal = write_fixture("internal-state.json")
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/storage-files/delete",
            headers=AUTH,
            json={"names": ["media_output/keep.txt", "internal-state.json"]},
        )
        assert result.status_code == 403
        assert output.exists() and internal.exists()


def test_output_symlink_cannot_reach_internal_state():
    internal = write_fixture("internal-state.json")
    link = write_fixture("media_output/link.txt")
    link.unlink()
    try:
        link.symlink_to(internal)
    except OSError as exc:
        pytest.skip("当前Windows账户不能创建符号链接：" + str(exc.winerror))
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/download-output",
            headers=READ,
            params={"path": "media_output/link.txt"},
        )
        assert response.status_code == 403
        assert "media_output/link.txt" not in [
            item["name"]
            for item in client.get("/api/storage-files", headers=READ).json()["items"]
        ]
        response = client.post(
            "/api/storage-files/delete",
            headers=AUTH,
            json={"names": ["media_output/link.txt"]},
        )
        assert response.status_code == 403
        assert internal.exists()


@pytest.mark.parametrize(
    "name",
    [
        "media_output-copy/file.txt",
        "media_output/../internal-state.json",
        "media_output/nested/../../internal-state.json",
        "media_output/file.txt:secret",
        "C:/data/media_output/a.txt",
        "//server/share/media_output/a.txt",
        "media_output/CON",
        "media_output/file.txt.",
    ],
)
def test_path_aliases_are_rejected_even_when_the_file_does_not_exist(name):
    with TestClient(create_app()) as client:
        assert (
            client.get(
                "/api/download-output", headers=READ, params={"path": name}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/storage-files/delete", headers=AUTH, json={"names": [name]}
            ).status_code
            == 403
        )


def create_directory_link(link, target):
    """Windows使用真实Junction，不因账户缺少文件符号链接权限跳过边界验收。"""
    import subprocess

    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        environment = {
            **os.environ,
            "GW_TEST_JUNCTION": str(link),
            "GW_TEST_TARGET": str(target),
        }
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:GW_TEST_JUNCTION -Target $env:GW_TEST_TARGET | Out-Null",
            ],
            env=environment,
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, (
            "隔离Junction夹具创建失败，不能宣称重解析点已验收"
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("placement", ["namespace", "nested", "same_namespace"])
def test_actual_junctions_never_expose_or_delete_the_target(placement, tmp_path):
    root = Path(os.environ["GW_DATA_DIR"])
    if placement == "namespace":
        target = tmp_path / "private-target"
        link = root / "media_output"
        relative = "media_output/asset_1_preview.bin"
    elif placement == "nested":
        target = root / "private"
        link = root / "media_output" / "nested"
        relative = "media_output/nested/asset_1_preview.bin"
    else:
        target = root / "media_output" / "owner-b"
        link = root / "media_output" / "owner-a"
        relative = "media_output/owner-a/asset_1_preview.bin"
    target.mkdir(parents=True, exist_ok=True)
    protected = target / "asset_1_preview.bin"
    protected.write_bytes(b"fixture-junction-target")
    create_directory_link(link, target)
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/download-output", headers=READ, params={"path": relative}
        )
        assert response.status_code == 403
        listed = client.get("/api/storage-files", headers=READ).json()["items"]
        assert relative not in [item["name"] for item in listed]
        deleted = client.post(
            "/api/storage-files/delete", headers=AUTH, json={"names": [relative]}
        )
        assert (
            deleted.status_code == 403
            and protected.read_bytes() == b"fixture-junction-target"
        )
        if placement != "same_namespace":
            # 合法owner-b自身仍是产物；这里只验证内部/外部状态不会通过asset_id扫描旁路返回。
            assert (
                client.get(
                    "/api/download-output", headers=READ, params={"asset_id": "asset_1"}
                ).status_code
                == 404
            )


def test_delete_io_failure_reports_only_real_removals(monkeypatch):
    removed = write_fixture("local_assets/removed.txt")
    failed = write_fixture("local_assets/failed.txt")
    original = Path.unlink

    def deny_one(path, *args, **kwargs):
        if path == failed:
            raise PermissionError("fixture-private-absolute-path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_one)
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/storage-files/delete",
            headers=AUTH,
            json={"names": ["local_assets/removed.txt", "local_assets/failed.txt"]},
        )
        assert result.status_code == 200 and result.json()["data_status"] == "partial"
        assert result.json()["deleted"] == ["local_assets/removed.txt"]
        assert result.json()["count"] == 1 and failed.exists() and not removed.exists()
        assert result.json()["failed"] == [
            {"name": "local_assets/failed.txt", "code": "OUTPUT_DELETE_FAILED"}
        ]
        assert "fixture-private-absolute-path" not in result.text


def test_output_endpoints_still_require_authentication():
    with TestClient(create_app()) as client:
        assert (
            client.get(
                "/api/download-output", params={"path": "media_output/file.txt"}
            ).status_code
            == 401
        )
        assert client.get("/api/storage-files").status_code == 401
        assert (
            client.post(
                "/api/storage-files/delete", json={"names": ["internal.json"]}
            ).status_code
            == 401
        )


def test_thumbnail_generation_and_deletion_use_exact_asset_id(tmp_path, monkeypatch):
    from PIL import Image

    root = tmp_path / "sources"
    root.mkdir()
    source = root / "image.png"
    Image.new("RGB", (16, 16), (2, 3, 4)).save(source)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    with TestClient(create_app()) as client:
        asset_id = client.post(
            "/api/asset-registry/assets/import",
            headers=AUTH,
            json={"items": [{"name": "缩略图源", "kind": "image"}]},
        ).json()["asset_ids"][0]
        assert (
            client.patch(
                "/api/asset-registry/assets/" + asset_id,
                headers=AUTH,
                json={
                    "display_path": source.name,
                    "root_id": storage.root_identity_id(root.resolve()),
                },
            ).status_code
            == 200
        )
        settings = client.get("/api/asset-thumbnails/settings", headers=AUTH)
        assert settings.status_code == 200, settings.text
        updated = client.patch(
            "/api/asset-thumbnails/settings",
            headers=AUTH,
            json={"format": "webp", "expected_version": settings.json()["revision"]},
        )
        assert updated.status_code == 200, updated.text
        created = client.post(
            "/api/asset-thumbnails/generate",
            headers=AUTH,
            json={"asset_id": asset_id, "width": 64},
        )
        assert created.status_code == 200, created.text
        output = created.json()["output"]
        assert output.endswith("/" + asset_id + ".webp")
        # 同前缀文件夹具只能证明精确删除，不冒充它具有注册表业务来源。
        protected = write_fixture("media_output/thumbnails/" + asset_id + "0.jpg.png")
        result = client.post(
            "/api/asset-thumbnails/delete", headers=AUTH, json={"asset_ids": [asset_id]}
        )
        assert result.status_code == 200 and result.json()["removed"] == 1
        assert (
            client.get(
                "/api/download-output", headers=READ, params={"path": output}
            ).status_code
            == 404
        )
        assert protected.exists() and source.exists()


def test_storyboard_delete_preserves_other_ids_and_nonframe_files():
    selected = write_fixture("media_output/storyboards/asset_1/frame_1_000.png")
    kept = [
        write_fixture(name)
        for name in (
            "media_output/storyboards/asset_10/frame_1_000.png",
            "media_output/storyboards/asset_1/frame_1_00.png",
            "media_output/storyboards/asset_1/clip.mp4",
            "media_output/asset_1/frame_1_000.png",
        )
    ]
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/asset-thumbnails/delete-storyboards",
            headers=AUTH,
            json={"asset_ids": ["asset_1"]},
        )
        assert result.status_code == 200 and result.json()["removed"] == 1
        assert not selected.exists() and all(path.exists() for path in kept)
        assert result.json()["data_gaps"] == ["legacy_shared_frames_preserved"]


def test_storyboard_junction_rejection_precedes_any_deletion():
    root = Path(os.environ["GW_DATA_DIR"])
    first = write_fixture("media_output/storyboards/asset_valid/frame_1_000.png")
    protected = write_fixture("media_output/storyboards/asset_10/frame_1_000.png")
    create_directory_link(
        root / "media_output" / "storyboards" / "asset_1", protected.parent
    )
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/asset-thumbnails/delete-storyboards",
            headers=AUTH,
            json={"asset_ids": ["asset_valid", "asset_1"]},
        )
        assert result.status_code == 403
        assert first.exists() and protected.exists()
        for asset_id in ("..", "../asset_10", "asset_1/../asset_10"):
            rejected = client.post(
                "/api/asset-thumbnails/delete-storyboards",
                headers=AUTH,
                json={"asset_ids": [asset_id]},
            )
            assert rejected.status_code == 400
            assert protected.exists()


def test_cache_delete_reports_partial_filesystem_failure(monkeypatch):
    removed = write_fixture("media_output/storyboards/asset_1/frame_1_000.png")
    failed = write_fixture("media_output/storyboards/asset_1/frame_2_000.png")
    original = Path.unlink

    def deny_one(path, *args, **kwargs):
        if path == failed:
            raise PermissionError("fixture-private-error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_one)
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/asset-thumbnails/delete-storyboards",
            headers=AUTH,
            json={"asset_ids": ["asset_1"]},
        )
        assert result.status_code == 200 and result.json()["removed"] == 1
        assert (
            result.json()["data_status"] == "partial"
            and len(result.json()["failed"]) == 1
        )
        assert not removed.exists() and failed.exists()
        assert "fixture-private-error" not in result.text
