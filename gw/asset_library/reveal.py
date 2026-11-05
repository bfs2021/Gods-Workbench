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

"""我的资源「在系统中打开」（QIN-546）。

只做两件事：把请求路径解析并锁定在允许根内，然后用 argv 直接唤起系统文件管理器。
约束与既有原生目录选择器一致：

- 不经过 shell，路径作为独立 argv 传入，不存在命令注入面；
- 不等待子进程退出（explorer 常以非 0 退出码结束），不阻塞事件循环；
- 无图形环境或启动失败如实返回 ``unavailable`` 与原因，绝不静默吞掉。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from gw.core import storage
from gw.core.errors import CleanroomException


def resolve_browsable_path(raw_path: str, root_id: str | None = None) -> tuple[Path, str]:
    """解析并校验「只读打开」目标路径，返回 (resolved_path, root_id)。

    与整理写操作不同，打开文件管理器不改变磁盘内容，因此不套用只读根拒绝；
    但仍必须落在允许根内、不得指向应用内部数据目录。
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise CleanroomException(400, "INVALID_PATH", "路径不合法")
    if "\0" in raw_path:
        raise CleanroomException(400, "INVALID_PATH", "路径包含非法字符")

    resolved = storage.resolve_request_path(raw_path, root_id=root_id)
    resolved_res = resolved.resolve()

    data_root = storage.data_root().resolve()
    if resolved_res == data_root or resolved_res.is_relative_to(data_root):
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "禁止打开应用内部数据目录")

    root_identity, _ = storage.location_identity(resolved)
    known = {storage.root_identity_id(root) for root in storage.allowed_roots()}
    if root_identity not in known:
        raise CleanroomException(403, "PATH_OUTSIDE_ALLOWED_ROOTS", "目标路径不属于任何允许的挂载目录")

    if not resolved_res.exists():
        raise CleanroomException(404, "NOT_FOUND", "目标文件或文件夹不存在")

    return resolved_res, root_identity


def _spawn(argv: list[str]) -> None:
    """以独立 argv 唤起系统程序；不等待退出。"""
    subprocess.Popen(  # noqa: S603 - argv 直传，无 shell 参与
        argv,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def _launcher_argv(target: Path) -> list[str]:
    """按平台给出打开目录的命令行；路径始终是最后一个独立参数。"""
    if sys.platform.startswith("win"):
        # explorer.exe 打开目录；不能加引号包裹，必须由 argv 直接承载。
        return ["explorer.exe", str(target)]
    if sys.platform == "darwin":
        return ["open", str(target)]
    return ["xdg-open", str(target)]


def open_in_file_manager(target: Path) -> dict[str, Any]:
    """在系统文件管理器中打开目录，返回结构化结果。

    返回字段固定为 ``status``/``reason``：
    - ``opened``：已发出唤起请求（子进程退出码不作为成功判据）；
    - ``unavailable``：无图形环境或启动失败，``reason`` 说明原因。
    """
    argv = _launcher_argv(target)
    try:
        # 目录被删或无权访问时，某些平台只会在子进程里失败；先做一次存在性判定。
        if not target.exists():
            return {"status": "unavailable", "reason": "target-missing"}
        _spawn(argv)
    except OSError:
        return {"status": "unavailable", "reason": "spawn-failed"}
    except Exception:  # pragma: no cover - 兜底，不让平台差异冒泡成 500
        return {"status": "unavailable", "reason": "launch-failed"}
    return {"status": "opened", "reason": None}
