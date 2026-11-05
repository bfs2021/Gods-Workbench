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

"""QIN-545 我的资源磁盘文件夹整理接口契约测试。

覆盖：
- 新建任意深度子文件夹、重名冲突与撤销
- 文件夹重命名、子树索引增量更新与撤销
- 文件夹移动、整树移动、目录自包含嵌套拦截与撤销
- 文件移动冲突策略（skip / rename / overwrite）与覆盖恢复撤销
- 文件被外部占用时的 423 FILE_LOCKED 报错
- 删除移入应用回收区与撤销恢复
- 路径逃逸（..）与跨根操作（CROSS_ROOT_FORBIDDEN）安全拦截
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_library import organize
from gw.asset_library import repository as asset_lib_repo
from gw.asset_registry import repository as asset_reg_repo
from gw.core import storage

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}


@pytest.fixture
def organize_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "gw-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    auth_db = tmp_path / "gw-auth" / "auth.sqlite3"
    auth_db.parent.mkdir(parents=True, exist_ok=True)
    video_dir = tmp_path / "gw-video"
    video_dir.mkdir(parents=True, exist_ok=True)

    sys_root = tmp_path / "sys-resources"
    sys_root.mkdir(parents=True, exist_ok=True)

    user_root_1 = tmp_path / "user-library-1"
    user_root_1.mkdir(parents=True, exist_ok=True)

    user_root_2 = tmp_path / "user-library-2"
    user_root_2.mkdir(parents=True, exist_ok=True)

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
                "id": "root_user_1",
                "path": str(user_root_1),
                "aliases": [],
                "active": True,
            },
            {
                "id": "root_user_2",
                "path": str(user_root_2),
                "aliases": [],
                "active": True,
            },
        ],
        "system_aliases": {},
    }
    (data_dir / "allowed_roots.json").write_text(
        json.dumps(allowed_roots_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    storage._ALLOWED_ROOTS_STATE_CACHE = None
    storage._ALLOWED_ROOTS_STATE_PATH_CACHE = None

    client = TestClient(create_app())
    return {
        "client": client,
        "data_dir": data_dir,
        "sys_root": sys_root,
        "user_root_1": user_root_1,
        "user_root_2": user_root_2,
        "root_id_1": "root_user_1",
        "root_id_2": "root_user_2",
    }


def test_create_arbitrary_depth_folders(organize_env):
    client = organize_env["client"]
    root = organize_env["user_root_1"]
    rid = organize_env["root_id_1"]

    # 1. 创建任意深度的子文件夹
    res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "level1/level2/level3", "root_id": rid},
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["created"] is True
    assert (root / "level1" / "level2" / "level3").is_dir()
    assert data["undo"]["action"] == "create_folder"

    # 2. 重复创建同一目录返回 409 TARGET_EXISTS
    dup = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "level1/level2/level3", "root_id": rid},
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "TARGET_EXISTS"

    # 3. 撤销创建
    undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": data["undo"]},
    )
    assert undo_res.status_code == 200, undo_res.text
    assert undo_res.json()["undone"] is True
    assert not (root / "level1" / "level2" / "level3").exists()


def test_rename_folder_and_incremental_index_update(organize_env):
    client = organize_env["client"]
    root = organize_env["user_root_1"]
    rid = organize_env["root_id_1"]

    # 初始化测试文件夹与内部文件
    folder = root / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    pic1 = folder / "img1.png"
    pic1.write_bytes(b"test-image-1")
    sub = folder / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    pic2 = sub / "img2.png"
    pic2.write_bytes(b"test-image-2")

    # 登记到本地素材库与全局资产注册表
    asset_lib_repo.index_local_files([str(pic1), str(pic2)], "image")
    imported = asset_reg_repo.import_assets(
        [
            {
                "name": "img1.png",
                "kind": "image",
            },
            {
                "name": "img2.png",
                "kind": "image",
            },
        ],
        source_kind="local",
        expected_version=None,
    )
    aid1, aid2 = imported["asset_ids"][0], imported["asset_ids"][1]
    asset_reg_repo.update_asset(
        aid1,
        {
            "display_path": "photos/img1.png",
            "canonical_relpath": "photos/img1.png",
            "root_id": rid,
            "storage_root_id": rid,
        },
    )
    asset_reg_repo.update_asset(
        aid2,
        {
            "display_path": "photos/sub/img2.png",
            "canonical_relpath": "photos/sub/img2.png",
            "root_id": rid,
            "storage_root_id": rid,
        },
    )

    # 重命名文件夹
    res = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "photos", "name": "gallery", "root_id": rid},
    )
    assert res.status_code == 200, res.text
    assert (root / "gallery" / "img1.png").is_file()
    assert (root / "gallery" / "sub" / "img2.png").is_file()
    assert not (root / "photos").exists()

    # 验证索引增量更新（无需全量扫描，路径已自动同步为 gallery/...）
    local_entries = asset_lib_repo._local_state().read().get("entries", {})
    disp_paths = [e["display_path"] for e in local_entries.values()]
    assert "gallery/img1.png" in disp_paths
    assert "gallery/sub/img2.png" in disp_paths

    reg_assets = asset_reg_repo.state().read().get("assets", {})
    reg_paths = [a["display_path"] for a in reg_assets.values()]
    assert "gallery/img1.png" in reg_paths
    assert "gallery/sub/img2.png" in reg_paths

    # 撤销重命名
    undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": res.json()["undo"]},
    )
    assert undo_res.status_code == 200, undo_res.text
    assert (root / "photos" / "img1.png").is_file()
    assert not (root / "gallery").exists()

    # 撤销后索引增量恢复
    disp_paths_reverted = [
        e["display_path"] for e in asset_lib_repo._local_state().read().get("entries", {}).values()
    ]
    assert "photos/img1.png" in disp_paths_reverted
    assert "photos/sub/img2.png" in disp_paths_reverted


def test_move_folder_and_self_containment_rejection(organize_env):
    client = organize_env["client"]
    root = organize_env["user_root_1"]
    rid = organize_env["root_id_1"]

    # 初始化测试文件夹
    src_dir = root / "folder_a"
    src_dir.mkdir(parents=True, exist_ok=True)
    file_in_dir = src_dir / "note.txt"
    file_in_dir.write_text("hello", encoding="utf-8")

    # 1. 尝试将文件夹移动到其自身的子目录中，必须被拦截
    self_move = client.post(
        "/api/local-assets/folders/move",
        headers=AUTH,
        json={"source": "folder_a", "target": "folder_a/sub", "root_id": rid},
    )
    assert self_move.status_code == 400
    assert self_move.json()["detail"]["code"] == "INVALID_MOVE_DESTINATION"

    # 2. 正常移动到同根其他位置
    move_res = client.post(
        "/api/local-assets/folders/move",
        headers=AUTH,
        json={"source": "folder_a", "target": "archived/folder_a", "root_id": rid},
    )
    assert move_res.status_code == 200, move_res.text
    assert (root / "archived" / "folder_a" / "note.txt").is_file()
    assert not (root / "folder_a").exists()

    # 3. 撤销移动
    undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": move_res.json()["undo"]},
    )
    assert undo_res.status_code == 200, undo_res.text
    assert (root / "folder_a" / "note.txt").is_file()
    assert not (root / "archived" / "folder_a").exists()


def test_move_file_conflict_resolution_and_undo(organize_env):
    client = organize_env["client"]
    root = organize_env["user_root_1"]
    rid = organize_env["root_id_1"]

    src_file = root / "source.txt"
    src_file.write_text("source content", encoding="utf-8")
    target_file = root / "dest.txt"
    target_file.write_text("existing dest content", encoding="utf-8")

    # 1. 默认未指定 conflict_resolution 时遇到重名返回 409
    res_conflict = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={"source": "source.txt", "target": "dest.txt", "organize": True, "root_id": rid},
    )
    assert res_conflict.status_code == 409
    assert res_conflict.json()["detail"]["code"] == "TARGET_EXISTS"

    # 2. conflict_resolution="skip"：跳过，双方保持原状
    res_skip = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "source.txt",
            "target": "dest.txt",
            "conflict_resolution": "skip",
            "root_id": rid,
        },
    )
    assert res_skip.status_code == 200
    assert res_skip.json()["skipped"] is True
    assert src_file.exists()
    assert target_file.read_text(encoding="utf-8") == "existing dest content"

    # 3. conflict_resolution="rename"：自动重命名为 dest (1).txt
    res_rename = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "source.txt",
            "target": "dest.txt",
            "conflict_resolution": "rename",
            "root_id": rid,
        },
    )
    assert res_rename.status_code == 200
    renamed_target = root / "dest (1).txt"
    assert renamed_target.exists()
    assert renamed_target.read_text(encoding="utf-8") == "source content"
    assert not src_file.exists()

    # 4. conflict_resolution="overwrite"：覆盖并保留暂存以支持撤销
    # 重新生成 source.txt
    src_file.write_text("brand new content", encoding="utf-8")
    res_overwrite = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "source.txt",
            "target": "dest.txt",
            "conflict_resolution": "overwrite",
            "root_id": rid,
        },
    )
    assert res_overwrite.status_code == 200
    assert target_file.read_text(encoding="utf-8") == "brand new content"
    assert not src_file.exists()

    # 撤销覆盖移动：恢复被覆盖的 dest.txt 与 source.txt！
    undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": res_overwrite.json()["undo"]},
    )
    assert undo_res.status_code == 200, undo_res.text
    assert src_file.read_text(encoding="utf-8") == "brand new content"
    assert target_file.read_text(encoding="utf-8") == "existing dest content"


def test_file_locked_error_handling(organize_env):
    client = organize_env["client"]
    root = organize_env["user_root_1"]
    rid = organize_env["root_id_1"]

    file_to_move = root / "locked_file.bin"
    file_to_move.write_bytes(b"locked-content")

    # 模拟外部进程锁死文件产生的 Windows sharing violation 异常 (winerror=32)
    def raise_locked(*args, **kwargs):
        err = PermissionError("The process cannot access the file because it is being used by another process.")
        err.winerror = 32
        raise err

    with patch("shutil.move", side_effect=raise_locked):
        res = client.post(
            "/api/local-assets/move",
            headers=AUTH,
            json={
                "source": "locked_file.bin",
                "target": "target.bin",
                "organize": True,
                "root_id": rid,
            },
        )
        assert res.status_code == 423
        assert res.json()["detail"]["code"] == "FILE_LOCKED"


def test_delete_to_recycler_and_undo(organize_env):
    client = organize_env["client"]
    root = organize_env["user_root_1"]
    rid = organize_env["root_id_1"]

    # 准备目录与文件
    to_delete_dir = root / "obsolete_project"
    to_delete_dir.mkdir(parents=True, exist_ok=True)
    doc = to_delete_dir / "draft.txt"
    doc.write_text("obsolete draft", encoding="utf-8")

    # 登记到索引
    asset_lib_repo.index_local_files([str(doc)], "document")

    # 删除进回收站（非永久删除）
    del_res = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": "obsolete_project", "root_id": rid, "organize": True},
    )
    assert del_res.status_code == 200, del_res.text
    assert del_res.json()["deleted"] is True
    assert not to_delete_dir.exists()

    # 检查回收暂存区（客户端脱敏，服务端落盘内部账本）
    undo_info = del_res.json()["undo"]
    assert "stashed_file" not in undo_info
    assert "abs_target" not in undo_info
    undo_file = organize._undo_records_dir() / f"{undo_info['undo_id']}.json"
    server_record = json.loads(undo_file.read_text(encoding="utf-8"))
    stashed_path = Path(server_record["stashed_file"]["stashed_path"])
    assert stashed_path.exists()

    # 索引中该条目已被增量移除
    disp_paths_after_del = [
        e["display_path"] for e in asset_lib_repo._local_state().read().get("entries", {}).values()
    ]
    assert "obsolete_project/draft.txt" not in disp_paths_after_del

    # 撤销删除操作
    undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": undo_info},
    )
    assert undo_res.status_code == 200, undo_res.text
    assert to_delete_dir.is_dir()
    assert doc.read_text(encoding="utf-8") == "obsolete draft"

    # 撤销后索引自动恢复
    disp_paths_restored = [
        e["display_path"] for e in asset_lib_repo._local_state().read().get("entries", {}).values()
    ]
    assert "obsolete_project/draft.txt" in disp_paths_restored


def test_security_path_escape_and_cross_root_forbidden(organize_env):
    client = organize_env["client"]
    root1 = organize_env["user_root_1"]
    root2 = organize_env["user_root_2"]
    rid1 = organize_env["root_id_1"]
    rid2 = organize_env["root_id_2"]

    file1 = root1 / "f1.txt"
    file1.write_text("root1 file", encoding="utf-8")

    # 1. 尝试使用 .. 相对路径逃逸
    escape_res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "../../../escaped_folder", "root_id": rid1},
    )
    assert escape_res.status_code == 403
    assert escape_res.json()["detail"]["code"] == "PATH_OUTSIDE_ALLOWED_ROOTS"

    # 2. 尝试跨根移动文件夹或文件
    cross_move = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "f1.txt",
            "target": str(root2 / "f1.txt"),
            "source_root_id": rid1,
            "target_root_id": rid2,
            "organize": True,
        },
    )
    assert cross_move.status_code == 403
    assert cross_move.json()["detail"]["code"] == "CROSS_ROOT_FORBIDDEN"


def test_security_cannot_delete_or_mutate_mount_root(organize_env):
    """验证挂载根目录本身禁止删除、重命名或整根移动。"""
    client = organize_env["client"]
    root1 = organize_env["user_root_1"]
    rid1 = organize_env["root_id_1"]

    # 1. 尝试删除根目录本身（path="."）
    del_dot = client.post(
        "/api/local-assets/folders/delete",
        headers=AUTH,
        json={"path": ".", "root_id": rid1},
    )
    assert del_dot.status_code == 400
    assert del_dot.json()["detail"]["code"] == "CANNOT_DELETE_ROOT"
    assert root1.exists()

    # 2. 尝试传入根目录绝对路径删除
    del_abs = client.post(
        "/api/local-assets/folders/delete",
        headers=AUTH,
        json={"path": str(root1), "root_id": rid1},
    )
    assert del_abs.status_code == 400
    assert del_abs.json()["detail"]["code"] == "CANNOT_DELETE_ROOT"
    assert root1.exists()

    # 3. 尝试重命名挂载根本身
    rename_root = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": ".", "name": "new_root_name", "root_id": rid1},
    )
    assert rename_root.status_code == 400
    assert rename_root.json()["detail"]["code"] == "CANNOT_RENAME_ROOT"

    # 4. 尝试移动挂载根本身
    move_root = client.post(
        "/api/local-assets/folders/move",
        headers=AUTH,
        json={"source": ".", "target": "some_folder", "root_id": rid1},
    )
    assert move_root.status_code == 400
    assert move_root.json()["detail"]["code"] == "CANNOT_MOVE_ROOT"


def test_security_undo_forged_payload_defense(organize_env):
    """验证撤销接口对伪造凭据、伪造路径及 fail-closed 机制的安全防御。"""
    client = organize_env["client"]
    root1 = organize_env["user_root_1"]
    rid1 = organize_env["root_id_1"]

    # 1. 缺少 undo_id 的伪造请求必须被 400 拦截
    res_no_id = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": {"action": "delete", "root_id": rid1}},
    )
    assert res_no_id.status_code == 400
    assert res_no_id.json()["detail"]["code"] == "INVALID_REQUEST"

    # 2. 伪造不存在的 undo_id 必须被 404 拦截
    res_fake_id = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": {"undo_id": "undo_non_existent_token"}},
    )
    assert res_fake_id.status_code == 404
    assert res_fake_id.json()["detail"]["code"] == "UNDO_NOT_FOUND"

    # 3. 验证 create_folder 撤销防篡改与防删已有数据（审核官重点审项）
    # 创建一个已有目录并在其中存放重要数据
    existing_dir = root1 / "important_data"
    existing_dir.mkdir(parents=True, exist_ok=True)
    (existing_dir / "secret.doc").write_text("critical", encoding="utf-8")

    # 创建一个空的新文件夹，得到合法 undo_id
    create_res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "temp_dir", "root_id": rid1},
    )
    assert create_res.status_code == 201
    valid_undo = create_res.json()["undo"]

    # 攻击者尝试篡改 target 为已有重要目录，但服务端严格以落盘权威记录为准
    tampered_undo = dict(valid_undo)
    tampered_undo["target"] = "important_data"
    client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": tampered_undo},
    )
    # 重要已有目录完好无损，被删除的是真正由该操作创建的 temp_dir
    assert existing_dir.exists()
    assert (existing_dir / "secret.doc").is_file()
    assert not (root1 / "temp_dir").exists()

    # 4. 再次撤销同一个已消费的 undo_id 必须被 404 拦截（防重放）
    replay_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": valid_undo},
    )
    assert replay_res.status_code == 404
    assert replay_res.json()["detail"]["code"] == "UNDO_NOT_FOUND"

    # 5. 回收站 manifest 缺失时 fail-closed 拒绝恢复（审核官重点审项）
    # 新建并删除一个文件产生回收暂存
    file_to_del = root1 / "doc_del.txt"
    file_to_del.write_text("del_me", encoding="utf-8")
    del_res = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": "doc_del.txt", "root_id": rid1, "organize": True},
    )
    del_undo = del_res.json()["undo"]
    # 验证客户端响应零泄露绝对路径
    assert "abs_target" not in del_undo
    assert "stashed_file" not in del_undo

    # 服务端读取内部权威账本定位 staging 目录进行破坏性测试
    undo_file = organize._undo_records_dir() / f"{del_undo['undo_id']}.json"
    server_record = json.loads(undo_file.read_text(encoding="utf-8"))
    del_staging = Path(server_record["stashed_file"]["staging_dir"])

    # 故意破坏或移除 manifest.json，验证 fail-closed，绝不回退客户端输入
    manifest_file = del_staging / "manifest.json"
    manifest_file.unlink()

    fail_closed_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": del_undo},
    )
    assert fail_closed_res.status_code == 404
    assert fail_closed_res.json()["detail"]["code"] == "RECYCLE_MANIFEST_NOT_FOUND"


def test_naming_and_path_validation(organize_env):
    r"""测试相国拍板口径的文件夹命名与路径长度校验。

    - 允许 Unicode（含中文）；
    - 拦截 Windows 保留名（CON、PRN、AUX、NUL、COM1–COM9、LPT1–LPT9，带扩展名也算）；
    - 拦截非法字符 <>:"/\|?* 及控制字符；
    - 拦截以点或空格结尾的名字；
    - 拦截超长名字（> 255 字符）或超长路径（> 260 字符）。
    """
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]

    # 1. 允许 Unicode（含中文）
    chinese_res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "剧本素材/第一场_室内", "root_id": rid1},
    )
    assert chinese_res.status_code == 201, chinese_res.text
    assert chinese_res.json()["name"] == "第一场_室内"

    rename_chinese = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "剧本素材/第一场_室内", "name": "分镜_01_特写镜头", "root_id": rid1},
    )
    assert rename_chinese.status_code == 200, rename_chinese.text

    # 2. 拦截 Windows 保留名（带扩展名也算，不区分大小写）
    reserved_cases = ["CON", "con.txt", "prn.dir", "AUX", "aux.tar.gz", "nul", "COM1", "com9.log", "LPT3", "lpt8.dat"]
    for name in reserved_cases:
        res = client.post(
            "/api/local-assets/folders",
            headers=AUTH,
            json={"name": name, "root_id": rid1},
        )
        assert res.status_code == 400, f"Expected 400 for reserved name {name}, got {res.status_code}"
        assert res.json()["detail"]["code"] == "RESERVED_NAME_FORBIDDEN"

        # 重命名同样拦截
        ren_res = client.patch(
            "/api/local-assets/folders",
            headers=AUTH,
            json={"path": "剧本素材", "name": name, "root_id": rid1},
        )
        assert ren_res.status_code == 400
        assert ren_res.json()["detail"]["code"] == "RESERVED_NAME_FORBIDDEN"

    # 3. 拦截非法字符 <>:"/\|?* 及控制字符
    illegal_cases = ['bad<name>', 'bad>name', 'bad:name', 'bad"name', 'bad|name', 'bad?name', 'bad*name', 'bad\tname', 'bad\nname']
    for name in illegal_cases:
        res = client.post(
            "/api/local-assets/folders",
            headers=AUTH,
            json={"name": name, "root_id": rid1},
        )
        assert res.status_code == 400, f"Expected 400 for illegal char {name}, got {res.status_code}"
        assert res.json()["detail"]["code"] == "INVALID_CHARACTERS"

    # 4. 拦截以点或空格结尾的名字
    end_cases = ["folder.", "folder..", "folder ", "folder   "]
    for name in end_cases:
        res = client.post(
            "/api/local-assets/folders",
            headers=AUTH,
            json={"name": name, "root_id": rid1},
        )
        assert res.status_code == 400, f"Expected 400 for end dot/space {name}, got {res.status_code}"
        assert res.json()["detail"]["code"] == "INVALID_NAME_END"

    # 5. 拦截超长名字（> 255 字符）
    long_name = "a" * 256
    res_long = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"name": long_name, "root_id": rid1},
    )
    assert res_long.status_code == 400
    assert res_long.json()["detail"]["code"] == "NAME_TOO_LONG"


def test_storage_internal_isolation_from_download_output(organize_env):
    """验证回收区和撤销账本存放在系统内部目录，绝对无法通过 download-output API 被只读或普通用户下载。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    root1 = organize_env["user_root_1"]

    # 新建并删除一个私密文件
    secret_file = root1 / "confidential.txt"
    secret_file.write_text("classified-information", encoding="utf-8")
    del_res = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": "confidential.txt", "root_id": rid1, "organize": True},
    )
    assert del_res.status_code == 200
    undo = del_res.json()["undo"]

    # 从服务端内部账本读取物理存储路径（确认处于 system_internal 且非 output 命名空间）
    undo_file = organize._undo_records_dir() / f"{undo['undo_id']}.json"
    server_record = json.loads(undo_file.read_text(encoding="utf-8"))
    stashed_path = Path(server_record["stashed_file"]["stashed_path"])

    assert "system_internal" in str(stashed_path)
    assert "registry_uploads" not in str(stashed_path)

    # 尝试使用只读用户通过 /api/download-output 请求该内部文件，必须被拦截拒绝
    read_only_auth = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "readonly"}
    dl_res = client.get(
        "/api/download-output",
        headers=read_only_auth,
        params={"path": f"system_internal/recycler/organize/{stashed_path.name}"},
    )
    assert dl_res.status_code in (400, 403, 404)


def test_move_file_overwrite_index_recovery_on_undo(organize_env):
    """验证文件移动覆盖已索引文件并在撤销后，被覆盖文件的物理内容与索引条目均完整恢复。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    root1 = organize_env["user_root_1"]

    # 准备文件 A 和文件 B
    file_a = root1 / "file_a.png"
    file_a.write_bytes(b"content-of-a")
    file_b = root1 / "file_b.png"
    file_b.write_bytes(b"content-of-b")

    # 登记到本地索引与注册表
    asset_lib_repo.index_local_files([str(file_a), str(file_b)], "image")
    imported = asset_reg_repo.import_assets(
        [{"name": "file_a.png", "kind": "image"}, {"name": "file_b.png", "kind": "image"}],
        source_kind="local",
        expected_version=None,
    )
    aid_b = imported["asset_ids"][1]
    asset_reg_repo.update_asset(
        aid_b,
        {"display_path": "file_b.png", "canonical_relpath": "file_b.png", "root_id": rid1, "storage_root_id": rid1},
    )

    # 确认移动前 B 的索引存在
    disp_paths_before = [
        e["display_path"] for e in asset_lib_repo._local_state().read().get("entries", {}).values()
    ]
    assert "file_b.png" in disp_paths_before

    # 执行文件移动并指定覆盖（overwrite）
    move_res = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "file_a.png",
            "target": "file_b.png",
            "conflict_resolution": "overwrite",
            "source_root_id": rid1,
            "target_root_id": rid1,
            "organize": True,
        },
    )
    assert move_res.status_code == 200
    move_undo = move_res.json()["undo"]

    # 此时目标位置的内容变为 A 的内容
    assert (root1 / "file_b.png").read_bytes() == b"content-of-a"
    assert not file_a.exists()

    # 执行撤销操作
    undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": move_undo},
    )
    assert undo_res.status_code == 200, undo_res.text

    # 验证物理文件：A 回到原位，B 的原始内容被恢复
    assert file_a.read_bytes() == b"content-of-a"
    assert file_b.read_bytes() == b"content-of-b"

    # 验证索引条目：B 的索引条目被完整恢复
    disp_paths_after = [
        e["display_path"] for e in asset_lib_repo._local_state().read().get("entries", {}).values()
    ]
    assert "file_b.png" in disp_paths_after


def test_corrupted_manifest_fail_closed(organize_env):
    """验证 manifest 损坏或非字典格式时，Fail-Closed 返回 500 RECYCLE_MANIFEST_CORRUPTED。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    root1 = organize_env["user_root_1"]

    target = root1 / "corrupt_test.txt"
    target.write_text("data", encoding="utf-8")
    del_res = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": "corrupt_test.txt", "root_id": rid1, "organize": True},
    )
    undo = del_res.json()["undo"]

    undo_file = organize._undo_records_dir() / f"{undo['undo_id']}.json"
    server_record = json.loads(undo_file.read_text(encoding="utf-8"))
    del_staging = Path(server_record["stashed_file"]["staging_dir"])

    # 写入非字典或损坏 JSON 的 manifest
    (del_staging / "manifest.json").write_text('"corrupted-non-dict-manifest"', encoding="utf-8")

    res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": undo},
    )
    assert res.status_code == 500
    assert res.json()["detail"]["code"] == "RECYCLE_MANIFEST_CORRUPTED"


def test_cross_user_undo_permission_check(organize_env):
    """验证撤销绑定操作者：不同用户撤销被 403 拦截，管理员可跨用户撤销。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]

    user_a_auth = {"Authorization": "Bearer token-user-a", "X-User-Role": "editor"}
    user_b_auth = {"Authorization": "Bearer token-user-b", "X-User-Role": "editor"}
    admin_auth = {"Authorization": "Bearer token-admin", "X-User-Role": "admin"}

    # 用户 A 新建文件夹
    create_res = client.post(
        "/api/local-assets/folders",
        headers=user_a_auth,
        json={"name": "user_a_folder", "root_id": rid1},
    )
    assert create_res.status_code == 201
    undo = create_res.json()["undo"]

    # 用户 B 尝试撤销，必须被 403 PERMISSION_DENIED 拦截
    undo_b_res = client.post(
        "/api/local-assets/undo",
        headers=user_b_auth,
        json={"undo": undo},
    )
    assert undo_b_res.status_code == 403
    assert undo_b_res.json()["detail"]["code"] == "PERMISSION_DENIED"

    # 管理员执行撤销，成功放行
    admin_undo_res = client.post(
        "/api/local-assets/undo",
        headers=admin_auth,
        json={"undo": undo},
    )
    assert admin_undo_res.status_code == 200
    assert admin_undo_res.json()["undone"] is True


def test_delete_manifest_json_file_and_folder_can_undo(organize_env):
    """验证删除名为 manifest.json 的文件或文件夹，载荷与清单隔离，可正确撤销，不报 500。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    root1 = organize_env["user_root_1"]

    # 1. 测试名为 manifest.json 的文件
    manifest_file = root1 / "manifest.json"
    manifest_file.write_text("critical_user_manifest_content", encoding="utf-8")

    del_file_res = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": "manifest.json", "root_id": rid1, "organize": True},
    )
    assert del_file_res.status_code == 200
    assert not manifest_file.exists()
    file_undo = del_file_res.json()["undo"]

    # 撤销删除文件
    undo_file_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": file_undo},
    )
    assert undo_file_res.status_code == 200
    assert manifest_file.exists()
    assert manifest_file.read_text(encoding="utf-8") == "critical_user_manifest_content"

    manifest_file.unlink()

    # 2. 测试名为 manifest.json 的文件夹
    manifest_dir = root1 / "manifest.json"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    child_file = manifest_dir / "child.txt"
    child_file.write_text("inner_content", encoding="utf-8")

    del_dir_res = client.post(
        "/api/local-assets/folders/delete",
        headers=AUTH,
        json={"path": "manifest.json", "root_id": rid1},
    )
    assert del_dir_res.status_code == 200
    assert not manifest_dir.exists()
    dir_undo = del_dir_res.json()["undo"]

    # 撤销删除文件夹
    undo_dir_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": dir_undo},
    )
    assert undo_dir_res.status_code == 200
    assert manifest_dir.exists()
    assert manifest_dir.is_dir()
    assert child_file.exists()
    assert child_file.read_text(encoding="utf-8") == "inner_content"


def test_name_length_utf8_bytes_validation(organize_env):
    """验证按 UTF-8 字节计算名字长度：86 个汉字（258 字节 > 255）报 400 NAME_TOO_LONG，不报 500。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]

    # 86 个中文字符，字符数 86，但 UTF-8 字节数为 86 * 3 = 258 字节
    long_chinese_name = "测" * 86
    res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"name": long_chinese_name, "root_id": rid1},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "NAME_TOO_LONG"

    # 80 个中文字符，80 * 3 = 240 字节 <= 255 字节，正常创建与重命名
    valid_chinese_name = "测" * 80
    ok_res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"name": valid_chinese_name, "root_id": rid1},
    )
    assert ok_res.status_code == 201

    # 重命名超长字节同样拦截
    rename_res = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": valid_chinese_name, "name": long_chinese_name, "root_id": rid1},
    )
    assert rename_res.status_code == 400
    assert rename_res.json()["detail"]["code"] == "NAME_TOO_LONG"


def test_expected_version_cas_optimistic_concurrency(organize_env):
    """验证所有整理接口支持 expected_version CAS 乐观锁，不匹配时返回 409 VERSION_CONFLICT。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]

    # 查询初始版本
    ver_res = client.get("/api/local-assets/organize/version", headers=AUTH)
    assert ver_res.status_code == 200
    v0 = ver_res.json()["version"]

    # 1. 错误版本创建文件夹被 409 拦截
    bad_res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"name": "cas_folder", "root_id": rid1, "expected_version": v0 + 999},
    )
    assert bad_res.status_code == 409
    assert bad_res.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 2. 正确版本创建文件夹成功，版本递增
    ok_res = client.post(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"name": "cas_folder", "root_id": rid1, "expected_version": v0},
    )
    assert ok_res.status_code == 201
    v1 = ok_res.json()["version"]
    assert v1 == v0 + 1

    # 3. 错误版本重命名被 409 拦截
    bad_rename = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "cas_folder", "name": "cas_renamed", "root_id": rid1, "expected_version": v0},
    )
    assert bad_rename.status_code == 409
    assert bad_rename.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 4. 正确版本重命名成功，版本再递增
    ok_rename = client.patch(
        "/api/local-assets/folders",
        headers=AUTH,
        json={"path": "cas_folder", "name": "cas_renamed", "root_id": rid1, "expected_version": v1},
    )
    assert ok_rename.status_code == 200
    v2 = ok_rename.json()["version"]
    assert v2 == v1 + 1

    # 5. 错误版本删除被 409 拦截
    bad_del = client.post(
        "/api/local-assets/folders/delete",
        headers=AUTH,
        json={"path": "cas_renamed", "root_id": rid1, "expected_version": v1},
    )
    assert bad_del.status_code == 409
    assert bad_del.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 6. 正确版本删除成功
    ok_del = client.post(
        "/api/local-assets/folders/delete",
        headers=AUTH,
        json={"path": "cas_renamed", "root_id": rid1, "expected_version": v2},
    )
    assert ok_del.status_code == 200
    v3 = ok_del.json()["version"]
    assert v3 == v2 + 1
    undo = ok_del.json()["undo"]

    # 7. 错误版本撤销被 409 拦截
    bad_undo = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": undo, "expected_version": v2},
    )
    assert bad_undo.status_code == 409
    assert bad_undo.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 8. 正确版本撤销成功
    ok_undo = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": undo, "expected_version": v3},
    )
    assert ok_undo.status_code == 200
    v4 = ok_undo.json()["version"]
    assert v4 == v3 + 1


def test_move_file_self_move_rejection(organize_env):
    """验证 move_file 拒绝自移动，即使指定 rename 策略也不静默改名为 x (1).ext。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    root1 = organize_env["user_root_1"]

    f = root1 / "stay_here.txt"
    f.write_text("stay content", encoding="utf-8")

    # 1. 相同路径自移动（conflict_resolution=rename）
    res_rename = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "stay_here.txt",
            "target": "stay_here.txt",
            "source_root_id": rid1,
            "conflict_resolution": "rename",
        },
    )
    assert res_rename.status_code == 400
    assert res_rename.json()["detail"]["code"] == "INVALID_MOVE_DESTINATION"
    # 验证未生成 stay_here (1).txt
    assert not (root1 / "stay_here (1).txt").exists()

    # 2. 目标为自身所在目录（拼接后等价于自移动）
    sub = root1 / "sub"
    sub.mkdir()
    f_sub = sub / "file_in_sub.txt"
    f_sub.write_text("content", encoding="utf-8")
    res_dir = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "sub/file_in_sub.txt",
            "target": "sub",
            "source_root_id": rid1,
            "conflict_resolution": "rename",
        },
    )
    assert res_dir.status_code == 400
    assert res_dir.json()["detail"]["code"] == "INVALID_MOVE_DESTINATION"


def test_move_file_overwrite_undo_retryable(organize_env):
    """验证 move_file 覆盖撤销若第二步恢复失败，账本保留且支持再次撤销重试。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    root1 = organize_env["user_root_1"]

    file_a = root1 / "file_a.txt"
    file_b = root1 / "file_b.txt"
    file_a.write_text("content-a", encoding="utf-8")
    file_b.write_text("content-b", encoding="utf-8")

    # 移动 A 覆盖 B
    move_res = client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "file_a.txt",
            "target": "file_b.txt",
            "source_root_id": rid1,
            "conflict_resolution": "overwrite",
        },
    )
    assert move_res.status_code == 200
    undo = move_res.json()["undo"]

    # 模拟第一次撤销时，恢复 B 的过程抛出异常（例如通过 mock 使得 _restore_from_recycler 报错一次）
    real_restore = organize._restore_from_recycler
    call_count = {"count": 0}

    def flaky_restore(stash_record, root_id=None):
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise RuntimeError("Simulated transient error during B restore")
        return real_restore(stash_record, root_id=root_id)

    with patch("gw.asset_library.organize._restore_from_recycler", side_effect=flaky_restore):
        first_undo_res = client.post(
            "/api/local-assets/undo",
            headers=AUTH,
            json={"undo": undo},
        )
        assert first_undo_res.status_code == 500
        assert first_undo_res.json()["detail"]["code"] == "UNDO_FAILED"

    # 验证账本文件依然完好保留（没有被 unlink）
    undo_file = organize._undo_records_dir() / f"{undo['undo_id']}.json"
    assert undo_file.exists()

    # 第二次调用撤销重试，成功完成恢复
    second_undo_res = client.post(
        "/api/local-assets/undo",
        headers=AUTH,
        json={"undo": undo},
    )
    assert second_undo_res.status_code == 200
    assert second_undo_res.json()["undone"] is True
    # 验证 file_a 和 file_b 均已完全恢复且内容正确
    assert file_a.exists() and file_a.read_text(encoding="utf-8") == "content-a"
    assert file_b.exists() and file_b.read_text(encoding="utf-8") == "content-b"
    # 账本已被安全销毁
    assert not undo_file.exists()


def test_multi_root_missing_root_id_isolation_in_registry(organize_env):
    """验证注册表侧未登记 root_id 的条目按真实物理路径匹配，不会在多根环境下误改其他根条目。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    rid2 = organize_env["root_id_2"]
    root1 = organize_env["user_root_1"]
    root2 = organize_env["user_root_2"]

    # 在 root2 创建并登记一个资产，故意缺少 root_id，但真实路径属于 root2
    file_in_root2 = root2 / "photo.jpg"
    file_in_root2.write_text("photo", encoding="utf-8")
    def insert_missing_root(raw: dict[str, Any]) -> None:
        raw.setdefault("assets", {})["asset_in_root2"] = {
            "id": "asset_in_root2",
            "name": "photo.jpg",
            "canonical_relpath": "photo.jpg",
            "display_path": storage.relative_display(file_in_root2),
            "path": str(file_in_root2),
            # 缺少 storage_root_id / root_id
        }
    asset_reg_repo.state().mutate(insert_missing_root)

    # 在 root1 创建同名文件并做移动操作
    file_in_root1 = root1 / "photo.jpg"
    file_in_root1.write_text("photo1", encoding="utf-8")
    folder_in_root1 = root1 / "album"
    folder_in_root1.mkdir()

    client.post(
        "/api/local-assets/move",
        headers=AUTH,
        json={
            "source": "photo.jpg",
            "target": "album/photo.jpg",
            "source_root_id": rid1,
        },
    )

    # 检查 root2 下的条目：绝对没有被误改成 album/photo.jpg
    asset_after = asset_reg_repo.get_asset("asset_in_root2")
    assert asset_after is not None
    assert asset_after.get("canonical_relpath") == "photo.jpg"
    assert "album" not in asset_after.get("canonical_relpath", "")


def test_data_root_path_access_forbidden(organize_env):
    """验证整理接口显式拒绝访问系统内部 data_root() 路径。"""
    client = organize_env["client"]
    rid1 = organize_env["root_id_1"]
    dr_path = str(storage.data_root() / "system_internal" / "organize_version.json")

    res = client.post(
        "/api/local-assets/delete",
        headers=AUTH,
        json={"path": dr_path, "root_id": rid1, "organize": True},
    )
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "PATH_OUTSIDE_ALLOWED_ROOTS"
