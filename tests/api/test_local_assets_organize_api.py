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

"""QIN-545 磁盘整理 API 安全防线与权限测试。

覆盖：
- 系统资源根目录（环境变量根）一律拒绝写入（403 SYSTEM_ROOT_READ_ONLY）
- 只读根目录拒绝写入（403 SOURCE_READ_ONLY）
- 身份认证与角色门禁（401 / 403）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.core import storage

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
VIEWER = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "viewer"}


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "gw-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    auth_db = tmp_path / "gw-auth" / "auth.sqlite3"
    auth_db.parent.mkdir(parents=True, exist_ok=True)
    video_dir = tmp_path / "gw-video"
    video_dir.mkdir(parents=True, exist_ok=True)

    sys_root = tmp_path / "system-assets"
    sys_root.mkdir(parents=True, exist_ok=True)
    sys_sub = sys_root / "sys_folder"
    sys_sub.mkdir(parents=True, exist_ok=True)
    (sys_sub / "sys_file.txt").write_text("system asset", encoding="utf-8")

    user_root = tmp_path / "user-assets"
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "user_file.txt").write_text("user asset", encoding="utf-8")

    readonly_root = tmp_path / "readonly-assets"
    readonly_root.mkdir(parents=True, exist_ok=True)
    (readonly_root / "ro_file.txt").write_text("readonly asset", encoding="utf-8")

    monkeypatch.setenv("GW_AUTH_MODE", "local")
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(auth_db))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(video_dir))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(sys_root))

    allowed_roots_config = {
        "schema_version": 1,
        "revision": 1,
        "roots": [
            {
                "id": "root_user",
                "path": str(user_root),
                "aliases": [],
                "active": True,
            },
            {
                "id": "root_readonly",
                "path": str(readonly_root),
                "aliases": [],
                "active": True,
            },
        ],
        "system_aliases": {},
    }
    (data_dir / "allowed_roots.json").write_text(
        json.dumps(allowed_roots_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 设置只读属性
    storage_settings = {
        "revision": 1,
        "local_library_read_only": {"root_readonly": True},
    }
    (data_dir / "storage_settings.json").write_text(
        json.dumps(storage_settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    storage._ALLOWED_ROOTS_STATE_CACHE = None
    storage._ALLOWED_ROOTS_STATE_PATH_CACHE = None

    client = TestClient(create_app())
    return {
        "client": client,
        "sys_root": sys_root,
        "user_root": user_root,
        "readonly_root": readonly_root,
        "sys_root_id": storage.root_identity_id(sys_root),
        "user_root_id": "root_user",
        "readonly_root_id": "root_readonly",
    }


def test_system_root_read_only_rejection(api_env):
    """系统资源的根目录一律拒绝写入。"""
    client = api_env["client"]
    sys_root = api_env["sys_root"]
    sys_rid = api_env["sys_root_id"]

    # 1. 尝试在系统根下新建子目录
    res_mkdir = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": str(sys_root / "new_dir"), "root_id": sys_rid},
    )
    assert res_mkdir.status_code == 403
    assert res_mkdir.json()["detail"]["code"] == "SYSTEM_ROOT_READ_ONLY"

    # 2. 尝试在系统根下重命名目录
    res_rename = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": str(sys_root / "sys_folder"), "name": "renamed_sys", "root_id": sys_rid},
    )
    assert res_rename.status_code == 403
    assert res_rename.json()["detail"]["code"] == "SYSTEM_ROOT_READ_ONLY"

    # 3. 尝试在系统根下移动目录
    res_move_dir = client.post(
        "/api/local-assets/folders/move",
        headers=AUTH,
        json={"source": str(sys_root / "sys_folder"), "target": str(sys_root / "moved_sys"), "root_id": sys_rid},
    )
    assert res_move_dir.status_code == 403
    assert res_move_dir.json()["detail"]["code"] == "SYSTEM_ROOT_READ_ONLY"

    # 4. 尝试在系统根下移动文件
    res_move_file = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": str(sys_root / "sys_folder" / "sys_file.txt"),
            "target": str(sys_root / "sys_file_moved.txt"),
            "root_id": sys_rid,
            "organize": True,
        },
    )
    assert res_move_file.status_code == 403
    assert res_move_file.json()["detail"]["code"] == "SYSTEM_ROOT_READ_ONLY"

    # 5. 尝试在系统根下删除文件或目录
    res_del = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": str(sys_root / "sys_folder"), "root_id": sys_rid, "organize": True},
    )
    assert res_del.status_code == 403
    assert res_del.json()["detail"]["code"] == "SYSTEM_ROOT_READ_ONLY"


def test_source_read_only_rejection(api_env):
    """配置了只读的素材来源根目录拒绝写入。"""
    client = api_env["client"]
    ro_root = api_env["readonly_root"]
    ro_rid = api_env["readonly_root_id"]

    res_mkdir = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": str(ro_root / "sub"), "root_id": ro_rid},
    )
    assert res_mkdir.status_code == 403
    assert res_mkdir.json()["detail"]["code"] == "SOURCE_READ_ONLY"

    res_move = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": str(ro_root / "ro_file.txt"),
            "target": str(ro_root / "ro_moved.txt"),
            "root_id": ro_rid,
            "organize": True,
        },
    )
    assert res_move.status_code == 403
    assert res_move.json()["detail"]["code"] == "SOURCE_READ_ONLY"


def test_auth_and_permissions(api_env):
    """未登录返回 401，权限不足返回 403。"""
    client = api_env["client"]
    user_root = api_env["user_root"]
    user_rid = api_env["user_root_id"]

    # 1. 匿名访问
    anon_res = client.post(
        "/api/local-assets/folders",
        json={"path": str(user_root / "test"), "root_id": user_rid},
    )
    assert anon_res.status_code == 401

    # 2. 只有查看权限的角色 (viewer)
    viewer_res = client.post(
        "/api/local-assets/folders",
        headers=VIEWER,
        json={"path": str(user_root / "test"), "root_id": user_rid},
    )
    assert viewer_res.status_code == 403
