# Copyright 2026 Gods-Workbench Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Phase 12 视频帧/分镜/剪辑的真实媒体探测。

真实数据源：允许根目录内的真实视频文件 + ffmpeg/ffprobe 子进程。
失败关闭：未配置允许根目录、素材无本地视频、ffmpeg 缺失或执行失败一律 503，
**禁止**返回伪造帧或伪造剪辑结果。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gw.asset_registry import repository as repo
from gw.core import storage
from gw.core.errors import CleanroomException

VIDEO_SUFFIXES = frozenset(".mp4 .mov .mkv .avi .webm .m4v .flv .wmv".split())
FFPROBE_TIMEOUT = 20
FFMPEG_TIMEOUT = 120


def _ffmpeg(name: str) -> str:
    """定位 ffmpeg/ffprobe；缺失即失败关闭（不静默降级）。"""
    path = shutil.which(name)
    if not path:
        repo._unavailable(
            "/api/asset-registry",
            "FFMPEG_NOT_AVAILABLE",
            "未找到 %s，媒体处理能力不可用" % name,
        )
    return path


def _local_video(asset_id: str) -> Path:
    """取素材登记的本地视频路径；无本地视频即失败关闭。"""
    item = repo._require(repo.state().read(), asset_id)
    display = item.get("display_path")
    if not display:
        repo._unavailable(
            "/api/asset-registry", "MEDIA_NOT_AVAILABLE", "该素材未登记本地视频路径"
        )
    root_id = item.get("root_id")
    path = storage.resolve_within_roots(
        str(display), root_id=str(root_id) if root_id else None
    )
    if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
        repo._unavailable(
            "/api/asset-registry", "MEDIA_NOT_AVAILABLE", "该素材没有可读取的本地视频"
        )
    return path


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """执行媒体子进程；失败一律转 503，不泄漏原始命令输出到响应体。"""
    try:
        return subprocess.run(
            args, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        repo._unavailable(
            "/api/asset-registry", "MEDIA_PROCESS_FAILED", "媒体处理进程执行失败"
        )


def _output_dir(asset_id: str) -> Path:
    relative = "media_output/" + asset_id
    out = storage.resolve_output_directory(relative, must_exist=False)
    out.mkdir(parents=True, exist_ok=True)
    return storage.resolve_output_directory(relative)


def probe(video: Path) -> dict[str, Any]:
    """用 ffprobe 读真实媒体元数据；解析失败即失败关闭。"""
    ffprobe = _ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration",
            "-of",
            "default=noprint_wrappers=1",
            str(video),
        ]
    )
    if completed.returncode != 0:
        repo._unavailable(
            "/api/asset-registry", "MEDIA_PROBE_FAILED", "无法解析该视频文件"
        )
    width = height = None
    duration = None
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "width" and value.isdigit():
            width = int(value)
        elif key == "height" and value.isdigit():
            height = int(value)
        elif key == "duration":
            try:
                duration = float(value)
            except ValueError:
                duration = None
    return {"width": width, "height": height, "duration_seconds": duration}


JXL_HEADER_READ_BYTES = 64 * 1024
JXL_CONTAINER_SIGNATURE = b"\x00\x00\x00\x0cJXL \r\n\x87\n"
_JXL_COLOR_SPACES = {0: "RGB", 1: "GRAY", 2: "XYB", 255: "UNKNOWN"}
_JXL_ASPECT_RATIOS = ((1, 1), (12, 10), (4, 3), (3, 2), (16, 9), (5, 4), (2, 1))


class _JxlBitReader:
    """按 JPEG XL 规范的低位优先顺序读取小段头部位流。"""

    def __init__(self, data: bytes):
        self._data = data
        self._position = 0

    def read(self, count: int) -> int:
        if count < 0 or self._position + count > len(self._data) * 8:
            raise ValueError("JXL header ended early")
        value = 0
        for bit_index in range(count):
            byte_index, bit_offset = divmod(self._position, 8)
            value |= ((self._data[byte_index] >> bit_offset) & 1) << bit_index
            self._position += 1
        return value

    def read_u32(self, distributions: tuple[int | tuple[int, int], ...]) -> int:
        selector = self.read(2)
        distribution = distributions[selector]
        if isinstance(distribution, int):
            return distribution
        offset, extra_bits = distribution
        return offset + self.read(extra_bits)


def _jxl_codestream_prefix(data: bytes) -> bytes | None:
    """从裸流或 ISO-BMFF 容器前缀定位首段 codestream。"""
    if data.startswith(b"\xff\x0a"):
        return data
    if not data.startswith(JXL_CONTAINER_SIGNATURE):
        return None

    offset = len(JXL_CONTAINER_SIGNATURE)
    for _ in range(64):
        if offset + 8 > len(data):
            return None
        box_size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if box_size == 1:
            if offset + 16 > len(data):
                return None
            box_size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        content_offset = offset + header_size
        if box_type == b"jxlc":
            return data[content_offset:]
        if box_type == b"jxlp":
            if content_offset + 4 > len(data):
                return None
            sequence = int.from_bytes(data[content_offset : content_offset + 4], "big")
            if sequence & 0x7FFFFFFF:
                return None
            return data[content_offset + 4 :]
        if box_size == 0 or box_size < header_size:
            return None
        next_offset = offset + box_size
        if next_offset <= offset or next_offset > len(data):
            return None
        offset = next_offset
    return None


def _parse_jxl_header(data: bytes) -> dict[str, Any] | None:
    """只解析 JXL 主头；无法安全跳过的变体交给 ffprobe。"""
    codestream = _jxl_codestream_prefix(data)
    if codestream is None or not codestream.startswith(b"\xff\x0a"):
        return None
    reader = _JxlBitReader(codestream[2:])
    try:
        small = bool(reader.read(1))
        if small:
            height = (reader.read(5) + 1) * 8
        else:
            height = reader.read_u32(((1, 9), (1, 13), (1, 18), (1, 30))) + 1
        ratio = reader.read(3)
        if ratio == 0:
            if small:
                width = (reader.read(5) + 1) * 8
            else:
                width = reader.read_u32(((1, 9), (1, 13), (1, 18), (1, 30))) + 1
        else:
            numerator, denominator = _JXL_ASPECT_RATIOS[ratio - 1]
            width = height * numerator // denominator
        if not width or not height or width > 0xFFFFFFFF or height > 0xFFFFFFFF:
            return None

        if reader.read(1):  # ImageMetadata 的全默认快捷编码
            bit_depth = 8
            xyb_encoded = True
            extra_channels = 0
            alpha_channels = 0
            color_space_code = 0
        else:
            if reader.read(1):  # extra_fields: 预览/动画/内在尺寸/色调映射
                return None
            floating_point = bool(reader.read(1))
            if floating_point:
                bit_depth = reader.read_u32((32, 16, 24, (1, 6)))
                exponent_bits = reader.read(4) + 1
                if exponent_bits < 2 or exponent_bits > 8:
                    return None
            else:
                bit_depth = reader.read_u32((8, 10, 12, (1, 6)))
            reader.read(1)  # modular_16_bit_buffer_sufficient
            extra_channels = reader.read_u32((0, 1, (2, 4), (1, 12)))
            if extra_channels > 32:
                return None
            alpha_channels = 0
            for _ in range(extra_channels):
                if not reader.read(1):  # 仅支持可安全跳过的默认 alpha 信息
                    return None
                alpha_channels += 1
            xyb_encoded = bool(reader.read(1))
            if reader.read(1):  # ColorEncoding 全默认即 RGB
                color_space_code = 0
            else:
                reader.read(1)  # want_icc
                color_space_code = reader.read_u32((0, 1, (2, 4), (18, 6)))

        profile_color_space = _JXL_COLOR_SPACES.get(
            color_space_code, "UNKNOWN:%d" % color_space_code
        )
        color_space = "XYB" if xyb_encoded else profile_color_space
        color_channels = 1 if profile_color_space == "GRAY" else 3
        return {
            "width": width,
            "height": height,
            "bit_depth": bit_depth,
            "color_space_identifier": color_space,
            "profile_color_space": profile_color_space,
            "xyb_encoded": xyb_encoded,
            "color_channels": color_channels,
            "extra_channels": extra_channels,
            "alpha_channels": alpha_channels,
            "channels": color_channels + extra_channels,
            "metadata_source": "jxl_header",
        }
    except (IndexError, ValueError):
        return None


def _probe_jxl_ffprobe(path: Path) -> dict[str, Any] | None:
    """头部格式超出轻量解析范围时，尝试复用已安装的 ffprobe。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,bits_per_raw_sample,bits_per_sample,pix_fmt,color_space,color_primaries,color_transfer,channels",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        streams = json.loads(completed.stdout.decode("utf-8", "replace")).get(
            "streams", []
        )
        stream = next(item for item in streams if isinstance(item, dict))
    except (ValueError, StopIteration, TypeError):
        return None

    result: dict[str, Any] = {"metadata_source": "ffprobe"}
    for key in ("width", "height", "channels"):
        value = stream.get(key)
        if isinstance(value, int) and value > 0:
            result[key] = value
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = stream.get(key)
        if isinstance(value, str) and value.isdigit():
            result["bit_depth"] = int(value)
            break
        if isinstance(value, int) and value > 0:
            result["bit_depth"] = value
            break
    color_space = stream.get("color_space") or stream.get("pix_fmt")
    if isinstance(color_space, str) and color_space:
        result["color_space_identifier"] = color_space
    return result if {"width", "height"}.issubset(result) else None


def probe_jxl_metadata(path: Path) -> dict[str, Any] | None:
    """读取有限头部并提取 JXL 元数据；无法解析时使用 ffprobe 后备。"""
    try:
        with path.open("rb") as image_file:
            prefix = image_file.read(JXL_HEADER_READ_BYTES)
    except OSError:
        return None
    metadata = _parse_jxl_header(prefix)
    return metadata if metadata is not None else _probe_jxl_ffprobe(path)


def storyboard_directory(asset_id: str, version: str | None = None) -> Path:
    """专用分镜根按精确稳定ID分区，禁止路径别名及目录链接。"""
    if (
        not asset_id
        or len(asset_id) > 128
        or not all(ch.isalnum() or ch in "_-" for ch in asset_id)
    ):
        raise CleanroomException(400, "INVALID_ASSET_ID", "素材标识不是安全单段")
    if version is None:
        relative = "media_output/storyboards/" + asset_id
    else:
        # 版本桶：同一资产不同版本走独立目录，避免复用旧帧。
        digest = hashlib.sha256(str(version).encode("utf-8")).hexdigest()[:12]
        relative = "media_output/storyboards/%s/v-%s" % (asset_id, digest)
    return storage.resolve_output_directory(relative, must_exist=False)


def is_storyboard_cache_name(name: str) -> bool:
    """与本模块抽帧生成命名共享精确规则，不删除任意扩展名或前缀。"""
    if re.fullmatch(r"frame_[0-9]+_[0-9]{3}\.png", name):
        return True
    return name.endswith(".part.png")


def extract_frame(asset_id: str, at: float) -> dict[str, Any]:
    return _extract_frame(asset_id, at, storyboard_cache=False)


def _extract_frame(
    asset_id: str,
    at: float,
    *,
    storyboard_cache: bool,
    version: str | None = None,
) -> dict[str, Any]:
    """在指定时间点真实抽帧并落盘，返回真实输出文件信息。"""
    video = _local_video(asset_id)
    ffmpeg = _ffmpeg("ffmpeg")
    directory = (
        storyboard_directory(asset_id, version)
        if storyboard_cache
        else _output_dir(asset_id)
    )
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / ("frame_%.3f.png" % float(at)).replace(".", "_", 1)
    if storyboard_cache:
        if not is_storyboard_cache_name(out.name):
            raise CleanroomException(
                400, "INVALID_TIME_RANGE", "分镜时间不是有效非负数"
            )
        out = storage.resolve_output_path(
            out.relative_to(storage.data_root().resolve()).as_posix(), must_exist=False
        )
    partial = out.parent / (out.stem + ".part" + out.suffix)
    try:
        completed = _run(
            [
                ffmpeg,
                "-y",
                "-ss",
                "%.3f" % float(at),
                "-i",
                str(video),
                "-frames:v",
                "1",
                str(partial),
            ]
        )
        if completed.returncode != 0 or not partial.is_file():
            repo._unavailable(
                "/api/asset-registry", "MEDIA_PROCESS_FAILED", "抽帧失败"
            )
        partial.replace(out)
    finally:
        partial.unlink(missing_ok=True)
    payload = {
        "asset_id": asset_id,
        "at_seconds": float(at),
        "output_name": out.relative_to(storage.data_root().resolve()).as_posix(),
        "size_bytes": out.stat().st_size,
        "data_status": "ok",
        "data_gaps": [],
    }
    payload.update(probe(video))
    return payload


def storyboard(asset_id: str, version: str | None) -> dict[str, Any]:
    """按等分时间点真实抽帧生成分镜；帧数依据真实时长计算。"""
    video = _local_video(asset_id)
    meta = probe(video)
    duration = meta.get("duration_seconds")
    if not duration or duration <= 0:
        repo._unavailable(
            "/api/asset-registry",
            "MEDIA_PROBE_FAILED",
            "无法确定视频时长，无法生成分镜",
        )
    count = max(1, min(12, int(duration // 2) or 1))
    step = duration / (count + 1)
    frames = []
    for index in range(1, count + 1):
        frame = _extract_frame(
            asset_id, step * index, storyboard_cache=True, version=version
        )
        frames.append(
            {
                "index": index,
                "at_seconds": round(step * index, 3),
                "output_name": frame["output_name"],
                "size_bytes": frame["size_bytes"],
            }
        )
    return {
        "asset_id": asset_id,
        "version": version,
        "duration_seconds": duration,
        "width": meta.get("width"),
        "height": meta.get("height"),
        "frames": frames,
        "count": len(frames),
        "data_status": "ok",
        "data_gaps": [],
    }


def clip(
    asset_id: str, start: float, end: float, expected_version: int | None
) -> dict[str, Any]:
    """真实剪辑视频片段并落盘；起止时间非法一律 400。"""
    if float(end) <= float(start) or float(start) < 0:
        raise CleanroomException(
            400, "INVALID_TIME_RANGE", "结束时间必须大于开始时间且非负"
        )
    out_dir = _output_dir(asset_id)
    out = out_dir / (
        "clip_%s_%s.mp4" % ("%.3f" % float(start), "%.3f" % float(end))
    ).replace(".", "_", 1)
    partial = out.parent / (out.stem + ".part" + out.suffix)

    def mutate(raw: dict[str, Any]) -> Any:
        repo._cas(raw, expected_version)
        video = _local_video(asset_id)
        ffmpeg = _ffmpeg("ffmpeg")
        try:
            completed = _run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    "%.3f" % float(start),
                    "-to",
                    "%.3f" % float(end),
                    "-i",
                    str(video),
                    "-c",
                    "copy",
                    str(partial),
                ]
            )
            if completed.returncode != 0 or not partial.is_file():
                repo._unavailable(
                    "/api/asset-registry", "MEDIA_PROCESS_FAILED", "视频剪辑失败"
                )
            partial.replace(out)
        finally:
            partial.unlink(missing_ok=True)
        clip_id = repo._seq(raw, "clip_")
        record = {
            "clip_id": clip_id,
            "asset_id": asset_id,
            "start_seconds": float(start),
            "end_seconds": float(end),
            "output_name": out.relative_to(storage.data_root().resolve()).as_posix(),
            "size_bytes": out.stat().st_size,
            "created_at": storage.now_iso(),
        }
        raw.setdefault("clips", {})[clip_id] = record
        revision = repo._bump(raw)
        return {
            "clip": record,
            "revision": revision,
            "data_status": "ok",
            "data_gaps": [],
        }

    return repo.state().mutate(mutate)
