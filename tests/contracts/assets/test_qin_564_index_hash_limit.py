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

"""QIN-564：超限文件（>512MiB）不再硬失败哈希索引任务，改走采样哈希诚实降级。

契约覆盖（AC1-AC4）：
- AC1 超限文件不失败任务：登记 sha256 保持缺席（null），任务完成，结果
  如实区分「已哈希 N / 超限跳过 M」；
- AC2 采样哈希口径：sha256(首 4MiB ‖ 尾 4MiB ‖ 十进制长度)，写入独立
  命名空间 sha256_sampled 并打上 sha256_status="sampled" 诚实标记，
  绝不冒充全量内容哈希；
- AC3 素材级查重诚实降级：无全量哈希的超限资产不参与文件查重组，
  降级原因随资产记录可见，不静默当作「无重复」；
- AC4 阈值两侧重扫时哈希字段按当前扫描结果对齐，不残留过期标记。

真机 >512MiB 文件不落盘复现：哈希上限是模块常量 HASH_LIMIT_BYTES，测试
改写为 64KiB 假上限 + 1MiB 真实文件等价覆盖同一分支（扫描期 size 预判与
采样读取逻辑与真机完全一致，只是阈值不同）。
"""

import hashlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app
from gw.asset_registry import index_jobs

BASE = "/api/asset-registry"
AUTH = {"Authorization": "Bearer index-owner", "X-User-Role": "editor"}
ADMIN = {"Authorization": "Bearer index-admin", "X-User-Role": "admin"}
SUGGESTIONS_URL = BASE + "/governance/suggestions"

FAKE_LIMIT = 64 * 1024
FAKE_SAMPLE = 16 * 1024
OVERSIZE_SIZE = 1024 * 1024


@pytest.fixture(autouse=True)
def index_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("GW_AUTH_MODE", "local")
    root = tmp_path / "sources"
    root.mkdir()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "gw-data"))
    # raising=False：修复前的代码没有这两个常量，测试在旧实现上仍能运行，
    # 并复现 413 INDEX_HASH_LIMIT 硬失败缺陷（红），修复后阈值改写生效（绿）。
    monkeypatch.setattr(index_jobs, "HASH_LIMIT_BYTES", FAKE_LIMIT, raising=False)
    monkeypatch.setattr(index_jobs, "HASH_SAMPLE_BYTES", FAKE_SAMPLE, raising=False)
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None
    yield root
    if index_jobs._default:
        index_jobs._default.shutdown()
    index_jobs._default = None


def submit(client, payload=None, key="qin564", kind="reindex", auth=AUTH):
    return client.post(
        BASE + "/" + kind,
        headers={**auth, "Idempotency-Key": key},
        json={"background": True, **(payload or {})},
    )


def read(client, job_id, auth=AUTH):
    return client.get(BASE + "/workspace-jobs/" + job_id, headers=auth)


def wait(client, job_id):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        body = read(client, job_id).json()
        if body["job"]["status"] in index_jobs.TERMINAL:
            return body["job"]
        time.sleep(0.02)
    pytest.fail("后台索引未在期限内终结")


def write_oversize(root, name):
    """写一个超过假上限的真实文件，头尾内容可区分、中间为填充。"""
    head = b"QIN-564-HEAD" + b"H" * (FAKE_SAMPLE - 12)
    tail = b"T" * (FAKE_SAMPLE - 12) + b"QIN-564-TAIL"
    payload = head + b"M" * (OVERSIZE_SIZE - len(head) - len(tail)) + tail
    assert len(payload) == OVERSIZE_SIZE
    (root / name).write_bytes(payload)
    return payload


def expected_sampled_digest(payload):
    """AC2 口径的独立实现：sha256(首 4MiB ‖ 尾 4MiB ‖ 十进制长度)。"""
    digest = hashlib.sha256()
    digest.update(payload[:FAKE_SAMPLE])
    digest.update(payload[-FAKE_SAMPLE:])
    digest.update(str(len(payload)).encode("ascii"))
    return digest.hexdigest()


def assets_by_name(client):
    body = client.get(BASE + "/assets", headers=AUTH).json()
    return {asset["name"]: asset for asset in body["assets"]}


def test_oversize_file_completes_with_sampled_marker_and_counts(index_runtime):
    """AC1/AC2：超限文件任务成功，sha256 缺席、采样哈希与诚实标记登记，
    结果区分已哈希/超限跳过计数。"""
    payload = write_oversize(index_runtime, "huge.bin")
    with TestClient(create_app()) as client:
        created = submit(client, {"hash_files": True})
        assert created.status_code == 202, created.text
        job = wait(client, created.json()["job_id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["indexed"] == 1
        assert job["result"]["hashed"] == 0
        assert job["result"]["hash_skipped_oversize"] == 1

        asset = assets_by_name(client)["huge.bin"]
        assert "sha256" not in asset, "全量内容哈希缺席即诚实降级，不得伪造"
        assert asset["sha256_status"] == "sampled"
        assert asset["sha256_sampled"] == expected_sampled_digest(payload)


def test_mixed_directory_distinguishes_hashed_and_skipped(index_runtime):
    """AC1/AC4：混合目录任务不失败，小文件全量哈希、超限文件采样降级，
    计数如实区分。"""
    payload = write_oversize(index_runtime, "huge.bin")
    (index_runtime / "small.txt").write_text("小文件", encoding="utf-8")
    with TestClient(create_app()) as client:
        job = wait(client, submit(client, {"hash_files": True}, key="mixed").json()["job_id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["hashed"] == 1
        assert job["result"]["hash_skipped_oversize"] == 1

        assets = assets_by_name(client)
        assert len(assets["small.txt"]["sha256"]) == 64
        assert "sha256_status" not in assets["small.txt"]
        assert assets["huge.bin"]["sha256_status"] == "sampled"
        assert assets["huge.bin"]["sha256_sampled"] == expected_sampled_digest(payload)


def test_oversize_assets_not_in_duplicate_groups(index_runtime):
    """AC3：相同内容的超限文件不参与文件查重组（无全量哈希），但资产
    记录带采样标记，降级可解释，不静默当作「无重复」。"""
    payload = write_oversize(index_runtime, "copy-a.bin")
    write_oversize(index_runtime, "copy-b.bin")
    with TestClient(create_app()) as client:
        job = wait(client, submit(client, {"hash_files": True}, key="dedup").json()["job_id"])
        assert job["status"] == "succeeded", job

        listing = client.get(SUGGESTIONS_URL, headers=ADMIN)
        assert listing.status_code == 200
        assert listing.json()["suggestions"]["duplicate_files"] == []

        assets = assets_by_name(client)
        for name in ("copy-a.bin", "copy-b.bin"):
            assert assets[name]["sha256_status"] == "sampled"
            assert assets[name]["sha256_sampled"] == expected_sampled_digest(payload)
            assert "sha256" not in assets[name]


def test_rescan_reconciles_hash_state_when_file_crosses_limit(index_runtime):
    """AC4：文件跨过阈值后重扫，哈希字段按当前扫描结果对齐，不残留
    过期的全量或采样标记。"""
    small = b"S" * (FAKE_LIMIT - 1024)
    (index_runtime / "crossing.bin").write_bytes(small)
    with TestClient(create_app()) as client:
        first = wait(client, submit(client, {"hash_files": True}, key="r1").json()["job_id"])
        assert first["status"] == "succeeded", first
        asset = assets_by_name(client)["crossing.bin"]
        assert len(asset["sha256"]) == 64
        assert "sha256_status" not in asset

        write_oversize(index_runtime, "crossing.bin")
        second = wait(client, submit(client, {"hash_files": True}, key="r2").json()["job_id"])
        assert second["status"] == "succeeded", second
        assert second["result"]["hashed"] == 0
        assert second["result"]["hash_skipped_oversize"] == 1
        asset = assets_by_name(client)["crossing.bin"]
        assert "sha256" not in asset
        assert asset["sha256_status"] == "sampled"

        # 缩回上限以内再扫：采样标记清除，恢复全量哈希。
        (index_runtime / "crossing.bin").write_bytes(small)
        third = wait(client, submit(client, {"hash_files": True}, key="r3").json()["job_id"])
        assert third["status"] == "succeeded", third
        asset = assets_by_name(client)["crossing.bin"]
        assert len(asset["sha256"]) == 64
        assert "sha256_status" not in asset
        assert "sha256_sampled" not in asset


def test_hash_sync_result_also_reports_counts(index_runtime):
    """AC1：index/sync 哈希模式任务结果同样区分已哈希/超限跳过。"""
    write_oversize(index_runtime, "huge.bin")
    (index_runtime / "small.txt").write_text("小文件", encoding="utf-8")
    with TestClient(create_app()) as client:
        job = wait(
            client,
            submit(client, {"hash_files": True}, key="sync", kind="index/sync").json()[
                "job_id"
            ],
        )
        assert job["status"] == "succeeded", job
        assert job["result"]["hashed"] == 1
        assert job["result"]["hash_skipped_oversize"] == 1


def test_sampled_hash_unit_contract(tmp_path, monkeypatch):
    """AC2 单元口径：采样摘要只由首尾 4MiB 与长度决定，中段变化不改变
    采样值（该盲区由 sha256_status="sampled" 如实暴露）。"""
    root = tmp_path / "unit"
    root.mkdir()
    path = root / "unit.bin"
    payload = write_oversize(root, "unit.bin")
    expected = path.lstat()
    scanner = object.__new__(index_jobs.IndexJobs)
    scanner.condition = threading.Condition(threading.RLock())
    monkeypatch.setattr(scanner, "_checkpoint", lambda *_args: {})

    digest = scanner._sampled_hash("qin-564-job", 1, path, expected)
    assert digest == expected_sampled_digest(payload)

    # 只有中段不同的两个大文件采样值相同——这正是降级盲区，登记层必须
    # 以 sha256_status="sampled" 标记，不得当作全量哈希参与查重。
    mutated = bytearray(payload)
    mutated[FAKE_SAMPLE + 8] = ord("X")
    path.write_bytes(bytes(mutated))
    mutated_expected = path.lstat()
    assert scanner._sampled_hash("qin-564-job", 1, path, mutated_expected) == digest
