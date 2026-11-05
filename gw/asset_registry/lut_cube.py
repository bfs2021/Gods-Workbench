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

"""Adobe Cube LUT 文件的有界结构校验与头信息提取。"""

from __future__ import annotations

from io import BytesIO, TextIOWrapper
import math
from pathlib import Path
import re

MAX_LUT_CUBE_BYTES = 64 * 1024 * 1024
_INTEGER = re.compile(r"^[0-9]+$")


class LutCubeError(ValueError):
    """带有稳定错误码的可读 Cube LUT 校验错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> LutCubeError:
    return LutCubeError("LUT_CUBE_INVALID", message)


def _number(token: str, line_number: int) -> float:
    try:
        value = float(token)
    except ValueError as exc:
        raise _invalid(f"LUT 第 {line_number} 行包含无效数字") from exc
    if not math.isfinite(value):
        raise _invalid(f"LUT 第 {line_number} 行包含非有限数字")
    return value


def parse_lut_cube_bytes(data: bytes) -> dict:
    """校验 Cube 头部与采样行数，仅返回可持久化的轻量元数据。"""
    if len(data) > MAX_LUT_CUBE_BYTES:
        raise LutCubeError("LUT_CUBE_TOO_LARGE", "LUT 文件超过 64 MiB 上限")
    if not data:
        raise _invalid("LUT 文件为空")

    metadata: dict = {}
    directives: set[str] = set()
    sample_rows = 0
    data_started = False
    line_number = 0
    try:
        lines = TextIOWrapper(BytesIO(data), encoding="utf-8-sig", errors="strict")
        for raw_line in lines:
            line_number += 1
            content = raw_line.split("#", 1)[0].strip()
            if not content:
                continue
            parts = content.split()
            directive = parts[0].upper()
            if directive in {
                "TITLE",
                "LUT_1D_SIZE",
                "LUT_3D_SIZE",
                "DOMAIN_MIN",
                "DOMAIN_MAX",
                "LUT_1D_INPUT_RANGE",
                "LUT_3D_INPUT_RANGE",
            }:
                if data_started:
                    raise _invalid(f"LUT 第 {line_number} 行头信息出现在采样数据之后")
                if directive in directives:
                    raise _invalid(f"LUT 头信息 {directive} 重复")
                directives.add(directive)
                if directive == "TITLE":
                    title = content[len(parts[0]):].strip()
                    if not title:
                        raise _invalid(f"LUT 第 {line_number} 行缺少 TITLE 内容")
                    if len(title) >= 2 and title[0] == title[-1] == '"':
                        title = title[1:-1]
                    metadata["title"] = title[:300]
                    continue
                if directive in {"LUT_1D_SIZE", "LUT_3D_SIZE"}:
                    if len(parts) != 2 or not _INTEGER.fullmatch(parts[1]):
                        raise _invalid(f"LUT 第 {line_number} 行的 {directive} 无效")
                    size = int(parts[1])
                    if directive == "LUT_1D_SIZE":
                        if not 2 <= size <= 65536:
                            raise _invalid("LUT_1D_SIZE 必须在 2 到 65536 之间")
                        metadata["lut_1d_size"] = size
                    else:
                        if not 2 <= size <= 256:
                            raise _invalid("LUT_3D_SIZE 必须在 2 到 256 之间")
                        metadata["lut_3d_size"] = size
                    continue
                if directive in {"DOMAIN_MIN", "DOMAIN_MAX"}:
                    if len(parts) != 4:
                        raise _invalid(f"LUT 第 {line_number} 行的 {directive} 必须有三个数值")
                    values = [_number(value, line_number) for value in parts[1:]]
                    metadata[directive.lower()] = values
                    continue
                if len(parts) != 3:
                    raise _invalid(f"LUT 第 {line_number} 行的 {directive} 必须有两个数值")
                values = [_number(value, line_number) for value in parts[1:]]
                if values[0] >= values[1]:
                    raise _invalid(f"LUT 第 {line_number} 行的输入范围无效")
                metadata[directive.lower()] = values
                continue

            if len(parts) != 3:
                raise _invalid(f"LUT 第 {line_number} 行必须是三个通道数值")
            values = [_number(value, line_number) for value in parts]
            del values
            data_started = True
            sample_rows += 1
    except UnicodeDecodeError as exc:
        raise _invalid("LUT 文件编码无效，需使用 UTF-8") from exc

    if "DOMAIN_MIN" in directives and "DOMAIN_MAX" in directives:
        if any(
            minimum >= maximum
            for minimum, maximum in zip(
                metadata["domain_min"], metadata["domain_max"], strict=True
            )
        ):
            raise _invalid("LUT DOMAIN_MIN 必须逐通道小于 DOMAIN_MAX")

    one_d_size = metadata.get("lut_1d_size", 0)
    three_d_size = metadata.get("lut_3d_size", 0)
    if not one_d_size and not three_d_size:
        raise _invalid("LUT 文件缺少 LUT_1D_SIZE 或 LUT_3D_SIZE")
    expected_rows = one_d_size + three_d_size**3
    if sample_rows != expected_rows:
        raise _invalid(f"LUT 采样行数应为 {expected_rows}，实际为 {sample_rows}")
    metadata["sample_count"] = sample_rows
    return metadata


def parse_lut_cube_file(path: str | Path) -> dict:
    """以固定上限读取并校验 Cube 文件。"""
    with Path(path).open("rb") as source:
        data = source.read(MAX_LUT_CUBE_BYTES + 1)
    return parse_lut_cube_bytes(data)
