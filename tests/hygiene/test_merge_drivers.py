"""洁净室登记合并驱动的并集、哈希选择与真实 Git 合并回归。

覆盖 GitHub Issue #426 的合并驱动配套能力：

- JSON 驱动：对象列表按键并集、纯字符串列表排序去重、标量冲突取 ours；
- 哈希选择：合并期间**不**按工作树重算哈希，而是取相对 base 变更侧的 ``sha256``
  （两侧都变且互不相同视为致命冲突）；工作树重算只属于 ``--fix``；
- 基础账本钉：驱动写出账本后重写 ``ADMISSION_BASE_METADATA_SHA256``；
- 行驱动：并集、排序、去重并保留顶部注释；
- 真实仓库：配置驱动后 ``git merge`` 零冲突且结果自动并集；
- CI 自检：对当前仓库运行 ``fix-hygiene-registry.py --check``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "scripts" / "git-merge-drivers"
if str(_DRIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_DRIVER_DIR))

import merge_json_registry  # noqa: E402
import merge_line_list  # noqa: E402
from merge_common import BASE_LEDGER_PATH, canonical_sha256, dump_json  # noqa: E402

FIX_COMMAND = "python3 scripts/fix-hygiene-registry.py --fix"


def _entry(path: str, sha: str = "0" * 64, classification: str = "frozen_interface_contract"):
    return {
        "path": path,
        "classification": classification,
        "authorization_basis": "GitHub Issue #900, 2026-10-28",
        "sha256": sha,
        "admission": "accepted",
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json(payload), encoding="utf-8")


def _write_file(root: Path, relative: str, content: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _merge(
    ours: dict, theirs: dict, root: Path, base: dict | None = None
) -> tuple[dict, list[str]]:
    merged, fatal, _warnings = merge_json_registry.merge_payloads(
        ours, theirs, root, base
    )
    assert not fatal, fatal
    return merged, fatal


def _merge_result(
    ours: dict, theirs: dict, root: Path, base: dict | None = None
) -> tuple[dict, list[str], list[str]]:
    return merge_json_registry.merge_payloads(ours, theirs, root, base)


def _ledger_with(entries: list[dict]) -> dict:
    return {"schema": "gw.cleanroom-admission/v1", "admitted_files": entries}


def test_json_driver_unions_lists_and_dedupes_strings(tmp_path: Path):
    for relative in ("gw/a.py", "gw/b.py", "gw/c.py"):
        _write_file(tmp_path, relative, b"placeholder\n")
    _write_file(tmp_path, "docs/contracts/A.yaml", b"a: 1\n")
    _write_file(tmp_path, "docs/contracts/B.yaml", b"b: 2\n")

    ours = {
        "schema": "gw.cleanroom-admission/v1",
        "as_of": "2026-10-28",
        "purpose": "ours",
        "files": ["gw/b.py", "gw/a.py"],
        "authorized_issue_file_sets": [
            {"issue": 900, "authorization_basis": "GitHub Issue #900, 2026-10-28",
             "files": ["gw/b.py", "gw/a.py"]},
            {"issue": 901, "authorization_basis": "GitHub Issue #901, 2026-10-28",
             "files": ["gw/c.py"]},
        ],
        "admitted_files": [
            _entry("docs/contracts/A.yaml", classification="frozen_interface_contract"),
            _entry("docs/contracts/B.yaml"),
        ],
    }
    theirs = {
        "schema": "gw.cleanroom-admission/v1",
        "as_of": "2026-10-29",
        "purpose": "theirs",
        "files": ["gw/c.py", "gw/a.py"],
        "authorized_issue_file_sets": [
            {"issue": 900, "authorization_basis": "GitHub Issue #900, 2026-10-28",
             "files": ["gw/c.py"]},
            {"issue": 902, "authorization_basis": "GitHub Issue #902, 2026-10-28",
             "files": ["gw/a.py"]},
        ],
        "admitted_files": [
            _entry("docs/contracts/A.yaml", classification="golden_fixture"),
            _entry("docs/contracts/C.yaml"),
        ],
    }
    _write_file(tmp_path, "docs/contracts/C.yaml", b"c: 3\n")

    merged, _ = _merge(ours, theirs, tmp_path)

    assert merged["files"] == ["gw/a.py", "gw/b.py", "gw/c.py"]
    issue_sets = {item["issue"]: item for item in merged["authorized_issue_file_sets"]}
    assert list(issue_sets) == [900, 901, 902]
    assert issue_sets[900]["files"] == ["gw/a.py", "gw/b.py", "gw/c.py"]
    # 标量冲突保留 ours；as_of 虽为字符串也按 ours 处理。
    assert merged["as_of"] == "2026-10-28"
    assert merged["purpose"] == "ours"
    admitted = {item["path"]: item for item in merged["admitted_files"]}
    assert [item["path"] for item in merged["admitted_files"]] == [
        "docs/contracts/A.yaml",
        "docs/contracts/B.yaml",
        "docs/contracts/C.yaml",
    ]
    # 同一 path 的冲突字段保留 ours。
    assert admitted["docs/contracts/A.yaml"]["classification"] == "frozen_interface_contract"


def test_json_driver_takes_changed_side_hash_not_working_tree(tmp_path: Path):
    """回归 #478：工作树仍滞后时，必须采用 theirs（变更侧）登记的正确哈希。"""
    target = _write_file(tmp_path, "docs/contracts/A.yaml", b"line1\r\nline2\r\n")
    stale_hash = hashlib.sha256(b"line1\nline2\n").hexdigest()
    theirs_hash = "c" * 64

    base = _ledger_with([_entry("docs/contracts/A.yaml", sha=stale_hash)])
    ours = _ledger_with([_entry("docs/contracts/A.yaml", sha=stale_hash)])
    theirs = _ledger_with([_entry("docs/contracts/A.yaml", sha=theirs_hash)])

    merged, _ = _merge(ours, theirs, tmp_path, base)

    # 工作树内容仍是旧内容：驱动若按 WT 重算就会写回 stale_hash。
    assert stale_hash == canonical_sha256(target)
    assert merged["admitted_files"][0]["sha256"] == theirs_hash


def test_json_driver_keeps_ours_hash_when_only_ours_changed(tmp_path: Path):
    _write_file(tmp_path, "docs/contracts/A.yaml", b"line1\r\nline2\r\n")
    base_hash = "a" * 64
    ours_hash = "b" * 64

    base = _ledger_with([_entry("docs/contracts/A.yaml", sha=base_hash)])
    ours = _ledger_with([_entry("docs/contracts/A.yaml", sha=ours_hash)])
    theirs = _ledger_with([_entry("docs/contracts/A.yaml", sha=base_hash)])

    merged, _ = _merge(ours, theirs, tmp_path, base)

    assert merged["admitted_files"][0]["sha256"] == ours_hash


def test_json_driver_takes_common_hash_when_both_sides_changed_identically(tmp_path: Path):
    _write_file(tmp_path, "docs/contracts/A.yaml", b"a: 1\n")
    base = _ledger_with([_entry("docs/contracts/A.yaml", sha="a" * 64)])
    ours = _ledger_with([_entry("docs/contracts/A.yaml", sha="b" * 64)])
    theirs = _ledger_with([_entry("docs/contracts/A.yaml", sha="b" * 64)])

    merged, _ = _merge(ours, theirs, tmp_path, base)

    assert merged["admitted_files"][0]["sha256"] == "b" * 64


def test_json_driver_fails_when_both_sides_changed_hash_differently(tmp_path: Path):
    """两侧都相对 base 改了哈希且互不相同：不猜，交回 Git 保留冲突。"""
    _write_file(tmp_path, "docs/contracts/A.yaml", b"a: 1\n")
    base = _ledger_with([_entry("docs/contracts/A.yaml", sha="a" * 64)])
    ours = _ledger_with([_entry("docs/contracts/A.yaml", sha="b" * 64)])
    theirs = _ledger_with([_entry("docs/contracts/A.yaml", sha="c" * 64)])

    _merged, fatal = _merge_result(ours, theirs, tmp_path, base)[:2]

    assert fatal, "两侧哈希都变且不同必须视为致命冲突"
    assert "sha256" in fatal[0]


def test_json_driver_fails_when_base_missing_and_hashes_differ(tmp_path: Path):
    """无基线时无法判定变更侧，同样以非零退出交回 Git。"""
    _write_file(tmp_path, "docs/contracts/A.yaml", b"a: 1\n")
    ours = _ledger_with([_entry("docs/contracts/A.yaml", sha="b" * 64)])
    theirs = _ledger_with([_entry("docs/contracts/A.yaml", sha="c" * 64)])

    _merged, fatal = _merge_result(ours, theirs, tmp_path, None)[:2]

    assert fatal, "基线缺失且两侧哈希不同必须视为致命冲突"


def test_json_driver_warns_on_missing_target_without_fabricating_hash(tmp_path: Path):
    ours = _ledger_with([_entry("docs/contracts/A.yaml", sha="b" * 64)])
    theirs = _ledger_with([_entry("docs/contracts/A.yaml", sha="b" * 64)])

    merged, fatal, warnings = _merge_result(ours, theirs, tmp_path, None)

    assert not fatal
    assert merged["admitted_files"][0]["sha256"] == "b" * 64
    assert any("目标文件缺失" in warning for warning in warnings)


def test_json_driver_rejects_schema_mismatch(tmp_path: Path):
    ours = {"schema": "gw.cleanroom-admission/v1"}
    theirs = {"schema": "gw.cleanroom-admission-shard/v1"}
    merged, fatal = merge_json_registry.merge_payloads(ours, theirs, tmp_path)[:2]
    assert fatal, "schema 不一致必须视为致命冲突"
    assert "schema" in fatal[0]


def test_base_ledger_driver_rewrites_pin(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ledger_path = tmp_path.joinpath(*BASE_LEDGER_PATH.split("/"))
    _write_file(tmp_path, "docs/contracts/A.yaml", b"a: 1\n")
    _write_file(tmp_path, "docs/contracts/B.yaml", b"b: 2\n")
    ledger = {
        "schema": "gw.cleanroom-admission/v1",
        "admitted_files": [_entry("docs/contracts/A.yaml")],
    }
    theirs = {
        "schema": "gw.cleanroom-admission/v1",
        "admitted_files": [_entry("docs/contracts/B.yaml")],
    }
    _write_json(ledger_path, ledger)
    base_path = tmp_path / "base.json"
    theirs_path = tmp_path / "theirs.json"
    _write_json(base_path, ledger)
    _write_json(theirs_path, theirs)

    pin_path = tmp_path / "tests" / "hygiene" / "test_cleanroom_hygiene.py"
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(
        "ADMISSION_BASE_METADATA_SHA256 = (\n"
        '    "' + "0" * 64 + '"\n'
        ")\n",
        encoding="utf-8",
    )

    exit_code = merge_json_registry.main(
        ["driver", str(base_path), str(ledger_path), str(theirs_path), BASE_LEDGER_PATH]
    )
    assert exit_code == 0

    merged = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert [item["path"] for item in merged["admitted_files"]] == [
        "docs/contracts/A.yaml",
        "docs/contracts/B.yaml",
    ]
    match = re.search(
        r"ADMISSION_BASE_METADATA_SHA256\s*=\s*\(?\s*\"([0-9a-f]{64})\"",
        pin_path.read_text(encoding="utf-8"),
    )
    assert match, "钉常量未被找到"
    assert match.group(1) == canonical_sha256(ledger_path)


def test_line_driver_unions_sorts_and_keeps_header(tmp_path: Path):
    ours = "# managed list\n\nb.txt\na.txt\n"
    theirs = "# other header\nc.txt\na.txt\n"
    merged = merge_line_list.merge_line_text(ours, theirs)
    assert merged == "# managed list\n\na.txt\nb.txt\nc.txt\n"

    ours_path = tmp_path / "ours.txt"
    theirs_path = tmp_path / "theirs.txt"
    ours_path.write_text(ours, encoding="utf-8")
    theirs_path.write_text(theirs, encoding="utf-8")
    assert (
        merge_line_list.main(
            ["driver", str(tmp_path / "base.txt"), str(ours_path), str(theirs_path), "ours.txt"]
        )
        == 0
    )
    assert ours_path.read_text(encoding="utf-8") == merged


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "merge-driver-test",
        "GIT_AUTHOR_EMAIL": "merge-driver-test@example.invalid",
        "GIT_COMMITTER_NAME": "merge-driver-test",
        "GIT_COMMITTER_EMAIL": "merge-driver-test@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, text=True, env=env
    )


def _quote(path: Path) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def _configure_drivers(root: Path) -> None:
    json_cmd = f"{_quote(Path(sys.executable))} {_quote(_DRIVER_DIR / 'merge_json_registry.py')} %O %A %B %P"
    line_cmd = f"{_quote(Path(sys.executable))} {_quote(_DRIVER_DIR / 'merge_line_list.py')} %O %A %B %P"
    assert _git(root, "config", "merge.hygiene-json.driver", json_cmd).returncode == 0
    assert _git(root, "config", "merge.hygiene-lines.driver", line_cmd).returncode == 0


def test_real_git_merge_uses_configured_drivers(tmp_path: Path):
    for relative, content in (
        ("docs/contracts/A.yaml", b"a: 1\n"),
        ("docs/contracts/B.yaml", b"b: 2\n"),
    ):
        _write_file(tmp_path, relative, content)
    _write_file(
        tmp_path,
        ".gitattributes",
        (
            f"{BASE_LEDGER_PATH} merge=hygiene-json\n"
            "tests/frontend/node-files.txt merge=hygiene-lines\n"
        ).encode(),
    )
    ledger_path = tmp_path.joinpath(*BASE_LEDGER_PATH.split("/"))
    _write_json(
        ledger_path,
        {
            "schema": "gw.cleanroom-admission/v1",
            "as_of": "2026-10-28",
            "release_authorized": True,
            "authorized_issue_file_sets": [],
            "admitted_files": [],
            "accepted_test_files": [],
        },
    )
    pin_path = tmp_path / "tests" / "hygiene" / "test_cleanroom_hygiene.py"
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(
        'ADMISSION_BASE_METADATA_SHA256 = (\n    "' + "0" * 64 + '"\n)\n',
        encoding="utf-8",
    )
    _write_file(tmp_path, "tests/frontend/node-files.txt", b"tests/frontend/z.mjs\n")

    assert _git(tmp_path, "init", "--quiet").returncode == 0
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "base").returncode == 0
    _configure_drivers(tmp_path)
    base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    # 分支 ours：账本追加 A，node 清单追加 a。
    assert _git(tmp_path, "checkout", "--quiet", "-b", "ours").returncode == 0
    ours_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ours_ledger["admitted_files"] = [
        _entry(
            "docs/contracts/A.yaml",
            sha=canonical_sha256(tmp_path / "docs" / "contracts" / "A.yaml"),
        )
    ]
    _write_json(ledger_path, ours_ledger)
    _write_file(
        tmp_path,
        "tests/frontend/node-files.txt",
        b"tests/frontend/a.mjs\ntests/frontend/z.mjs\n",
    )
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "ours").returncode == 0

    # 分支 theirs：从同一基线出发，账本追加 B，node 清单追加 b。
    assert _git(tmp_path, "checkout", "--quiet", "-b", "theirs", base_sha).returncode == 0
    theirs_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    theirs_ledger["admitted_files"] = [
        _entry(
            "docs/contracts/B.yaml",
            sha=canonical_sha256(tmp_path / "docs" / "contracts" / "B.yaml"),
        )
    ]
    _write_json(ledger_path, theirs_ledger)
    _write_file(
        tmp_path,
        "tests/frontend/node-files.txt",
        b"tests/frontend/b.mjs\ntests/frontend/z.mjs\n",
    )
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "theirs").returncode == 0

    assert _git(tmp_path, "checkout", "--quiet", "ours").returncode == 0
    merge = _git(tmp_path, "merge", "--no-edit", "theirs")
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert "CONFLICT" not in merge.stdout + merge.stderr

    merged = json.loads(ledger_path.read_text(encoding="utf-8"))
    paths = [item["path"] for item in merged["admitted_files"]]
    assert paths == ["docs/contracts/A.yaml", "docs/contracts/B.yaml"]
    for entry in merged["admitted_files"]:
        target = tmp_path.joinpath(*entry["path"].split("/"))
        assert entry["sha256"] == canonical_sha256(target)

    node_lines = (tmp_path / "tests" / "frontend" / "node-files.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert node_lines == [
        "tests/frontend/a.mjs",
        "tests/frontend/b.mjs",
        "tests/frontend/z.mjs",
    ]

    pin_match = re.search(
        r"ADMISSION_BASE_METADATA_SHA256\s*=\s*\(?\s*\"([0-9a-f]{64})\"",
        pin_path.read_text(encoding="utf-8"),
    )
    assert pin_match and pin_match.group(1) == canonical_sha256(ledger_path), (
        "基础账本合并后必须重写 ADMISSION_BASE_METADATA_SHA256"
    )


def _init_registry_repo(root: Path, ledger: dict) -> tuple[Path, str]:
    """写入驱动映射 + 基础账本 + 钉文件，初始化仓库并返回（账本路径, 基线提交）。"""
    _write_file(
        root, ".gitattributes", f"{BASE_LEDGER_PATH} merge=hygiene-json\n".encode()
    )
    ledger_path = root.joinpath(*BASE_LEDGER_PATH.split("/"))
    _write_json(ledger_path, ledger)
    pin_path = root / "tests" / "hygiene" / "test_cleanroom_hygiene.py"
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(
        'ADMISSION_BASE_METADATA_SHA256 = (\n    "' + "0" * 64 + '"\n)\n',
        encoding="utf-8",
    )
    assert _git(root, "init", "--quiet").returncode == 0
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "--quiet", "-m", "base").returncode == 0
    _configure_drivers(root)
    return ledger_path, _git(root, "rev-parse", "HEAD").stdout.strip()


def test_real_git_merge_keeps_theirs_hash_when_working_tree_lags(tmp_path: Path):
    """回归 #478：驱动不得把对方正确哈希覆盖成滞后的工作树哈希。

    base：A.yaml = OLD，账本登记 OLD；ours：只追加无关条目；theirs：改 A.yaml 为 NEW
    并登记 NEW。合并后账本必须是 NEW，而不是 merge 瞬间工作树的 OLD。
    """
    old_content = b"a: 1\n"
    new_content = b"a: 1\nb: 2\n"
    a_path = _write_file(tmp_path, "docs/contracts/A.yaml", old_content)
    old_hash = canonical_sha256(a_path)
    ledger = {
        "schema": "gw.cleanroom-admission/v1",
        "as_of": "2026-10-28",
        "authorized_issue_file_sets": [],
        "admitted_files": [_entry("docs/contracts/A.yaml", sha=old_hash)],
        "accepted_test_files": [],
    }
    ledger_path, base_sha = _init_registry_repo(tmp_path, ledger)

    # ours：A.yaml 与其哈希都不动，只追加无关条目 B.yaml。
    assert _git(tmp_path, "checkout", "--quiet", "-b", "ours").returncode == 0
    b_path = _write_file(tmp_path, "docs/contracts/B.yaml", b"b: 2\n")
    ours_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ours_ledger["admitted_files"].append(
        _entry("docs/contracts/B.yaml", sha=canonical_sha256(b_path))
    )
    _write_json(ledger_path, ours_ledger)
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "ours").returncode == 0

    # theirs：改 A.yaml 内容并登记新哈希。
    assert _git(tmp_path, "checkout", "--quiet", "-b", "theirs", base_sha).returncode == 0
    _write_file(tmp_path, "docs/contracts/A.yaml", new_content)
    new_hash = canonical_sha256(a_path)
    assert new_hash != old_hash
    theirs_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    theirs_ledger["admitted_files"] = [_entry("docs/contracts/A.yaml", sha=new_hash)]
    _write_json(ledger_path, theirs_ledger)
    assert _git(tmp_path, "add", "-A").returncode == 0
    assert _git(tmp_path, "commit", "--quiet", "-m", "theirs").returncode == 0

    assert _git(tmp_path, "checkout", "--quiet", "ours").returncode == 0
    merge = _git(tmp_path, "merge", "--no-edit", "theirs")
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert "CONFLICT" not in merge.stdout + merge.stderr

    # Windows 检出（core.autocrlf=true）会把 LF 落盘成 CRLF；
    # 按 canonical 口径归一化行尾后再比对，保证跨平台内容逻辑等价。
    normalized_bytes = a_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    assert normalized_bytes == new_content
    merged = json.loads(ledger_path.read_text(encoding="utf-8"))
    admitted = {item["path"]: item for item in merged["admitted_files"]}
    assert admitted["docs/contracts/A.yaml"]["sha256"] == new_hash
    assert admitted["docs/contracts/A.yaml"]["sha256"] == canonical_sha256(a_path)
    assert admitted["docs/contracts/B.yaml"]["sha256"] == canonical_sha256(b_path)


def test_repository_registry_passes_check(repo_root: Path):
    """CI 兜底：网页端合并绕过驱动后，本用例仍会以修复命令失败。"""
    script = repo_root / "scripts" / "fix-hygiene-registry.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        "洁净室登记与工作树不一致。\n"
        f"{output}\n"
        f"修复命令：{FIX_COMMAND}\n"
        "本地合并前请先安装驱动：sh scripts/git-merge-drivers/install.sh"
    )
