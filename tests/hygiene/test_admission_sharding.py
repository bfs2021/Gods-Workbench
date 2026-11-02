"""票面分片登记的结构、并集与并行合入回归。

对应 GitHub Issue #426（QIN-465）：

- 结构：一票一文件，文件名即票号，未知字段与重复登记必须显式失败；
- 并集：基础账本 ∪ 分片 → 历史断言依赖的同一视图，历史票条目不受影响；
- 并行：两张各自只写本票分片的票，三路合并**零冲突**（AC2 的直接证据）；
- 兼容：仍按旧格式往基础账本追加条目的在途 PR，合入后依然能被加载（双读窗口）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cleanroom_admission import (
    BASE_METADATA_PATH,
    SHARD_DIR_PATH,
    SHARD_SCHEMA,
    AdmissionMetadataError,
    admission_union_digest,
    canonical_sha256,
    load_admission_metadata,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _copy_base_ledger(root: Path) -> Path:
    """在临时仓库里放一份真实基础账本，并建好分片目录。"""
    source = _REPO_ROOT.joinpath(*BASE_METADATA_PATH.split("/"))
    target = root.joinpath(*BASE_METADATA_PATH.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    root.joinpath(*SHARD_DIR_PATH.split("/")).mkdir(parents=True, exist_ok=True)
    return target


def _write_shard(
    root: Path,
    issue: int,
    files: list[str] | None = None,
    *,
    raw: dict | None = None,
    name: str | None = None,
) -> Path:
    shard_dir = root.joinpath(*SHARD_DIR_PATH.split("/"))
    shard_dir.mkdir(parents=True, exist_ok=True)
    payload = raw if raw is not None else {
        "schema": SHARD_SCHEMA,
        "issue": issue,
        "authorization_basis": f"GitHub Issue #{issue}, 2026-10-28",
        "files": files or [f"gw/example_{issue}.py"],
    }
    path = shard_dir / (name or f"issue-{issue}.json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "hygiene-test",
        "GIT_AUTHOR_EMAIL": "hygiene-test@example.invalid",
        "GIT_COMMITTER_NAME": "hygiene-test",
        "GIT_COMMITTER_EMAIL": "hygiene-test@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, text=True, env=env
    )


def _init_repo(root: Path) -> str:
    assert _git(root, "init", "--quiet").returncode == 0
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "--quiet", "-m", "base").returncode == 0
    result = _git(root, "rev-parse", "HEAD")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_base_ledger_keeps_global_boundaries_and_stays_untouched(repo_root: Path):
    """基础账本只承载全局边界与历史条目；本票登记不落在这里。"""
    base_path = repo_root.joinpath(*BASE_METADATA_PATH.split("/"))
    base = json.loads(base_path.read_text(encoding="utf-8"))
    assert base["schema"] == "gw.cleanroom-admission/v1"
    assert base["release_authorized"] is True
    legacy_issues = {item["issue"] for item in base["authorized_issue_file_sets"]}
    assert 426 not in legacy_issues, "本票登记必须写在分片，不得回填基础账本"
    shard_dir = repo_root.joinpath(*SHARD_DIR_PATH.split("/"))
    assert (shard_dir / "issue-426.json").is_file(), "缺少本票分片"


def test_issue_426_shard_registers_its_own_file_set(repo_root: Path):
    """本票分片逐项登记自身改动文件集，并出现在并集视图中。"""
    metadata = load_admission_metadata(repo_root)
    registrations = [
        item for item in metadata["authorized_issue_file_sets"] if item["issue"] == 426
    ]
    assert len(registrations) == 1
    assert registrations[0]["authorization_basis"] == "GitHub Issue #426, 2026-10-28"
    assert set(registrations[0]["files"]) == {
        ".gitattributes",
        "scripts/fix-hygiene-registry.py",
        "scripts/git-merge-drivers/README.md",
        "scripts/git-merge-drivers/install.py",
        "scripts/git-merge-drivers/install.sh",
        "scripts/git-merge-drivers/merge_common.py",
        "scripts/git-merge-drivers/merge_json_registry.py",
        "scripts/git-merge-drivers/merge_line_list.py",
        "tests/hygiene/cleanroom_admission.py",
        "tests/hygiene/fixtures/admissions/README.md",
        "tests/hygiene/fixtures/admissions/issue-426.json",
        "tests/hygiene/test_admission_sharding.py",
        "tests/hygiene/test_cleanroom_hygiene.py",
        "tests/hygiene/test_merge_drivers.py",
    }
    assert registrations[0]["source"] == f"{SHARD_DIR_PATH}/issue-426.json"


def test_union_keeps_legacy_registrations_and_admitted_files(repo_root: Path):
    """并集不吞掉历史登记：旧票条目与 111 条准入文件全部仍在。"""
    metadata = load_admission_metadata(repo_root)
    issues = {item["issue"] for item in metadata["authorized_issue_file_sets"]}
    assert {333, 334, 335, 336, 338, 345, 364, 366, 367, 426} <= issues
    assert len(metadata["admitted_files"]) == 111
    assert len(metadata["admission_shards"]) >= 1


def test_parallel_ticket_shards_merge_with_zero_registration_conflict(tmp_path: Path):
    """两张并行票各写本票分片 → 三路合并零冲突，双方登记都在（AC2 证据）。

    复现方式与真实并行 PR 同构：同一基线、两个分支、各自只新增
    ``admissions/issue-<票号>.json``，然后 ``git merge``。旧结构下两个分支都会
    追加同一个 ``cleanroom_admission_metadata.json`` 而发生冲突；新结构下两个
    分支改的是不同文件，合并结果直接包含双方登记。
    """
    _copy_base_ledger(tmp_path)
    base_sha = _init_repo(tmp_path)

    assert _git(tmp_path, "checkout", "--quiet", "-b", "ticket-a").returncode == 0
    _write_shard(tmp_path, 901, ["gw/parallel_a.py"])
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "ticket-a shard").returncode == 0

    assert _git(tmp_path, "checkout", "--quiet", "-b", "ticket-b", base_sha).returncode == 0
    _write_shard(tmp_path, 902, ["gw/parallel_b.py"])
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "ticket-b shard").returncode == 0

    assert _git(tmp_path, "checkout", "--quiet", "ticket-a").returncode == 0
    merge = _git(tmp_path, "merge", "--no-edit", "ticket-b")
    assert merge.returncode == 0, merge.stderr
    assert "CONFLICT" not in merge.stdout
    assert "CONFLICT" not in merge.stderr

    conflicting = [
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file() and "<<<<<<<" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not conflicting, f"合并结果残留冲突标记: {conflicting}"

    metadata = load_admission_metadata(tmp_path)
    issues = {item["issue"] for item in metadata["authorized_issue_file_sets"]}
    assert {901, 902} <= issues, "合并后双方分片必须同时生效"


def test_legacy_single_file_registration_still_loads_with_shards(tmp_path: Path):
    """双读窗口：在途 PR 仍往基础账本追加条目时，加载结果同时包含两处登记。"""
    base_path = _copy_base_ledger(tmp_path)
    base = json.loads(base_path.read_text(encoding="utf-8"))
    base["authorized_issue_file_sets"].append(
        {
            "issue": 903,
            "authorization_basis": "GitHub Issue #903, 2026-10-28",
            "files": ["gw/legacy_style.py"],
        }
    )
    base_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_shard(tmp_path, 904, ["gw/shard_style.py"])

    metadata = load_admission_metadata(tmp_path)
    issues = {item["issue"] for item in metadata["authorized_issue_file_sets"]}
    assert {903, 904} <= issues


def test_duplicate_issue_between_base_and_shard_is_rejected(tmp_path: Path):
    """同一票号既在基础账本又在分片中 → 必须报错，不能静默二选一。"""
    _copy_base_ledger(tmp_path)
    _write_shard(tmp_path, 364, ["gw/duplicated.py"])
    with pytest.raises(AdmissionMetadataError, match="重复登记"):
        load_admission_metadata(tmp_path)


def test_duplicate_admitted_path_across_shards_is_rejected(tmp_path: Path):
    """两张票登记同一准入路径 → 必须报错，避免归属含糊。"""
    _copy_base_ledger(tmp_path)
    entry = {
        "path": "docs/contracts/DUPLICATE.yaml",
        "classification": "frozen_interface_contract",
        "authorization_basis": "GitHub Issue #905, 2026-10-28",
        "sha256": "0" * 64,
        "admission": "accepted",
    }
    _write_shard(
        tmp_path,
        905,
        raw={
            "schema": SHARD_SCHEMA,
            "issue": 905,
            "authorization_basis": "GitHub Issue #905, 2026-10-28",
            "files": ["gw/dup_a.py"],
            "admitted_files": [entry],
        },
    )
    _write_shard(
        tmp_path,
        906,
        raw={
            "schema": SHARD_SCHEMA,
            "issue": 906,
            "authorization_basis": "GitHub Issue #906, 2026-10-28",
            "files": ["gw/dup_b.py"],
            "admitted_files": [entry],
        },
    )
    with pytest.raises(AdmissionMetadataError, match="重复登记"):
        load_admission_metadata(tmp_path)


def test_shard_filename_must_match_issue_field(tmp_path: Path):
    """文件名与 issue 字段不一致 → 拒绝，防止登记错票号。"""
    _copy_base_ledger(tmp_path)
    _write_shard(
        tmp_path,
        910,
        raw={
            "schema": SHARD_SCHEMA,
            "issue": 911,
            "authorization_basis": "GitHub Issue #911, 2026-10-28",
            "files": ["gw/mismatch.py"],
        },
    )
    with pytest.raises(AdmissionMetadataError, match="票号"):
        load_admission_metadata(tmp_path)


def test_unknown_shard_field_is_rejected(tmp_path: Path):
    """未知字段（拼写错误）不得退化为「没有登记」。"""
    _copy_base_ledger(tmp_path)
    _write_shard(
        tmp_path,
        912,
        raw={
            "schema": SHARD_SCHEMA,
            "issue": 912,
            "authorization_basis": "GitHub Issue #912, 2026-10-28",
            "files": ["gw/typo.py"],
            "admitted_file": [],
        },
    )
    with pytest.raises(AdmissionMetadataError, match="未知字段"):
        load_admission_metadata(tmp_path)


def test_absolute_path_in_shard_is_rejected(tmp_path: Path):
    """登记路径必须是仓库内相对路径。"""
    _copy_base_ledger(tmp_path)
    _write_shard(tmp_path, 913, ["/etc/passwd"])
    with pytest.raises(AdmissionMetadataError, match="绝对路径"):
        load_admission_metadata(tmp_path)


@pytest.mark.parametrize(
    ("bad_path", "label"),
    [
        ("docs/契约/example.yaml", "非 ASCII（中文目录）"),
        ("docs/contracts/example copy.yaml", "含空格"),
    ],
)
def test_non_ascii_or_space_path_in_shard_is_rejected(
    tmp_path: Path, bad_path: str, label: str
):
    """全局命名规范要求 ASCII 无空格；错误文案须与实际规则一致。"""
    _copy_base_ledger(tmp_path)
    _write_shard(tmp_path, 915, [bad_path])
    with pytest.raises(AdmissionMetadataError, match="必须是 ASCII 且无空格的规范路径") as exc:
        load_admission_metadata(tmp_path)
    assert bad_path in str(exc.value), f"{label} 的场景未在报错中给出原路径"


def test_shard_may_register_path_in_admitted_and_accepted_test_lists(tmp_path: Path):
    """同一路径既可绑哈希也可提供边界分类，这是既有的真实登记形态。"""
    _copy_base_ledger(tmp_path)
    entry = {
        "path": "tests/contracts/example/test_both_lists.py",
        "classification": "curated_test_coverage",
        "authorization_basis": "GitHub Issue #914, 2026-10-28",
        "sha256": "1" * 64,
        "admission": "accepted",
    }
    _write_shard(
        tmp_path,
        914,
        raw={
            "schema": SHARD_SCHEMA,
            "issue": 914,
            "authorization_basis": "GitHub Issue #914, 2026-10-28",
            "files": ["tests/contracts/example/test_both_lists.py"],
            "admitted_files": [entry],
            "accepted_test_files": [
                {
                    "path": entry["path"],
                    "classification": entry["classification"],
                    "authorization_basis": entry["authorization_basis"],
                    "admission": "accepted",
                }
            ],
        },
    )
    metadata = load_admission_metadata(tmp_path)
    assert entry["path"] in {item["path"] for item in metadata["admitted_files"]}
    assert entry["path"] in {item["path"] for item in metadata["accepted_test_files"]}


def test_union_digest_is_order_independent(tmp_path: Path):
    """并集摘要与分片写入顺序无关，可稳定用于审计留痕。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    _copy_base_ledger(first)
    _copy_base_ledger(second)
    _write_shard(first, 921, ["gw/order_a.py"])
    _write_shard(first, 922, ["gw/order_b.py"])
    _write_shard(second, 922, ["gw/order_b.py"])
    _write_shard(second, 921, ["gw/order_a.py"])

    assert admission_union_digest(load_admission_metadata(first)) == admission_union_digest(
        load_admission_metadata(second)
    )


def test_shard_hashes_bind_real_file_content(repo_root: Path):
    """分片里的 sha256 与真实文件一致（逐条绑定，不依赖整文件哈希）。"""
    metadata = load_admission_metadata(repo_root)
    admitted = [entry for entry in metadata["admitted_files"] if entry.get("sha256")]
    assert admitted
    for entry in admitted:
        path = repo_root.joinpath(*entry["path"].split("/"))
        assert path.is_file(), f"准入文件缺失: {entry['path']}"
        assert canonical_sha256(path) == entry["sha256"], f"准入哈希不匹配: {entry['path']}"
