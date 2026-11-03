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

"""QIN-519 / QIN-525 全库预览缓存预热契约测试。

覆盖：
- ``scope="library"`` 复用 ``generate-background``，返回 202 + 稳定 job_id + poll_hint；
- 任务真异步（提交即返回，进度经 ``jobs/{job_id}`` 回读 processed/total）；
- 已有新鲜缓存一律跳过（第二次运行 generated=0、skipped=total），真实落盘不重复；
- QIN-525：预热范围覆盖全部素材类型（含 ``kind="indexed"`` 的索引素材），
  文档 / 字体 / 文本等无位图能力的类型也产出可解码占位图；
- QIN-525：``mode="rebuild"`` 先清空整棵缓存树再全量重生成；
- QIN-525：``scope="all"`` 按分类删除缓存，未勾选类别与非法命名文件一律保留。
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gw.api.app import create_app
from gw.asset_registry import repository as asset_repo
from gw.core.errors import CleanroomException
from gw.media import repository as media_repo

AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
CACHE_SUFFIXES = (".webp", ".jpg.png", ".jxl")


@pytest.fixture(autouse=True)
def media_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_root = tmp_path / "gw-data"
    allowed_root = tmp_path / "roots"
    data_root.mkdir()
    allowed_root.mkdir()
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed_root))
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(tmp_path / "gw-auth" / "auth.sqlite3"))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(tmp_path / "gw-video"))
    # 显式钉死 webp：默认 jxl 依赖 ffmpeg 的 libjxl，CI 镜像缺编码器会造成
    # 「生成 0 张」的假失败；断言本身不锁扩展名。
    media_repo.patch_thumbnail_settings(
        {
            "mode": "source",
            "directory": "",
            "cache_folder": "god-cache",
            "format": "webp",
            "width": 256,
        }
    )
    return {"data": data_root, "roots": allowed_root}


def _png(width: int = 320, height: int = 180, color: tuple[int, int, int] = (30, 40, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _register_asset(
    runtime: dict[str, Path], name: str, kind: str = "image"
) -> tuple[str, int]:
    path = runtime["roots"] / name
    path.write_bytes(_png())
    created = asset_repo.import_assets([{"name": name, "kind": kind}], kind, None)
    asset_id = created["asset_ids"][0]
    updated = asset_repo.update_asset(
        asset_id, {"display_path": str(path), "expected_version": created["revision"]}
    )
    return asset_id, int(updated.get("revision") or created["revision"])


def _wait_job(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = media_repo.get_job(job_id)["task"]
        if job["status"] not in ("queued", "running", "paused"):
            return job
        time.sleep(0.05)
    raise AssertionError("预热任务超时未结束：%s" % job_id)


def _wait_status(job_id: str, statuses: tuple[str, ...], timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = media_repo.get_job(job_id)["task"]
        if job["status"] in statuses:
            return job
        time.sleep(0.03)
    raise AssertionError("预热任务未进入状态 %s：%s" % (statuses, job_id))


def _cache_files(runtime: dict[str, Path]) -> list[Path]:
    root = runtime["data"] / "media_output" / "god-cache"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith(CACHE_SUFFIXES)
    )


def test_preview_all_endpoint_returns_async_job(media_runtime: dict[str, Path]) -> None:
    asset_ids = [_register_asset(media_runtime, "preview-%d.png" % index)[0] for index in range(3)]

    with TestClient(create_app()) as client:
        started = time.monotonic()
        response = client.post(
            "/api/asset-thumbnails/generate-background",
            headers=AUTH,
            json={"scope": "library", "throttle_ms": 300},
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 202, response.text
        data = response.json()
        assert data["scope"] == "library"
        assert data["total"] == len(asset_ids)
        job_id = data["job_id"]
        assert data["poll_hint"] == "/api/asset-thumbnails/jobs/%s" % job_id
        # 真异步：3 项 × 300ms 限速远未跑完，POST 已返回。
        assert elapsed < 0.7, elapsed

        first = client.get(data["poll_hint"], headers=AUTH)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "running"

        job = _wait_job(job_id)
        assert job["status"] == "succeeded", job
        progress = job["progress"]
        assert progress["total"] == len(asset_ids)
        assert progress["processed"] == len(asset_ids)
        assert job["result"]["generated"] == len(asset_ids)
        assert job["result"]["skipped"] == 0
        assert job["result"]["width"] == 256
        assert job["result"]["elapsed_seconds"] >= 0
        assert len(_cache_files(media_runtime)) == len(asset_ids)

        replay = client.get(data["poll_hint"], headers=AUTH)
        assert replay.status_code == 200
        assert replay.json()["status"] == "succeeded"


def test_preview_all_is_idempotent_across_runs(media_runtime: dict[str, Path]) -> None:
    asset_ids = [_register_asset(media_runtime, "repeat-%d.png" % index)[0] for index in range(2)]

    job_id, poll_hint, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert poll_hint == "/api/asset-thumbnails/jobs/%s" % job_id
    assert total == len(asset_ids)
    first = _wait_job(job_id)
    assert first["status"] == "succeeded", first
    assert first["result"]["generated"] == len(asset_ids)
    assert first["result"]["skipped"] == 0
    first_files = _cache_files(media_runtime)
    assert len(first_files) == len(asset_ids)

    second_id, _, second_total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert second_total == len(asset_ids)
    second = _wait_job(second_id)
    assert second["status"] == "succeeded", second
    # 幂等：已新鲜的同宽缓存直接跳过，不重复解码。
    assert second["result"]["generated"] == 0
    assert second["result"]["skipped"] == len(asset_ids)
    assert _cache_files(media_runtime) == first_files


def test_preview_all_covers_documents_and_skips_archived_assets(
    media_runtime: dict[str, Path],
) -> None:
    """QIN-525：预热覆盖全部类型，只排除已归档素材。"""
    _register_asset(media_runtime, "keep.png")
    _register_asset(media_runtime, "notes.txt", kind="text")
    archived_id, revision = _register_asset(media_runtime, "archived.png")
    asset_repo.update_asset(archived_id, {"archived": True, "expected_version": revision})

    job_id, _, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert total == 2
    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 2
    assert len(_cache_files(media_runtime)) == 2


def test_preview_all_enumerates_indexed_assets(media_runtime: dict[str, Path]) -> None:
    """AC1 回归：``kind="indexed"`` 的索引素材必须进入预热范围。

    QIN-519 用顶层 ``kind in ("image", "video")`` 过滤，真实库里的索引素材
    ``kind`` 全是 ``indexed``，导致枚举为空、任务空跑却报「已完成」。
    """
    _register_asset(media_runtime, "indexed-shot.png", kind="indexed")

    job_id, _, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert total == 1, "kind=indexed 的素材必须被枚举进预热范围"
    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 1
    assert len(_cache_files(media_runtime)) == 1


def test_preview_all_generates_placeholder_for_documents_and_fonts(
    media_runtime: dict[str, Path],
) -> None:
    """AC2：文档 / 文本 / 字体没有位图能力时，产出可解码的类型占位图。"""
    for name, kind in (
        ("spec.pdf", "document"),
        ("notes.md", "text"),
        ("Brand.otf", "font"),
        ("scene.glb", "model"),
    ):
        _register_asset(media_runtime, name, kind=kind)

    job_id, _, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert total == 4
    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 4
    assert job["result"]["failed"] == 0, job

    files = _cache_files(media_runtime)
    assert len(files) == 4
    for path in files:
        with Image.open(path) as image:
            assert image.width == 256
            assert image.height == 256


def test_preview_all_rebuild_clears_then_regenerates(
    media_runtime: dict[str, Path],
) -> None:
    """AC3：``mode="rebuild"`` 先清空缓存树，再全量重生成（skipped 必须为 0）。"""
    for index in range(2):
        _register_asset(media_runtime, "rebuild-%d.png" % index)

    first_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 0})
    first = _wait_job(first_id)
    assert first["result"]["generated"] == 2
    assert len(_cache_files(media_runtime)) == 2

    rebuild_id, _, total = media_repo.start_preview_all_job(
        {"mode": "rebuild", "throttle_ms": 0}
    )
    assert total == 2
    job = _wait_job(rebuild_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["mode"] == "rebuild"
    assert job["result"]["cleared"] == 2
    assert job["result"]["generated"] == 2
    assert job["result"]["skipped"] == 0
    assert len(_cache_files(media_runtime)) == 2


def test_preview_all_rejects_unknown_mode(media_runtime: dict[str, Path]) -> None:
    with pytest.raises(CleanroomException) as excinfo:
        media_repo.start_preview_all_job({"mode": "everything"})
    assert excinfo.value.code == "INVALID_PREVIEW_MODE"


def test_delete_cache_scope_all_filters_categories(media_runtime: dict[str, Path]) -> None:
    """AC4：只删勾选类别；未勾选类别、分镜与非缓存文件全部保留。"""
    bucket = media_runtime["data"] / "media_output" / "god-cache" / "bucket-1"
    bucket.mkdir(parents=True)
    image_file = bucket / "asset-1-w0256-deadbeef.webp"
    video_file = bucket / "asset-2-w0256-deadbeef.frame.jpg"
    storyboard_file = bucket / "storyboard.jpg"
    user_file = bucket / "keep-me.txt"
    for path in (image_file, video_file, storyboard_file, user_file):
        path.write_bytes(b"cache")

    result = media_repo.delete_outputs(
        "thumbnails", {"scope": "all", "categories": ["video"]}
    )
    assert result["kind"] == "cache"
    assert result["removed"] == 1
    assert not video_file.exists()
    assert image_file.exists()
    assert storyboard_file.exists()
    assert user_file.exists(), "非缓存命名的文件绝不能删"

    all_result = media_repo.delete_outputs(
        "thumbnails", {"scope": "all", "categories": ["all"]}
    )
    assert all_result["removed"] == 2
    assert not image_file.exists()
    assert not storyboard_file.exists()
    assert user_file.exists()


def test_settings_response_includes_cache_statistics(
    media_runtime: dict[str, Path],
) -> None:
    """设置接口附带缓存统计，供「全部缓存 / 重建缓存」按钮判态。"""
    _register_asset(media_runtime, "stats.png")
    job_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 0})
    _wait_job(job_id)

    with TestClient(create_app()) as client:
        response = client.get("/api/asset-thumbnails/settings", headers=AUTH)
        assert response.status_code == 200, response.text
        cache = response.json()["cache"]
        assert cache["available"] is True
        assert cache["total_assets"] == 1
        assert cache["cached_assets"] == 1
        assert {item["id"] for item in cache["categories"]} == {
            "image",
            "video",
            "storyboard",
            "other",
        }


def test_preview_all_reuses_running_job_instead_of_duplicating(
    media_runtime: dict[str, Path],
) -> None:
    for index in range(2):
        _register_asset(media_runtime, "dedupe-%d.png" % index)

    first_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 400})
    second_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 400})
    assert second_id == first_id
    job = _wait_job(first_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 2


def _seed_running_job(raw: dict, job_id: str, total: int) -> None:
    """造一条「上次进程留下的 running 记录」，模拟重启前被中断的预热。"""
    raw.setdefault("jobs", {})[job_id] = {
        "job_id": job_id,
        "status": "running",
        "state": "running",
        "kind": "preview_all",
        "mode": "rebuild",
        "progress": {
            "processed": 0,
            "total": total,
            "generated": 0,
            "skipped": 0,
            "failed": 0,
        },
        "result": None,
        "error": None,
        "created_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
    }


def test_preview_all_reconciles_interrupted_job(media_runtime: dict[str, Path]) -> None:
    """遗留的 running 记录必须收敛为 failed，不能顶死后续每一次预热。

    QIN-525 实机复现：进程在预热中途退出后，``media_jobs.json`` 里留下
    ``status="running"`` 的记录，而线程已不存在；旧逻辑把它当成「正在跑」直接
    复用，于是点【全部缓存】/【重建缓存】永远没有反应。
    """
    _register_asset(media_runtime, "resume.png")
    media_repo._job_state().mutate(lambda raw: _seed_running_job(raw, "pjob_9001", 1))

    job_id, poll_hint, total = media_repo.start_preview_all_job({"throttle_ms": 0})
    assert job_id != "pjob_9001", "遗留 running 记录不得被复用"
    assert poll_hint == "/api/asset-thumbnails/jobs/%s" % job_id
    assert total == 1

    interrupted = media_repo.get_job("pjob_9001")["task"]
    assert interrupted["status"] == "failed"
    assert interrupted["error"] == "PREVIEW_ALL_INTERRUPTED"
    assert interrupted["finished_at"]

    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == 1


def test_preview_all_worker_crash_marks_job_failed(
    media_runtime: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """线程内逃逸异常必须收敛为 failed：绝不留永远 running 的僵尸任务。"""

    class _WorkerCrash(BaseException):
        """模拟线程级致命异常（不被单项 except Exception 捕获）。"""

    asset_id, _ = _register_asset(media_runtime, "crash.png")
    media_repo._job_state().mutate(lambda raw: _seed_running_job(raw, "pjob_9002", 1))

    def boom(_payload: dict) -> dict:
        raise _WorkerCrash("模拟线程崩溃")

    monkeypatch.setattr(media_repo, "preview_bytes", boom)
    media_repo._run_preview_all("pjob_9002", [asset_id], 256, 0.0)

    task = media_repo.get_job("pjob_9002")["task"]
    assert task["status"] == "failed"
    assert task["error"] == "PREVIEW_ALL_CRASHED"
    assert task["finished_at"]



def test_preview_all_runs_items_concurrently_without_losing_any(
    media_runtime: dict[str, Path],
) -> None:
    """并发预热必须逐项落盘：并发不是「抽样」或「只跑第一批」。"""
    count = 12
    for index in range(count):
        _register_asset(media_runtime, "parallel-%d.png" % index)

    job_id, _, total = media_repo.start_preview_all_job(
        {"throttle_ms": 0, "workers": 4}
    )
    assert total == count
    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["workers"] == 4
    assert job["result"]["generated"] == count
    assert job["result"]["failed"] == 0
    assert len(_cache_files(media_runtime)) == count


def test_preview_all_pause_freezes_progress_then_resume_finishes(
    media_runtime: dict[str, Path],
) -> None:
    """暂停后进度不再推进；继续后把剩余项跑完。"""
    count = 12
    for index in range(count):
        _register_asset(media_runtime, "pause-%d.png" % index)

    job_id, _, total = media_repo.start_preview_all_job(
        {"workers": 1, "throttle_ms": 500}
    )
    media_repo.control_preview_all_job(job_id, {"action": "pause"})
    paused = _wait_status(job_id, ("paused",))
    frozen = paused["progress"]["processed"]

    # 暂停语义是「不再派发新批次」，正在飞行的那一批会跑完并如实计入进度。
    time.sleep(0.8)
    settled = media_repo.get_job(job_id)["task"]
    assert settled["status"] == "paused", settled
    settled_processed = settled["progress"]["processed"]
    assert settled_processed - frozen <= 2, "暂停后最多收尾一批，不得继续推进"

    time.sleep(1.0)
    still = media_repo.get_job(job_id)["task"]
    assert still["status"] == "paused", still
    assert still["progress"]["processed"] == settled_processed, "暂停期间不得继续推进"

    media_repo.control_preview_all_job(job_id, {"action": "resume"})
    job = _wait_job(job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["generated"] == total
    assert job["progress"]["processed"] == total


def test_preview_all_cancel_stops_early_and_reports_partial_result(
    media_runtime: dict[str, Path],
) -> None:
    """取消必须在跑完之前停：状态为 cancelled，并如实回报已完成项。"""
    count = 12
    for index in range(count):
        _register_asset(media_runtime, "cancel-%d.png" % index)

    job_id, _, total = media_repo.start_preview_all_job(
        {"workers": 1, "throttle_ms": 500}
    )
    degraded_before = int(media_repo._PREVIEW_CACHE_STATS.get("degraded", 0))
    time.sleep(0.1)
    media_repo.control_preview_all_job(job_id, {"action": "cancel"})
    job = _wait_job(job_id)

    assert job["status"] == "cancelled", job
    assert job["result"]["cancelled"] is True
    assert job["result"]["processed"] < total
    assert job["result"]["generated"] == job["result"]["processed"]
    # 取消场景不应出现缓存降级：计数对得上却零文件，说明写盘被静默跳过了。
    degraded_after = int(media_repo._PREVIEW_CACHE_STATS.get("degraded", 0))
    assert degraded_after == degraded_before, "取消场景出现缓存降级"
    # 数据根必须仍是本用例的临时目录：被前一个用例污染时产物会写到别处。
    from gw.core import storage as _storage

    assert _storage.data_root() == media_runtime["data"], _storage.data_root()
    # 已生成的产物保留，取消不清空已落盘内容。
    settings_now = media_repo.get_thumbnail_settings()
    assert len(_cache_files(media_runtime)) == job["result"]["generated"], (
        media_repo.cache_root_path(settings_now),
        media_repo.cache_statistics(),
        job["result"],
    )


def test_preview_all_control_rejects_unknown_action_and_finished_job(
    media_runtime: dict[str, Path],
) -> None:
    _register_asset(media_runtime, "control.png")
    job_id, _, _ = media_repo.start_preview_all_job({"throttle_ms": 0})
    _wait_job(job_id)

    with pytest.raises(CleanroomException) as illegal:
        media_repo.control_preview_all_job(job_id, {"action": "explode"})
    assert illegal.value.code == "INVALID_PREVIEW_ACTION"

    with pytest.raises(CleanroomException) as finished:
        media_repo.control_preview_all_job(job_id, {"action": "pause"})
    assert finished.value.code == "PREVIEW_JOB_FINISHED"


def test_preview_all_rejects_invalid_workers(media_runtime: dict[str, Path]) -> None:
    with pytest.raises(CleanroomException) as excinfo:
        media_repo.start_preview_all_job({"workers": "many"})
    assert excinfo.value.code == "INVALID_PREVIEW_WORKERS"


def test_preview_all_default_workers_is_bounded() -> None:
    """默认并发度不超过 16（低配机按 CPU 收敛），显式值夹在 1..32。"""
    default = media_repo._preview_all_workers({})
    assert 1 <= default <= media_repo.PREVIEW_ALL_DEFAULT_WORKERS
    assert media_repo._preview_all_workers({"workers": 99}) == (
        media_repo.PREVIEW_ALL_MAX_WORKERS
    )
    assert media_repo._preview_all_workers({"workers": 0}) == 1
