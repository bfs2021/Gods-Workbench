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

"""QIN-515 存储层「允许根内受控缓存目录」准入契约测试。

覆盖 AC8：越界、未配置允许根、重解析点（符号链接 / Junction）指向根外时，
缓存目录解析必须失败关闭且磁盘零写入；正常路径可在根内按需创建。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gw.core import storage
from gw.core.errors import CleanroomException

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="需要 Windows 重解析点")


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """隔离数据根与允许根；返回常用路径。"""
    data_root = tmp_path / "gw-data"
    allowed_root = tmp_path / "roots"
    data_root.mkdir()
    allowed_root.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed_root))
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    return {"data": data_root, "roots": allowed_root, "tmp": tmp_path}


def test_cache_directory_inside_root_is_created(runtime: dict[str, Path]) -> None:
    """根内目标按需创建，返回解析后的真实路径。"""
    target = runtime["roots"] / "god-cache"

    resolved = storage.resolve_cache_write_directory(str(target))

    assert resolved == target.resolve()
    assert resolved.is_dir()


def test_cache_directory_outside_roots_is_rejected(runtime: dict[str, Path]) -> None:
    """根外目录一律 403，且不创建任何目录。"""
    outside = runtime["tmp"] / "outside"

    with pytest.raises(CleanroomException) as error:
        storage.resolve_cache_write_directory(str(outside))

    assert error.value.status_code == 403
    assert error.value.code == "PATH_OUTSIDE_ALLOWED_ROOTS"
    assert not outside.exists()


def test_cache_directory_requires_allowed_roots(
    runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置允许根时不放行写盘能力。"""
    monkeypatch.setenv("GW_ALLOWED_ROOTS", "")

    with pytest.raises(CleanroomException) as error:
        storage.resolve_cache_write_directory(str(runtime["roots"] / "god-cache"))

    assert error.value.status_code == 403
    assert error.value.code == "LOCAL_FILE_ACCESS_NOT_ADMITTED"


def test_cache_directory_path_occupied_by_file_is_rejected(
    runtime: dict[str, Path]
) -> None:
    """目标位置已被同名文件占用时明确拒绝，不覆盖用户文件。"""
    occupied = runtime["roots"] / "god-cache"
    occupied.write_bytes(b"not-a-directory")

    with pytest.raises(CleanroomException) as error:
        storage.resolve_cache_write_directory(str(occupied))

    assert error.value.status_code == 400
    assert error.value.code == "INVALID_CACHE_DIRECTORY"
    assert occupied.is_file()


@WINDOWS_ONLY
def test_cache_directory_junction_outside_roots_is_rejected(
    runtime: dict[str, Path]
) -> None:
    """AC8：Junction 指向根外时拒绝写入，根外目录零新增。"""
    outside = runtime["tmp"] / "outside-junction"
    outside.mkdir()
    link = runtime["roots"] / "linked-cache"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        check=True,
    )
    try:
        with pytest.raises(CleanroomException) as error:
            storage.resolve_cache_write_directory(str(link / "god-cache"))
        assert error.value.status_code == 403
        assert error.value.code == "PATH_OUTSIDE_ALLOWED_ROOTS"
        assert list(outside.rglob("*")) == []
    finally:
        link.rmdir()
