
"""Phase 12 A4 媒体与缩略图契约测试（真实落盘语义）。

覆盖：401 / 403 / 写后读回 / 真实 PIL 缩略图 / 真实 ffmpeg 波形 /
后台任务可回读 / 越界与未配置根目录 403 / 删除后不可读 / 依赖缺失 503。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "docs/contracts/MEDIA-INTERFACE-CATALOG.yaml"
FIX = ROOT / "docs/fixtures"
AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
READONLY = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "readonly"}


def pairs():
    return re.findall(r"method: (\w+), path: (\S+)", CATALOG.read_text(encoding="utf-8"))


def norm(p):
    return p[:-1] if p.endswith("}") and not p.endswith("{p}") else p


def path(p):
    return norm(p).replace("{p}", "tjob_0001")


def test_b5_contract_and_fixtures():
    ps = pairs()
    assert len(ps) == 16 and len({p for _, p in ps}) == 14
    f = json.loads((FIX / "phase11-b5-media-boundary.json").read_text(encoding="utf-8"))
    assert f["path_count"] == 14 and f["method_path_count"] == 16
    assert "p12-a4-1" in CATALOG.read_text(encoding="utf-8")
    for n in ("phase11-b5-media-boundary.json", "phase11-b5-fail-closed.json",
              "phase11-b5-async-policy.json"):
        assert (FIX / n).is_file()


def test_b5_auth_and_readonly_boundary():
    with TestClient(create_app()) as c:
        for m, p in pairs():
            real = path(p)
            payload = {} if m in ("POST", "PATCH", "DELETE") else None
            if payload is not None:
                r = c.request(m, real, json=payload)
            else:
                r = c.request(m, real)
            assert r.status_code == 401, (m, real, r.text)
            if payload is not None:
                r = c.request(m, real, headers=READONLY, json=payload)
                assert r.status_code == 403, (m, real, r.text)


def test_b5_settings_write_readback():
    with TestClient(create_app()) as c:
        settings = c.get("/api/asset-thumbnails/settings", headers=AUTH)
        assert settings.status_code == 200, settings.text
        r = c.patch("/api/asset-thumbnails/settings", headers=AUTH,
                    json={"mode": "custom", "cache_folder": "god-cache", "width": 320,
                          "expected_version": settings.json()["revision"]})
        assert r.status_code == 200, r.text
        assert r.json()["width"] == 320
        g = c.get("/api/asset-thumbnails/settings", headers=AUTH)
        assert g.status_code == 200 and g.json()["width"] == 320
        assert g.json()["data_status"] == "ok"
        # 凭据字段必须被剥离，不落库不回显。
        p = c.patch("/api/asset-proxy/settings", headers=AUTH,
                    json={"enabled": True, "base_url": "https://example.test",
                          "api_keys": "SHOULD-NOT-STORED", "token": "SHOULD-NOT-STORED"})
        assert p.status_code == 200, p.text
        body = json.dumps(p.json(), ensure_ascii=False)
        assert "SHOULD-NOT-STORED" not in body
        assert not c.get("/api/asset-proxy/settings", headers=AUTH).json().get("api_keys")


def test_b5_local_file_read_write_and_thumbnail(tmp_path, monkeypatch):
    roots = tmp_path / "roots"
    roots.mkdir()
    source = roots / "pic.png"
    from PIL import Image
    Image.new("RGB", (400, 200), (10, 20, 30)).save(source)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(roots))

    with TestClient(create_app()) as c:
        settings = c.get("/api/asset-thumbnails/settings", headers=AUTH)
        assert settings.status_code == 200, settings.text
        updated = c.patch(
            "/api/asset-thumbnails/settings",
            headers=AUTH,
            json={"format": "webp", "expected_version": settings.json()["revision"]},
        )
        assert updated.status_code == 200, updated.text
        r = c.post("/api/asset-thumbnails/generate", headers=AUTH,
                   json={"path": str(source), "width": 128})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["data_status"] == "ok" and data["generated"] == 1
        assert str(roots) not in json.dumps(data, ensure_ascii=False)
        Path(r.json()["output"])
        # output 是相对展示串；真实文件落在 GW_DATA_DIR 内，按 cache_folder/分桶分层。
        real = Path(os.environ["GW_DATA_DIR"]) / Path(r.json()["output"]).parent
        files = list(real.glob("*.webp"))
        assert files, list(real.glob("*"))
        with Image.open(files[0]) as image:
            assert image.width == 128 and image.height == 64

        # 后台任务：真实执行后可用 GET 回读。
        bg = c.post("/api/asset-thumbnails/generate-background", headers=AUTH,
                    json={"path": str(source), "width": 96})
        assert bg.status_code == 202, bg.text
        job_id = bg.json()["job_id"]
        assert bg.json()["poll_hint"] == "/api/asset-thumbnails/jobs/%s" % job_id
        g = c.get("/api/asset-thumbnails/jobs/%s" % job_id, headers=AUTH)
        assert g.status_code == 200 and g.json()["status"] == "succeeded"

        # 删除后不可再读。
        # 派生物 key 已改为源身份摘要；删除按返回 output 的精确文件名。
        d = c.post("/api/asset-thumbnails/delete", headers=AUTH,
                   json={"asset_ids": [Path(data["output"]).stem.split(".")[0]]})
        assert d.status_code == 200 and d.json()["removed"] >= 1
        assert not list(real.glob("pic*.webp"))

        # 越界 → 403，且不回显绝对路径。
        outside = tmp_path / "outside.png"
        Image.new("RGB", (10, 10), (1, 2, 3)).save(outside)
        bad = c.post("/api/asset-thumbnails/generate", headers=AUTH, json={"path": str(outside)})
        assert bad.status_code == 403
        assert str(outside) not in json.dumps(bad.json(), ensure_ascii=False)


def test_b5_unconfigured_roots_fails_closed(tmp_path):
    source = tmp_path / "x.png"
    from PIL import Image
    Image.new("RGB", (10, 10)).save(source)
    with TestClient(create_app()) as c:
        r = c.post("/api/asset-thumbnails/generate", headers=AUTH, json={"path": str(source)})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "LOCAL_FILE_ACCESS_NOT_ADMITTED"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 未安装")
def test_b5_waveform_real_rms(tmp_path, monkeypatch):
    import math
    import struct
    roots = tmp_path / "roots"
    roots.mkdir()
    wav = roots / "tone.wav"
    rate = 16000
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
                      for i in range(rate // 2))
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(roots))
    with TestClient(create_app()) as c:
        r = c.get("/api/audio-waveform-data", headers=AUTH, params={"path": str(wav)})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["bucket_count"] > 0 and d["duration"] > 0
        assert not (all(v == 0 for v in d["maximum"]) and all(v == 0 for v in d["minimum"]))
        assert str(roots) not in json.dumps(d, ensure_ascii=False)


def test_b5_missing_dependency_fails_closed(tmp_path, monkeypatch):
    roots = tmp_path / "roots"
    roots.mkdir()
    source = roots / "pic.png"
    from PIL import Image
    Image.new("RGB", (40, 40)).save(source)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(roots))
    import gw.media.repository as repo  # noqa: F401  # import side effect required
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with TestClient(create_app()) as c:
        r = c.get("/api/media-preview", headers=AUTH,
                  params={"path": str(source), "w": "64"})
        assert r.status_code in (200, 503)
        v = roots / "clip.mp4"
        v.write_bytes(b"not-a-real-video")
        r2 = c.get("/api/media-transcode", headers=AUTH, params={"path": str(v)})
        assert r2.status_code == 503
        assert r2.json()["detail"]["data_status"] == "not_integrated"


def test_b5_online_image_ssrf_blocked():
    with TestClient(create_app()) as c:
        r = c.post("/api/online-image", headers=AUTH,
                   json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert r.status_code == 403
        d = r.json()["detail"]
        assert d["code"] == "SSRF_BLOCKED"
        assert "169.254.169.254" not in json.dumps(d)
