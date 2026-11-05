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

"""QIN-546「在系统中打开」接口契约测试。

覆盖：认证 / 管理员边界 / 回环边界（不回环时绝不唤起外部进程）/
允许根约束 / 目标不存在 / 正常唤起与降级原因透传，以及平台命令行选择。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api import routes_local_assets as routes
from gw.api.app import create_app
from gw.asset_library import organize, reveal
from gw.core import storage

ADMIN = {"Authorization": "Bearer qin546-admin", "X-User-Role": "admin"}
EDITOR = {"Authorization": "Bearer qin546-editor", "X-User-Role": "editor"}
ENDPOINT = "/api/local-assets/folders/open"


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

    user_root = tmp_path / "user-assets"
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "sub").mkdir(parents=True, exist_ok=True)

    outside_root = tmp_path / "outside-assets"
    outside_root.mkdir(parents=True, exist_ok=True)

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
            {"id": "root_user", "path": str(user_root), "aliases": [], "active": True},
        ],
        "system_aliases": {},
    }
    (data_dir / "allowed_roots.json").write_text(
        json.dumps(allowed_roots_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    storage._ALLOWED_ROOTS_STATE_CACHE = None
    storage._ALLOWED_ROOTS_STATE_PATH_CACHE = None
    organize.init_organize_version_file(1)
    client = TestClient(create_app())
    return {
        "client": client,
        "user_root": user_root,
        "sub": user_root / "sub",
        "outside_root": outside_root,
    }


def _as_loopback(monkeypatch) -> None:
    monkeypatch.setattr(routes, "_is_loopback_request", lambda request: True)


def test_reveal_requires_authentication(api_env):
    with api_env["client"] as client:
        assert client.post(ENDPOINT, json={}).status_code == 401


def test_reveal_rejects_non_admin(api_env, monkeypatch):
    _as_loopback(monkeypatch)
    called = False

    def forbidden_open(target):
        nonlocal called
        called = True
        return {"status": "opened", "reason": None}

    monkeypatch.setattr(reveal, "open_in_file_manager", forbidden_open)
    with api_env["client"] as client:
        response = client.post(ENDPOINT, json={"path": str(api_env["sub"])}, headers=EDITOR)
    assert response.status_code == 403, response.text
    assert called is False


def test_reveal_rejects_non_loopback_without_spawning(api_env, monkeypatch):
    """TestClient 来源不是回环地址：拒绝且绝不启动外部进程。"""
    called = False

    def forbidden_open(target):
        nonlocal called
        called = True
        return {"status": "opened", "reason": None}

    monkeypatch.setattr(reveal, "open_in_file_manager", forbidden_open)
    with api_env["client"] as client:
        response = client.post(ENDPOINT, json={"path": str(api_env["sub"])}, headers=ADMIN)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "REVEAL_NON_LOOPBACK"
    assert called is False


def test_reveal_rejects_path_outside_allowed_roots(api_env, monkeypatch):
    _as_loopback(monkeypatch)
    monkeypatch.setattr(
        reveal, "open_in_file_manager", lambda target: {"status": "opened", "reason": None}
    )
    with api_env["client"] as client:
        response = client.post(
            ENDPOINT, json={"path": str(api_env["outside_root"])}, headers=ADMIN
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "PATH_OUTSIDE_ALLOWED_ROOTS"


def test_reveal_reports_missing_target(api_env, monkeypatch):
    _as_loopback(monkeypatch)
    with api_env["client"] as client:
        response = client.post(
            ENDPOINT,
            json={"path": str(api_env["user_root"] / "ghost")},
            headers=ADMIN,
        )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "NOT_FOUND"


def test_reveal_opens_folder_and_hides_absolute_path(api_env, monkeypatch):
    _as_loopback(monkeypatch)
    seen = []

    def fake_open(target):
        seen.append(Path(target))
        return {"status": "opened", "reason": None}

    monkeypatch.setattr(reveal, "open_in_file_manager", fake_open)
    with api_env["client"] as client:
        response = client.post(
            ENDPOINT,
            json={"path": str(api_env["sub"]), "root_id": "root_user"},
            headers=ADMIN,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "opened"
    assert body["root_id"] == "root_user"
    assert seen and seen[0] == api_env["sub"].resolve()
    # 响应体不得回显绝对路径
    assert str(api_env["sub"]) not in json.dumps(body, ensure_ascii=False)
    assert body["display_path"].endswith("sub")


def test_reveal_surfaces_unavailable_reason(api_env, monkeypatch):
    _as_loopback(monkeypatch)
    monkeypatch.setattr(
        reveal,
        "open_in_file_manager",
        lambda target: {"status": "unavailable", "reason": "spawn-failed"},
    )
    with api_env["client"] as client:
        response = client.post(ENDPOINT, json={"path": str(api_env["sub"])}, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unavailable"
    assert response.json()["reason"] == "spawn-failed"


def test_launcher_argv_is_platform_specific_and_never_uses_shell(tmp_path, monkeypatch):
    target = tmp_path / "folder with space"
    target.mkdir()
    captured = []

    monkeypatch.setattr(reveal, "_spawn", lambda argv: captured.append(list(argv)))
    monkeypatch.setattr(sys, "platform", "win32")
    assert reveal.open_in_file_manager(target)["status"] == "opened"
    assert captured[-1] == ["explorer.exe", str(target)]

    monkeypatch.setattr(sys, "platform", "darwin")
    reveal.open_in_file_manager(target)
    assert captured[-1] == ["open", str(target)]

    monkeypatch.setattr(sys, "platform", "linux")
    reveal.open_in_file_manager(target)
    assert captured[-1] == ["xdg-open", str(target)]


def test_launcher_reports_spawn_failure(tmp_path, monkeypatch):
    target = tmp_path / "folder"
    target.mkdir()

    def boom(argv):
        raise OSError("no launcher")

    monkeypatch.setattr(reveal, "_spawn", boom)
    result = reveal.open_in_file_manager(target)
    assert result == {"status": "unavailable", "reason": "spawn-failed"}
