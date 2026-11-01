"""审核覆盖补齐：受控HTTP、真实文件和写后读回，不调用付费服务。"""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import threading
import zipfile
import pytest

from fastapi.testclient import TestClient
from gw.api.app import create_app
from gw.core import storage
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
GOV = {**AUTH, "X-User-Role": "governor"}


def _registry_revision(client, headers=AUTH):
    response = client.get("/api/asset-registry/status", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["revision"]


@contextmanager
def local_upstream():
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            hits.append(("GET", self.path))
            body = json.dumps({"data": [{"id": "fixture-chat"}, {"id": "fixture-video"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_HEAD(self):
            hits.append(("HEAD", self.path))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", "1234")
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_provider_models_latency_and_durable_probe_use_real_loopback():
    with local_upstream() as (url, hits), TestClient(create_app()) as client:
        payload = {"base_url": url, "protocol": "openai"}
        models = client.post("/api/providers/fetch-models", headers=AUTH, json=payload)
        assert models.status_code == 200, models.text
        assert models.json()["all"] == ["fixture-chat", "fixture-video"]
        connection = client.post("/api/providers/test-connection", headers=AUTH, json=payload)
        assert connection.status_code == 200, connection.text
        assert connection.json()["latency_ms"] >= 0 and connection.json()["model_count"] == 2
        submitted = client.post("/api/providers/probe-async", headers=AUTH, json=payload)
        assert submitted.status_code == 200, submitted.text
        job = client.get(submitted.json()["poll_hint"], headers=AUTH)
        assert job.status_code == 200 and job.json()["status"] == "succeeded"
        assert job.json()["task"]["raw"]["data"][1]["id"] == "fixture-video"
        with TestClient(create_app()) as restarted:
            assert restarted.get(submitted.json()["poll_hint"], headers=AUTH).json() == job.json()
        assert hits == [("GET", "/v1/models")] * 3


def test_remote_metadata_head_persists_and_delete_disappears():
    path = "/api/asset-registry/remote-assets"
    with local_upstream() as (url, hits), TestClient(create_app()) as client:
        response = client.post(path, headers=AUTH, json={"url": url + "/fixture.mp4", "name": "远程样例"})
        assert response.status_code == 200, response.text
        asset = response.json()["remote_asset"]
        assert asset["content_type"] == "video/mp4" and asset["size_bytes"] == 1234
        assert asset["data_status"] == "ok" and asset["data_gaps"] == []
        with TestClient(create_app()) as restarted:
            persisted = restarted.get(path, headers=AUTH).json()["remote_assets"]
            assert len(persisted) == 1
            assert persisted[0] == {key: value for key, value in asset.items() if key not in {"id", "kind"}}
        assert client.delete(path + "/" + asset["asset_id"], headers=AUTH).status_code == 200
        assert client.get(path, headers=AUTH).json()["remote_assets"] == []
        assert hits == [("HEAD", "/fixture.mp4")]


def test_registered_media_download_zip_hash_and_recycle_readback(tmp_path, monkeypatch):
    source = tmp_path / "允许目录"
    source.mkdir()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(source))
    payloads = {"甲.txt": "完整中文正文".encode(), "beta.bin": bytes(range(256))}
    path = "/api/asset-registry/assets"
    with TestClient(create_app()) as client:
        ids = []
        for name, data in payloads.items():
            (source / name).write_bytes(data)
            created = client.post(path + "/import", headers=AUTH, json={"items": [{"name": name}]}).json()["asset_ids"][0]
            ids.append(created)
            assert client.patch(
                path + "/" + created,
                headers=AUTH,
                json={
                    "display_path": name,
                    "root_id": storage.root_identity_id(source.resolve()),
                },
            ).status_code == 200
            media = client.get(path + "/" + created + "/media", headers=AUTH)
            assert media.status_code == 200 and media.content == data
        archive = client.post(path + "/archive", headers=AUTH, json={"asset_ids": ids})
        assert archive.status_code == 200, archive.text
        with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
            assert set(package.namelist()) == set(payloads)
            for name, data in payloads.items():
                assert hashlib.sha256(package.read(name)).digest() == hashlib.sha256(data).digest()
        deleted = client.request("DELETE", path + "/" + ids[0], headers=AUTH,
                                 json={"expected_version": _registry_revision(client)})
        assert deleted.status_code == 200, deleted.text
        assert client.get(path + "/" + ids[0], headers=AUTH).status_code == 404
        entry = deleted.json()["recycle_entry_id"]
        restored = client.post("/api/asset-registry/recycle-bin/" + entry + "/restore", headers=GOV,
                               json={"expected_version": _registry_revision(client, GOV)})
        assert restored.status_code == 200, restored.text
        assert client.get(path + "/" + ids[0] + "/media", headers=AUTH).content == payloads["甲.txt"]


def test_content_version_get_patch_delete_readback():
    path = "/api/asset-content"
    params = {"asset_id": "success-path-content"}
    with TestClient(create_app()) as client:
        first = client.patch(path, params=params, headers=AUTH, json={"content": "第一版"})
        assert first.status_code == 200, first.text
        second = client.patch(path, params=params, headers=AUTH, json={"content": "第二版"})
        assert second.status_code == 200, second.text
        versions = client.get(path + "/versions", params=params, headers=AUTH).json()["versions"]
        old = next(item for item in versions if not item["current"])
        version_path = path + "/versions/" + old["version_id"]
        assert client.get(version_path, params=params, headers=AUTH).json()["content"] == "第一版"
        assert client.patch(version_path, params=params, headers=AUTH, json={"content": "校正历史"}).status_code == 200
        assert client.get(version_path, params=params, headers=AUTH).json()["content"] == "校正历史"
        assert client.get(path, params=params, headers=AUTH).json()["content"] == "第二版"
        assert client.delete(version_path, params=params, headers=AUTH).status_code == 200
        assert client.get(version_path, params=params, headers=AUTH).status_code == 404


def test_local_caption_folder_and_storage_readback(tmp_path, monkeypatch):
    from gw.asset_library import repository as repo
    root = tmp_path / "素材"
    root.mkdir()
    image = root / "镜头.png"
    image.write_bytes(b"fixture-bytes")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    asset_id = repo.index_local_files([str(image)], "image")["items"][0]["asset_id"]
    with TestClient(create_app()) as client:
        assert client.post("/api/local-assets/classify", headers=AUTH, json={"asset_ids": [asset_id]}).status_code == 200
        assert client.post("/api/local-assets/caption", headers=AUTH, json={"asset_ids": [asset_id]}).status_code == 200
        assert client.patch("/api/local-assets/caption", headers=AUTH, json={"asset_id": asset_id, "caption": "人工说明"}).status_code == 200
        items = client.get("/api/local-assets", headers=AUTH).json()["items"]
        assert items[0]["caption"] == "人工说明"
        folder = client.post("/api/local-assets/folders", headers=AUTH, json={"name": "初名"})
        assert folder.status_code == 201, folder.text
        assert client.patch("/api/local-assets/folders", headers=AUTH, json={"name": "初名", "new_name": "新名"}).status_code == 200
        data_root = Path(os.environ["GW_DATA_DIR"])
        assert not (data_root / "local_assets" / "初名").exists()
        assert (data_root / "local_assets" / "新名").is_dir()
        output = data_root / "local_assets" / "新名" / "字幕.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:01,000\n中文字幕\n", encoding="utf-8")
        name = output.relative_to(data_root).as_posix()
        assert name in [v["name"] for v in client.get("/api/storage-files", headers=AUTH).json()["items"]]
        assert client.post("/api/storage-files/delete", headers=AUTH, json={"names": [name]}).status_code == 200
        assert not output.exists()
        assert name not in [v["name"] for v in client.get("/api/storage-files", headers=AUTH).json()["items"]]


def test_remote_url_rejects_loopback_prefix_spoofing_and_credentials(monkeypatch):
    import socket
    from gw.settings import probes
    monkeypatch.setattr(probes.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 80))])
    with TestClient(create_app()) as client:
        for url in ("http://127.0.0.1.evil.test/file", "https://private.test/file"):
            response = client.post("/api/asset-registry/remote-assets", headers=AUTH, json={"url": url})
            assert response.status_code == 403, response.text
        response = client.post("/api/asset-registry/remote-assets", headers=AUTH,
                               json={"url": "http://user:secret@127.0.0.1/file"})
        assert response.status_code == 400 and "secret" not in response.text


def test_remote_head_does_not_follow_redirects_or_claim_success():
    with local_upstream() as (url, hits), TestClient(create_app()) as client:
        response = client.post("/api/asset-registry/remote-assets", headers=AUTH,
                               json={"url": url + "/redirect"})
        assert response.status_code == 200, response.text
        asset = response.json()["remote_asset"]
        assert asset["data_status"] == "degraded"
        assert asset["data_gaps"] == ["remote_head_status_302"]
        assert asset["content_type"] is None and asset["size_bytes"] is None
        assert hits == [("HEAD", "/redirect")]


def test_real_ffmpeg_registered_frame_storyboard_clip_and_transcode(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from PIL import Image
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("未安装ffmpeg/ffprobe；此环境不能声称真实媒体验收通过")
    root = tmp_path / "media"
    root.mkdir()
    source = root / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10",
                    "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                   check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    data_root = Path(os.environ["GW_DATA_DIR"])
    path = "/api/asset-registry/assets"
    with TestClient(create_app()) as client:
        created = client.post(path + "/import", headers=AUTH, json={"items": [{"name": "合成视频", "kind": "video"}]}).json()["asset_ids"][0]
        assert client.patch(
            path + "/" + created,
            headers=AUTH,
            json={
                "display_path": source.name,
                "root_id": storage.root_identity_id(root.resolve()),
            },
        ).status_code == 200
        frame = client.get(path + "/" + created + "/video/frame", headers=AUTH, params={"at": 0.5})
        assert frame.status_code == 200, frame.text
        with Image.open(data_root / frame.json()["output_name"]) as image:
            assert image.size == (160, 120)
            assert image.getpixel((80, 60))[0] > 200
        storyboard = client.get(path + "/" + created + "/video/storyboard", headers=AUTH)
        assert storyboard.status_code == 200, storyboard.text
        # 九宫格固定 9 帧（首帧、尾帧与 7 个等分中间帧），与视频时长无关。
        assert storyboard.json()["count"] == 9
        # 中间帧只作合成输入：合成成功后删除，只留一张固定横板 3×3 单图。
        assert storyboard.json()["frames"] == []
        assert (data_root / storyboard.json()["image"]).is_file()
        clipped = client.post(path + "/" + created + "/video/clip", headers=AUTH,
                              json={"start_seconds": 0, "end_seconds": 1})
        assert clipped.status_code == 200, clipped.text
        clip_path = data_root / clipped.json()["clip"]["output_name"]
        assert clip_path.is_file()
        transcoded = client.get("/api/media-transcode", headers=AUTH, params={"path": str(source)})
        assert transcoded.status_code == 200, transcoded.text
        output = tmp_path / "downloaded-transcode.mp4"
        output.write_bytes(transcoded.content)
        assert len(transcoded.content) == int(transcoded.headers["X-Transcoded-Bytes"])
        for target, maximum in ((clip_path, 1.5), (output, 2.2)):
            probe = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target)],
                                   check=True, capture_output=True, text=True, timeout=30)
            metadata = json.loads(probe.stdout)
            assert metadata["streams"][0]["codec_name"] == "h264"
            assert 0 < float(metadata["format"]["duration"]) <= maximum
        story_output = storyboard.json()["image"]
        # 分镜与缩略图共用 cache_folder 受管缓存根（QIN-510），按稳定 ID 分区。
        assert story_output.startswith(
            media_repo.cache_root_relative() + "/storyboards/" + created + "/"
        )
        story_bytes = client.get("/api/download-output", headers=AUTH, params={"path": story_output})
        assert story_bytes.status_code == 200
        assert story_bytes.content == (data_root / story_output).read_bytes()
        deleted = client.post("/api/asset-thumbnails/delete-storyboards", headers=AUTH, json={"asset_ids": [created]})
        # 中间帧已随合成删除，这里只剩那张 3×3 合成单图。
        assert deleted.status_code == 200 and deleted.json()["removed"] == 1
        assert deleted.json()["data_gaps"] == ["legacy_shared_frames_preserved"]
        assert client.get("/api/download-output", headers=AUTH, params={"path": story_output}).status_code == 404
        assert (data_root / frame.json()["output_name"]).is_file() and clip_path.is_file()
        repeated = client.post("/api/asset-thumbnails/delete-storyboards", headers=AUTH, json={"asset_ids": [created]})
        assert repeated.status_code == 200 and repeated.json()["removed"] == 0


def test_image_version_bytes_archive_alias_and_governance_restore(tmp_path, monkeypatch):
    from PIL import Image
    source = tmp_path / "image-root"
    source.mkdir()
    image = source / "version.png"
    Image.new("RGB", (16, 16), (10, 60, 210)).save(image)
    payload = image.read_bytes()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(source))
    base = "/api/asset-registry"
    with TestClient(create_app()) as client:
        asset_id = client.post(base + "/assets/import", headers=AUTH,
            json={"items": [{"name": "图片版本验证", "kind": "image"}]}).json()["asset_ids"][0]
        resource = base + "/assets/" + asset_id
        version = client.post(resource + "/image-versions", headers=AUTH,
            json={"source_path": str(image)}).json()["version"]
        downloaded = client.get(resource + "/image-versions/" + version["version_id"] + "/media", headers=AUTH)
        assert downloaded.status_code == 200 and downloaded.content == payload
        settings = client.get("/api/asset-thumbnails/settings", headers=AUTH)
        assert settings.status_code == 200, settings.text
        updated = client.patch(
            "/api/asset-thumbnails/settings",
            headers=AUTH,
            json={"format": "webp", "expected_version": settings.json()["revision"]},
        )
        assert updated.status_code == 200, updated.text
        thumb = client.post("/api/asset-thumbnails/generate", headers=AUTH,
            json={"path": str(image), "width": 64})
        assert thumb.status_code == 200
        thumb_download = client.get("/api/download-output", headers=AUTH, params={"path": thumb.json()["output"]})
        assert thumb_download.status_code == 200
        with Image.open(io.BytesIO(thumb_download.content)) as preview:
            assert preview.size == (64, 64)
        archive = client.post(base + "/assets/archive-bundle", headers=AUTH, json={"asset_ids": [asset_id]})
        assert archive.status_code == 200, archive.text
        with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
            assert package.read("version.png") == payload
        assert client.patch(resource, headers=AUTH, json={"archived": True}).status_code == 200
        overview = client.get(base + "/governance/overview", headers=GOV)
        assert overview.status_code == 200
        assert [item["asset_id"] for item in overview.json()["assets"]] == [asset_id]
        impact = client.get(base + "/governance/cascade-preview", headers=GOV,
            params={"target_type": "asset", "target_id": asset_id})
        assert impact.json()["impact"] == {"assets": [asset_id], "projects": [], "canvases": [], "total": 1}
        restored = client.post(base + "/governance/assets/" + asset_id + "/restore", headers=GOV)
        assert restored.status_code == 200
        assert client.get(resource, headers=AUTH).json()["asset"]["archived"] is False
        deleted = client.request("DELETE", resource, headers=AUTH,
                                 json={"expected_version": _registry_revision(client)})
        assert deleted.status_code == 200, deleted.text
        entry = deleted.json()["recycle_entry_id"]
        restored_trash = client.post(base + "/governance/asset-trash/" + entry + "/restore", headers=GOV,
                                     json={"expected_version": _registry_revision(client, GOV)})
        assert restored_trash.status_code == 200, restored_trash.text
        assert client.get(resource + "/media", headers=AUTH).content == payload


def test_online_image_real_http_download_readback(monkeypatch):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), (20, 170, 40)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/fixture.png")
                self.end_headers()
                self.wfile.write(b"redirect-not-image")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with TestClient(create_app()) as client:
            result = client.post("/api/online-image", headers=AUTH,
                json={"url": f"http://127.0.0.1:{server.server_port}/fixture.png"})
            assert result.status_code == 200, result.text
            downloaded = client.get("/api/download-output", headers=AUTH, params={"path": result.json()["output"]})
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == payload
            assert hits == ["/fixture.png"]
            redirected = client.post("/api/online-image", headers=AUTH,
                json={"url": f"http://127.0.0.1:{server.server_port}/redirect"})
            assert redirected.status_code == 503
            assert hits == ["/fixture.png", "/redirect"]
            from gw.media import repository as media_repo
            monkeypatch.setattr(media_repo, "MAX_REMOTE_BYTES", 16)
            oversized = client.post("/api/online-image", headers=AUTH,
                json={"url": f"http://127.0.0.1:{server.server_port}/oversized"})
            assert oversized.status_code == 413
            fetched = list((Path(os.environ["GW_DATA_DIR"]) / "media_output" / "online").glob("*"))
            assert len(fetched) == 1 and fetched[0].read_bytes() == payload
            credential_url = client.post("/api/online-image", headers=AUTH,
                json={"url": f"http://user:fixture-secret@127.0.0.1:{server.server_port}/secret"})
            assert credential_url.status_code == 400
            assert "fixture-secret" not in credential_url.text
            assert hits == ["/fixture.png", "/redirect", "/oversized"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_local_reveal_contract_checks_path_without_launching_program(tmp_path, monkeypatch):
    source = tmp_path / "allowed"
    source.mkdir()
    media = source / "本地文档.txt"
    media.write_text("真实路径准入，不启动外部程序", encoding="utf-8")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(source))
    with TestClient(create_app()) as client:
        reveal = client.post("/api/asset-file-reveal", headers=AUTH, json={"path": str(media)})
        assert reveal.status_code == 200, reveal.text
        assert reveal.json()["exists"] is True and reveal.json()["revealed"] is False
        assert reveal.json()["display_path"] == media.name
        asset_id = client.post("/api/asset-registry/assets/import", headers=AUTH,
            json={"items": [{"name": media.name}]}).json()["asset_ids"][0]
        resource = "/api/asset-registry/assets/" + asset_id
        assert client.patch(
            resource,
            headers=AUTH,
            json={
                "display_path": media.name,
                "root_id": storage.root_identity_id(source.resolve()),
            },
        ).status_code == 200
        opened = client.post(resource + "/open-local", headers=AUTH)
        assert opened.status_code == 200 and opened.json()["opened"] is False
        assert opened.json()["exists"] is True
        assert str(source) not in reveal.text and str(source) not in opened.text
