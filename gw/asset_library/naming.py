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

"""本地资产文件夹名称的跨接口校验规则。"""

from __future__ import annotations

import errno
from pathlib import Path

from gw.core.errors import CleanroomException

WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
)
ILLEGAL_CHARACTERS = frozenset('<>:\"/\\|?*')
MAX_NAME_BYTES = 255
MAX_PATH_LENGTH = 4096


def validate_folder_component_name(name: str, *, what: str = "文件夹名称") -> str:
    """校验单个文件夹名称段，保留输入原样，不静默清洗。"""
    if not isinstance(name, str):
        raise CleanroomException(400, "INVALID_PATH", f"{what}必须为字符串")

    if not name.strip():
        raise CleanroomException(400, "INVALID_PATH", f"{what}不能为空")

    encoded = name.encode("utf-8")
    if len(encoded) > MAX_NAME_BYTES:
        raise CleanroomException(
            400,
            "NAME_TOO_LONG",
            f"{what}超过最大字节限制（最多 {MAX_NAME_BYTES} 字节，当前为 {len(encoded)} 字节）",
        )

    if name.endswith((" ", ".")):
        raise CleanroomException(400, "INVALID_NAME_END", f"{what} '{name}' 不能以点或空格结尾")

    for char in name:
        if char in ILLEGAL_CHARACTERS or ord(char) < 32 or ord(char) == 127:
            raise CleanroomException(
                400,
                "INVALID_CHARACTERS",
                f"{what} '{name}' 包含非法字符或控制字符（禁止包含 <>:\"/\\|?* 及控制字符）",
            )

    base_name = name.split(".")[0].strip().upper()
    if base_name in WINDOWS_RESERVED_NAMES:
        raise CleanroomException(
            400,
            "RESERVED_NAME_FORBIDDEN",
            f"{what} '{name}' 命中 Windows 系统保留名称，禁止使用",
        )

    return name


def _handle_os_path_error(exc: OSError) -> None:
    """将过长路径错误映射为稳定的客户端错误。"""
    err = getattr(exc, "errno", None)
    winerr = getattr(exc, "winerror", None)
    is_long = err in (errno.ENAMETOOLONG, 36, 63) or winerr in (206, 111)
    if is_long:
        raise CleanroomException(400, "NAME_TOO_LONG", "文件或文件夹名称过长") from exc


def validate_target_path(target: Path, root: Path) -> None:
    """校验目标路径包含关系、名称段与绝对路径长度。"""
    try:
        resolved_target = target.resolve()
        resolved_root = root.resolve()
    except OSError as exc:
        _handle_os_path_error(exc)
        raise

    encoded_path = str(resolved_target).encode("utf-8")
    if len(encoded_path) > MAX_PATH_LENGTH:
        raise CleanroomException(
            400,
            "PATH_TOO_LONG",
            f"目标路径长度超过系统最大限制（{MAX_PATH_LENGTH} 字节）",
        )

    if resolved_target == resolved_root:
        return

    try:
        rel = resolved_target.relative_to(resolved_root)
    except ValueError:
        raise CleanroomException(
            403, "PATH_OUTSIDE_ALLOWED_ROOTS", "目标路径超出允许的根目录范围"
        ) from None

    for part in rel.parts:
        validate_folder_component_name(part)
