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
import math
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
#: 旧版分镜根（QIN-510 前固定目录）；只读与删除兼容，新人不再写入。
LEGACY_STORYBOARD_ROOT = "media_output/storyboards"
#: 分镜产物在受管缓存根内的固定子目录名。
STORYBOARD_NAMESPACE = "storyboards"
#: 九宫格固定 9 帧：首帧、尾帧与 7 个逐级二分的中间帧。
STORYBOARD_FRAME_COUNT = 9
#: 九宫格合成单图：3 列 × 3 行，宽边上限 2160（每格 720）。
STORYBOARD_COLUMNS = 3
STORYBOARD_MAX_WIDTH = 2160
STORYBOARD_TILE_WIDTH = STORYBOARD_MAX_WIDTH // STORYBOARD_COLUMNS
STORYBOARD_IMAGE_NAME = "storyboard.jpg"
STORYBOARD_IMAGE_QUALITY = 88
#: 尾帧留出的短尾窗，避免 ``-ss`` 落在 EOF 上抽不到帧。
STORYBOARD_TAIL_WINDOW_MIN_SEC = 0.04
STORYBOARD_TAIL_WINDOW_RATIO = 0.02
STORYBOARD_TAIL_WINDOW_MAX_SEC = 1.0


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
            "stream=width,height,duration,r_frame_rate",
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
    fps = None
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
        elif key == "r_frame_rate":
            fps = _parse_rate(value)
    return {
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "fps": fps,
    }


def _parse_rate(raw: str) -> float | None:
    """解析 ffprobe 的分数式帧率（如 30000/1001）；无法解析返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    numerator, _, denominator = text.partition("/")
    try:
        top = float(numerator)
        bottom = float(denominator) if denominator else 1.0
    except (TypeError, ValueError):
        return None
    if top <= 0 or bottom <= 0 or not math.isfinite(top / bottom):
        return None
    return top / bottom


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


def _storyboard_asset_id(asset_id: str) -> str:
    if (
        not asset_id
        or len(asset_id) > 128
        or not all(ch.isalnum() or ch in "_-" for ch in asset_id)
    ):
        raise CleanroomException(400, "INVALID_ASSET_ID", "素材标识不是安全单段")
    return asset_id


def storyboard_relative(
    asset_id: str,
    version: str | None = None,
    *,
    cache_root: str | None,
    legacy: bool = False,
) -> str:
    """分镜目录相对路径：与图片缩略图共用同一 cache_folder 受管缓存根。

    缓存根由调用方显式注入（``cache_root``），本模块不反向依赖媒体仓储，
    避免 asset_registry → media 的领域循环导入。
    旧版固定 ``media_output/storyboards`` 的缓存仍可被读取与删除（legacy=True），
    避免升级后历史产物变成无人清理的孤儿文件。
    """
    _storyboard_asset_id(asset_id)
    if legacy:
        root = LEGACY_STORYBOARD_ROOT
    elif not cache_root:
        raise CleanroomException(
            503, "CACHE_ROOT_UNAVAILABLE", "分镜缓存根未解析，无法确定落盘位置"
        )
    else:
        root = str(cache_root).strip("/") + "/" + STORYBOARD_NAMESPACE
    if version is None:
        return root + "/" + asset_id
    # 版本桶：同一资产不同版本走独立目录，避免复用旧帧。
    digest = hashlib.sha256(str(version).encode("utf-8")).hexdigest()[:12]
    return "%s/%s/v-%s" % (root, asset_id, digest)


def storyboard_directory(
    asset_id: str, version: str | None = None, *, cache_root: str | None
) -> Path:
    """专用分镜根按精确稳定ID分区，禁止路径别名及目录链接。"""
    return storage.resolve_output_directory(
        storyboard_relative(asset_id, version, cache_root=cache_root), must_exist=False
    )


def legacy_storyboard_directory(asset_id: str, version: str | None = None) -> Path:
    """旧版分镜根：仅用于清理历史缓存，不再写入。"""
    return storage.resolve_output_directory(
        storyboard_relative(asset_id, version, cache_root=None, legacy=True),
        must_exist=False,
    )


def is_storyboard_cache_name(name: str) -> bool:
    """与本模块抽帧生成命名共享精确规则，不删除任意扩展名或前缀。"""
    if re.fullmatch(r"frame_[0-9]+_[0-9]{3}\.png", name):
        return True
    if name == STORYBOARD_IMAGE_NAME:
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
    cache_root: str | None = None,
    scale_width: int | None = None,
) -> dict[str, Any]:
    """在指定时间点真实抽帧并落盘，返回真实输出文件信息。

    ``scale_width`` 仅用于分镜链路：图幅收到九宫格单格宽度，避免为合成单图
    保留 4K 级中间帧（9 帧 × 247 素材会白占几百 MB）。
    """
    video = _local_video(asset_id)
    ffmpeg = _ffmpeg("ffmpeg")
    directory = (
        storyboard_directory(asset_id, version, cache_root=cache_root)
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
    command = [
        ffmpeg,
        "-y",
        "-ss",
        "%.3f" % float(at),
        "-i",
        str(video),
    ]
    if scale_width:
        command += ["-vf", "scale=%d:-2" % int(scale_width)]
    command += ["-frames:v", "1", str(partial)]
    try:
        completed = _run(command)
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


def storyboard_frame_name(at: float) -> str:
    """分镜帧文件名：与 is_storyboard_cache_name 共享同一精确规则。"""
    return ("frame_%.3f.png" % float(at)).replace(".", "_", 1)


def _reusable_frame(
    asset_id: str,
    at: float,
    video: Path,
    version: str | None,
    cache_root: str | None,
) -> dict[str, Any] | None:
    """已生成帧是否可直接复用：存在、非空且不早于源视频修改时间。"""
    directory = storyboard_directory(asset_id, version, cache_root=cache_root)
    relative = (
        directory.relative_to(storage.data_root().resolve()).as_posix()
        + "/"
        + storyboard_frame_name(at)
    )
    try:
        target = storage.resolve_output_path(relative)
        stat = target.stat()
    except (CleanroomException, OSError):
        return None
    if stat.st_size <= 0:
        return None
    try:
        if stat.st_mtime_ns < video.stat().st_mtime_ns:
            return None
    except OSError:
        return None
    return {"output_name": relative, "size_bytes": stat.st_size}


def storyboard(
    asset_id: str,
    version: str | None,
    *,
    cache_root: str | None,
    force: bool = False,
) -> dict[str, Any]:
    """按九宫格时间点生成分镜；命中已生成缓存时直接复用，不重复抽帧。"""
    video = _local_video(asset_id)
    meta = probe(video)
    duration = meta.get("duration_seconds")
    if not duration or duration <= 0:
        repo._unavailable(
            "/api/asset-registry",
            "MEDIA_PROBE_FAILED",
            "无法确定视频时长，无法生成分镜",
        )
    stamps = storyboard_timestamps(duration, meta.get("fps"))
    tile_width = _storyboard_tile_width(meta.get("width"))
    frames = []
    generated = 0
    cached = 0
    for index, at in enumerate(stamps, start=1):
        reused = (
            None if force else _reusable_frame(asset_id, at, video, version, cache_root)
        )
        if reused is not None:
            cached += 1
            frames.append(
                {
                    "index": index,
                    "at_seconds": round(at, 3),
                    "output_name": reused["output_name"],
                    "size_bytes": reused["size_bytes"],
                    "status": "cached",
                }
            )
            continue
        frame = _extract_frame(
            asset_id,
            at,
            storyboard_cache=True,
            version=version,
            cache_root=cache_root,
            scale_width=tile_width,
        )
        generated += 1
        frames.append(
            {
                "index": index,
                "at_seconds": round(at, 3),
                "output_name": frame["output_name"],
                "size_bytes": frame["size_bytes"],
                "status": "generated",
            }
        )
    image = _compose_storyboard(
        asset_id,
        frames,
        version=version,
        cache_root=cache_root,
        video=video,
        force=force,
    )
    return {
        "asset_id": asset_id,
        "version": version,
        "duration_seconds": duration,
        "width": meta.get("width"),
        "height": meta.get("height"),
        "frames": frames,
        "count": len(frames),
        "generated": generated,
        "cached": cached,
        "image": image["output_name"],
        "image_width": image["width"],
        "image_height": image["height"],
        "image_status": image["status"],
        "tile_width": tile_width,
        "data_status": "ok",
        "data_gaps": [] if len(frames) == STORYBOARD_FRAME_COUNT else ["storyboard_frames_reduced"],
    }


def _storyboard_tile_width(source_width: Any) -> int:
    """单格宽度：不超过 720（3 格合计 ≤ 2160）；窄源不放大。"""
    try:
        width = int(source_width)
    except (TypeError, ValueError):
        return STORYBOARD_TILE_WIDTH
    if width <= 0:
        return STORYBOARD_TILE_WIDTH
    return min(width, STORYBOARD_TILE_WIDTH)


def storyboard_image_relative(
    asset_id: str,
    version: str | None = None,
    *,
    cache_root: str | None,
) -> str:
    """九宫格合成单图在数据根内的相对路径。"""
    return (
        storyboard_directory(asset_id, version, cache_root=cache_root)
        .relative_to(storage.data_root().resolve())
        .as_posix()
        + "/"
        + STORYBOARD_IMAGE_NAME
    )


def _compose_storyboard(
    asset_id: str,
    frames: list[dict[str, Any]],
    *,
    version: str | None,
    cache_root: str | None,
    video: Path,
    force: bool = False,
) -> dict[str, Any]:
    """把 9 帧拼成一张 3×3 单图；未过期则直接复用，不重复合成。"""
    from PIL import Image, UnidentifiedImageError

    relative = storyboard_image_relative(asset_id, version, cache_root=cache_root)
    target = storage.resolve_output_path(relative, must_exist=False)
    try:
        stat = target.stat()
    except OSError:
        stat = None
    if (
        not force
        and stat is not None
        and stat.st_size > 0
        and stat.st_mtime_ns >= video.stat().st_mtime_ns
    ):
        try:
            with Image.open(target) as cached:
                return {
                    "output_name": relative,
                    "width": cached.width,
                    "height": cached.height,
                    "status": "cached",
                }
        except (OSError, UnidentifiedImageError):
            pass  # 损坏的旧图按未命中处理，重新合成。
    if not frames:
        repo._unavailable(
            "/api/asset-registry", "MEDIA_PROCESS_FAILED", "没有可分镜的帧"
        )
    images = []
    try:
        for frame in frames:
            images.append(
                Image.open(storage.resolve_output_path(frame["output_name"])).convert(
                    "RGB"
                )
            )
    except (CleanroomException, OSError, UnidentifiedImageError):
        for image in images:
            image.close()
        repo._unavailable(
            "/api/asset-registry", "MEDIA_PROCESS_FAILED", "九宫格合成失败"
        )
    tile_width = max(image.width for image in images)
    tile_height = max(image.height for image in images)
    canvas = Image.new(
        "RGB",
        (tile_width * STORYBOARD_COLUMNS, tile_height * STORYBOARD_COLUMNS),
        (0, 0, 0),
    )
    for index, image in enumerate(images[: STORYBOARD_FRAME_COUNT]):
        row, column = divmod(index, STORYBOARD_COLUMNS)
        tile = (
            image
            if (image.width, image.height) == (tile_width, tile_height)
            else image.resize((tile_width, tile_height))
        )
        canvas.paste(tile, (column * tile_width, row * tile_height))
    partial = target.parent / (".part-" + STORYBOARD_IMAGE_NAME)
    canvas_width = tile_width * STORYBOARD_COLUMNS
    canvas_height = tile_height * STORYBOARD_COLUMNS
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.unlink(missing_ok=True)
        canvas.save(partial, format="JPEG", quality=STORYBOARD_IMAGE_QUALITY, optimize=True)
        partial.replace(target)
    except OSError:
        repo._unavailable(
            "/api/asset-registry", "MEDIA_PROCESS_FAILED", "九宫格保存失败"
        )
    finally:
        for image in images:
            image.close()
        canvas.close()
        partial.unlink(missing_ok=True)
    return {
        "output_name": relative,
        "width": canvas_width,
        "height": canvas_height,
        "status": "generated",
    }


def storyboard_timestamps(duration: float, fps: float | None = None) -> list[float]:
    """九宫格抽帧时间点：首帧、尾帧与 7 个逐级二分中间帧，共 9 个。

    等价于用户口径的递归二分：先取首/中/尾三帧，再在相邻帧之间补中间帧，
    连续补两级后正好得到 9 个等距点。

    尾部留出短尾窗：``-ss`` 落在最后一帧之后（但仍在容器时长内）会抽不到帧，
    让整批失败关闭。有帧率时按 1.5 帧留窗，否则退回按时长比例留窗。
    极短视频去重后可能少于 9 帧，此时由调用方在 ``data_gaps`` 如实标注。
    """
    span = float(duration)
    if not math.isfinite(span) or span <= 0:
        raise CleanroomException(400, "INVALID_TIME_RANGE", "视频时长不合法")
    if isinstance(fps, (int, float)) and math.isfinite(float(fps)) and float(fps) > 0:
        tail = min(STORYBOARD_TAIL_WINDOW_MAX_SEC, 1.5 / float(fps))
    else:
        tail = min(
            STORYBOARD_TAIL_WINDOW_MAX_SEC,
            max(STORYBOARD_TAIL_WINDOW_MIN_SEC, span * STORYBOARD_TAIL_WINDOW_RATIO),
        )
    tail = max(STORYBOARD_TAIL_WINDOW_MIN_SEC, min(STORYBOARD_TAIL_WINDOW_MAX_SEC, tail))
    effective = max(0.0, span - tail)
    stamps = [
        round(effective * index / (STORYBOARD_FRAME_COUNT - 1), 3)
        for index in range(STORYBOARD_FRAME_COUNT)
    ]
    deduped: list[float] = []
    for at in stamps:
        if not deduped or at > deduped[-1]:
            deduped.append(at)
    return deduped


def _valid_segment_range(segment: Any) -> tuple[float, float] | None:
    """只接受时间戳合法的片段；非法片段保持原值，不伪造缩略图。"""
    if not isinstance(segment, dict):
        return None
    start, end = segment.get("start_sec"), segment.get("end_sec")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    try:
        start_sec, end_sec = float(start), float(end)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(start_sec) and math.isfinite(end_sec)):
        return None
    if start_sec < 0 or end_sec <= start_sec:
        return None
    return start_sec, end_sec


def storyboard_thumbnails(
    asset_id: str, *, cache_root: str | None, force: bool = False
) -> dict[str, Any]:
    """生成九宫格与片段缩略图，并把真实落盘路径回写 ``video_segments``。

    任一片段抽帧失败即整体失败关闭：注册表保持原值，不写占位路径或假路径。
    已存在的帧按源视频 mtime 复用，重复生成不会产生新文件。
    """
    grid = storyboard(asset_id, None, cache_root=cache_root, force=force)
    video = _local_video(asset_id)
    record = repo.get_asset(asset_id)
    metadata = record.get("metadata")
    segments = metadata.get("video_segments") if isinstance(metadata, dict) else None
    result: dict[str, Any] = {
        "asset_id": asset_id,
        "storyboard": grid,
        "frames": grid["count"],
        "segment_total": 0,
        "segment_generated": 0,
        "segment_cached": 0,
        "segments_updated": 0,
        "generated": grid["generated"],
        "cached": grid["cached"],
        "data_status": "ok",
        "data_gaps": [],
    }
    if not isinstance(segments, list) or not segments:
        # 未登记片段的素材只生成九宫格，不凭空造 video_segments。
        result["data_gaps"] = ["video_segments_absent"]
        return result
    updated: list[Any] = []
    segment_generated = 0
    segment_cached = 0
    changed = False
    for segment in segments:
        window = _valid_segment_range(segment)
        if window is None:
            updated.append(segment)
            continue
        midpoint = (window[0] + window[1]) / 2
        reused = (
            None
            if force
            else _reusable_frame(asset_id, midpoint, video, None, cache_root)
        )
        if reused is not None:
            segment_cached += 1
            thumb = reused["output_name"]
        else:
            frame = _extract_frame(
                asset_id,
                midpoint,
                storyboard_cache=True,
                cache_root=cache_root,
                scale_width=grid.get("tile_width"),
            )
            segment_generated += 1
            thumb = frame["output_name"]
        if segment.get("thumbnail") != thumb:
            changed = True
        updated.append({**segment, "thumbnail": thumb})
    if changed:
        repo.update_asset(asset_id, {"video_segments": updated})
    result.update(
        {
            "segment_total": len(segments),
            "segment_generated": segment_generated,
            "segment_cached": segment_cached,
            "segments_updated": sum(
                1
                for item in updated
                if isinstance(item, dict) and item.get("thumbnail")
            ),
            "generated": grid["generated"] + segment_generated,
            "cached": grid["cached"] + segment_cached,
        }
    )
    return result


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
