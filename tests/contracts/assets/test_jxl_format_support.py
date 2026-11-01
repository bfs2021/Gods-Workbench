"""JXL 图片格式准入、轻量头部探测与原图直通契约。"""

from __future__ import annotations

import json
import subprocess

from fastapi.testclient import TestClient

from gw.api import routes_asset_registry
from gw.api.app import create_app
from gw.asset_library import repository as asset_library
from gw.asset_registry import collector, media_probe

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


def _jxl_header() -> bytes:
    """构造一个尺寸 32×16、10 位灰度并带默认 Alpha 通道的头部。"""
    bits: list[int] = []

    def write(value: int, count: int) -> None:
        bits.extend((value >> index) & 1 for index in range(count))

    # SizeHeader：小尺寸、16 高、无固定比例、32 宽。
    write(1, 1)
    write(1, 5)
    write(0, 3)
    write(3, 5)
    # ImageMetadata 使用非默认位深与一个默认 Alpha extra channel。
    write(0, 1)  # all_default
    write(0, 1)  # extra_fields
    write(0, 1)  # integer samples
    write(1, 2)  # 10-bit selector
    write(1, 1)  # modular_16_bit_buffer_sufficient
    write(1, 2)  # one extra channel
    write(1, 1)  # default Alpha channel metadata
    write(0, 1)  # XYB encoding disabled
    write(0, 1)  # ColorEncoding is not all-default
    write(1, 1)  # ICC profile present
    write(1, 2)  # gray color-space selector

    payload = bytearray(b"\xff\x0a")
    for offset in range(0, len(bits), 8):
        value = sum(bit << index for index, bit in enumerate(bits[offset : offset + 8]))
        payload.append(value)
    return bytes(payload)


def test_jxl_extension_classification_and_header_metadata(tmp_path):
    image = tmp_path / "sample.jxl"
    image.write_bytes(_jxl_header())

    assert asset_library.classify_name(image.name) == "image"
    metadata = media_probe.probe_jxl_metadata(image)
    assert metadata is not None
    assert {
        key: metadata[key]
        for key in (
            "width",
            "height",
            "bit_depth",
            "color_space_identifier",
            "profile_color_space",
            "xyb_encoded",
            "color_channels",
            "extra_channels",
            "alpha_channels",
            "channels",
            "metadata_source",
        )
    } == {
        "width": 32,
        "height": 16,
        "bit_depth": 10,
        "color_space_identifier": "GRAY",
        "profile_color_space": "GRAY",
        "xyb_encoded": False,
        "color_channels": 1,
        "extra_channels": 1,
        "alpha_channels": 1,
        "channels": 2,
        "metadata_source": "jxl_header",
    }


def test_jxl_collector_registration_and_direct_media_response(tmp_path, monkeypatch):
    header = _jxl_header()
    result = collector.store_content(
        header, "sample.jxl", "https://example.test/sample.jxl", owner="jxl-test"
    )
    assert result["asset"]["kind"] == "image"
    assert result["asset"]["metadata"]["jxl_metadata"]["metadata_source"] == "jxl_header"

    media_root = tmp_path / "media"
    media_root.mkdir()
    image = media_root / "sample.jxl"
    image.write_bytes(header)
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(media_root))
    monkeypatch.setattr(
        routes_asset_registry.repo, "media_file", lambda _asset_id: image
    )

    with TestClient(create_app()) as client:
        response = client.get(
            "/api/asset-registry/assets/ast_jxl/media", headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/jxl"
        assert response.content == header

        ranged = client.get(
            "/api/asset-registry/assets/ast_jxl/media",
            headers={**AUTH, "Range": "bytes=0-3"},
        )
        assert ranged.status_code == 206
        assert ranged.headers["content-type"] == "image/jxl"
        assert ranged.content == header[:4]


def test_jxl_metadata_falls_back_to_ffprobe(tmp_path, monkeypatch):
    image = tmp_path / "unsupported.jxl"
    image.write_bytes(b"unsupported-header")
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: "ffprobe")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "streams": [
                        {
                            "width": 640,
                            "height": 480,
                            "bits_per_raw_sample": "12",
                            "color_space": "rgb",
                            "channels": 3,
                        }
                    ]
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(media_probe.subprocess, "run", fake_run)
    assert media_probe.probe_jxl_metadata(image) == {
        "metadata_source": "ffprobe",
        "width": 640,
        "height": 480,
        "channels": 3,
        "bit_depth": 12,
        "color_space_identifier": "rgb",
    }