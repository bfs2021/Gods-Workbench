"""Phase 12 A2 素材注册表真实落盘契约回归。

覆盖：真实 CRUD 写→读回、稳定 ID、CAS 409、路径准入失败关闭、
媒体/索引依赖缺失 503、认证与只读角色边界、删除后不再返回。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gw.api.app import create_app

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "docs/contracts/ASSET-REGISTRY-INTERFACE-CATALOG.yaml"
FIXTURES = ROOT / "docs/fixtures"
AUTH = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "editor"}
READONLY = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "readonly"}
GOVERNOR = {"Authorization": "Bearer cleanroom-test", "X-User-Role": "governor"}


def _pairs() -> set[tuple[str, str]]:
    text = CATALOG.read_text(encoding="utf-8")
    return set(re.findall(r"\s+method: (\w+)\n\s+path: (\S+)", text))


def _paths() -> set[str]:
    return {path for _, path in _pairs()}


def _client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def assets(client):
    """预置两个真实资产，供各用例复用。"""
    response = client.post("/api/asset-registry/assets/import", headers=AUTH,
                           json={"items": [{"name": "角色参考.png", "kind": "image"},
                                           {"name": "场景.mp4", "kind": "video"}]})
    assert response.status_code == 200, response.text
    return [a["asset_id"] for a in response.json()["assets"]]


# ---------------------------------------------------------------------------
# 契约与夹具
# ---------------------------------------------------------------------------


def test_a2_contract_and_route_matrix_are_stable():
    fixture = json.loads((FIXTURES / "phase11-b3-route-matrix.json").read_text(encoding="utf-8"))
    assert fixture["path_count"] == 52
    assert fixture["method_path_count"] == 61
    assert len(_paths()) == 52
    assert len(_pairs()) == 61
    text = CATALOG.read_text(encoding="utf-8")
    assert "version: p12-a2-3" in text
    assert "ASSET_REGISTRY_NOT_INTEGRATED" not in text


def test_a2_fixtures_are_present():
    for name in (
        "phase11-b3-empty-registry.json",
        "phase11-b3-fail-closed.json",
        "phase11-b3-route-matrix.json",
    ):
        assert (FIXTURES / name).is_file()


# ---------------------------------------------------------------------------
# 认证与授权边界（先于参数校验）
# ---------------------------------------------------------------------------


def test_a2_compact_auth_boundaries(client):
    assert client.get("/api/asset-registry").status_code == 401
    assert client.get("/api/asset-registry/assets").status_code == 401
    assert client.post("/api/asset-registry/assets/import", json={"items": [{"name": "x"}]}).status_code == 401
    assert client.post("/api/asset-registry/assets/import", headers=READONLY,
                       json={"items": [{"name": "x"}]}).status_code == 403
    assert client.post("/api/asset-registry/governance/operations", headers=READONLY,
                       json={"operation": "sweep"}).status_code == 403
    assert client.post("/api/asset-registry/assets/import", headers=READONLY).status_code == 403


# ---------------------------------------------------------------------------
# 写入 → 读回（真实副作用可核验）
# ---------------------------------------------------------------------------


def test_a2_import_returns_stable_ids_and_reads_back(client):
    first = client.post("/api/asset-registry/assets/import", headers=AUTH,
                        json={"items": [{"name": "甲.png", "kind": "image"}]})
    second = client.post("/api/asset-registry/assets/import", headers=AUTH,
                         json={"items": [{"name": "乙.png", "kind": "image"}]})
    assert first.status_code == second.status_code == 200
    assert first.json()["asset_ids"] == ["ast_0001"]
    assert second.json()["asset_ids"] == ["ast_0002"]
    listed = client.get("/api/asset-registry/assets?limit=50", headers=AUTH).json()
    assert listed["total"] == 2
    assert [a["asset_id"] for a in listed["items"]] == ["ast_0001", "ast_0002"]
    assert listed["total_known"] is True and listed["has_more"] is False


def test_a2_asset_update_archive_delete_roundtrip(client, assets):
    asset_id = assets[0]
    updated = client.patch(f"/api/asset-registry/assets/{asset_id}", headers=AUTH,
                           json={"name": "角色参考-改名"})
    assert updated.status_code == 200
    assert updated.json()["asset"]["name"] == "角色参考-改名"
    assert updated.json()["asset"]["version"] == 2
    assert client.get(f"/api/asset-registry/assets/{asset_id}", headers=AUTH).json()["asset"]["name"] == "角色参考-改名"

    registry_revision = client.get("/api/asset-registry/status", headers=AUTH).json()["revision"]
    deleted = client.request("DELETE", f"/api/asset-registry/assets/{asset_id}", headers=AUTH,
                             json={"expected_version": registry_revision})
    assert deleted.status_code == 200
    entry_id = deleted.json()["recycle_entry_id"]
    assert client.get(f"/api/asset-registry/assets/{asset_id}", headers=AUTH).status_code == 404
    recycle = client.get("/api/asset-registry/recycle-bin", headers=AUTH).json()
    assert entry_id in [item["entry_id"] for item in recycle["items"]]

    registry_revision = client.get("/api/asset-registry/status", headers=AUTH).json()["revision"]
    restored = client.post(f"/api/asset-registry/recycle-bin/{entry_id}/restore", headers=GOVERNOR,
                           json={"expected_version": registry_revision})
    assert restored.status_code == 200
    assert client.get(f"/api/asset-registry/assets/{asset_id}", headers=AUTH).status_code == 200


def test_asset_delete_and_recycle_restore_require_registry_revision(client, assets):
    """注册表删除与回收恢复拒绝缺失/过期版本，冲突时保持状态不变。"""
    asset_id = assets[0]
    base = "/api/asset-registry"
    current = client.get(base + "/status", headers=AUTH).json()["revision"]

    missing_delete = client.request("DELETE", f"{base}/assets/{asset_id}", headers=AUTH, json={})
    assert missing_delete.status_code == 400
    stale_delete = client.request("DELETE", f"{base}/assets/{asset_id}", headers=AUTH,
                                  json={"expected_version": current + 1})
    assert stale_delete.status_code == 409
    assert client.get(f"{base}/assets/{asset_id}", headers=AUTH).status_code == 200

    deleted = client.request("DELETE", f"{base}/assets/{asset_id}", headers=AUTH,
                             json={"expected_version": current})
    assert deleted.status_code == 200, deleted.text
    entry_id = deleted.json()["recycle_entry_id"]
    current = client.get(base + "/status", headers=AUTH).json()["revision"]

    missing_restore = client.post(f"{base}/recycle-bin/{entry_id}/restore", headers=GOVERNOR, json={})
    assert missing_restore.status_code == 400
    stale_restore = client.post(f"{base}/recycle-bin/{entry_id}/restore", headers=GOVERNOR,
                                json={"expected_version": current + 1})
    assert stale_restore.status_code == 409
    assert client.get(f"{base}/assets/{asset_id}", headers=AUTH).status_code == 404

    restored = client.post(f"{base}/recycle-bin/{entry_id}/restore", headers=GOVERNOR,
                           json={"expected_version": current})
    assert restored.status_code == 200, restored.text
    assert client.get(f"{base}/assets/{asset_id}", headers=AUTH).status_code == 200


def test_a2_tags_relations_and_reference_resolve(client, assets):
    first, second = assets
    tagged = client.post("/api/asset-registry/assets/tags", headers=AUTH,
                         json={"asset_ids": [first], "names": ["角色", "主角"]})
    assert tagged.status_code == 200
    tag_ids = [t["tag_id"] for t in tagged.json()["tags"]]
    assert tag_ids == ["tag_0001", "tag_0002"]
    assert tagged.json()["assets"][0]["asset_id"] == first

    related = client.post("/api/asset-registry/assets/relations", headers=AUTH,
                          json={"asset_ids": [first, second], "relation_type": "related"})
    assert related.status_code == 200
    assert related.json()["count"] == 1
    removed = client.delete(f"/api/asset-registry/assets/{first}/relations/{second}", headers=AUTH)
    assert removed.status_code == 200
    assert client.delete(f"/api/asset-registry/assets/{first}/relations/{second}", headers=AUTH).status_code == 404

    resolved = client.post("/api/asset-registry/assets/resolve-reference", headers=AUTH,
                           json={"asset_id": first})
    assert resolved.status_code == 200 and resolved.json()["asset"]["asset_id"] == first
    assert client.post("/api/asset-registry/assets/resolve-reference", headers=AUTH,
                       json={"asset_id": "ast_9999"}).status_code == 404

    removed_tag = client.delete(f"/api/asset-registry/assets/{first}/tags/{tag_ids[0]}", headers=AUTH)
    assert removed_tag.status_code == 200
    assert removed_tag.json()["asset"]["asset_id"] == first
    assert client.delete(f"/api/asset-registry/assets/{first}/tags/{tag_ids[0]}", headers=AUTH).status_code == 404


def test_a2_image_versions_lifecycle(client, assets):
    asset_id = assets[0]
    created = client.post(f"/api/asset-registry/assets/{asset_id}/image-versions", headers=AUTH,
                          json={"edit": {"crop": {"x": 1, "y": 2, "w": 10, "h": 10}}, "canvas_id": "cv_0001"})
    assert created.status_code == 200
    version = created.json()["version"]
    assert version["id"] == "imgv_0001" and version["version_number"] == 1
    assert version["edit"]["crop"]["x"] == 1

    listed = client.get(f"/api/asset-registry/assets/{asset_id}/image-versions", headers=AUTH).json()
    assert [v["version_id"] for v in listed["versions"]] == ["imgv_0001"]

    hidden = client.patch(f"/api/asset-registry/assets/{asset_id}/image-versions/imgv_0001", headers=AUTH,
                          json={"hidden": True})
    assert hidden.status_code == 200 and hidden.json()["version"]["is_hidden"] is True
    assert client.get(f"/api/asset-registry/assets/{asset_id}/image-versions", headers=AUTH)\
        .json()["versions"][0]["is_hidden"] is True

    assert client.delete(f"/api/asset-registry/assets/{asset_id}/image-versions/imgv_0001",
                         headers=AUTH).status_code == 200
    assert client.get(f"/api/asset-registry/assets/{asset_id}/image-versions", headers=AUTH)\
        .json()["versions"] == []


def test_a2_folders_presets_and_directory_templates(client):
    folders = [client.post("/api/asset-registry/folders", headers=AUTH, json={"name": f"目录{i}"}).json()
               for i in range(2)]
    assert [f["folder"]["folder_id"] for f in folders] == ["fld_0001", "fld_0002"]
    assert len(client.get("/api/asset-registry/folders", headers=AUTH).json()["folders"]) == 2

    presets = [client.post("/api/asset-registry/presets", headers=AUTH,
                           json={"name": f"预设{i}", "definition": {"kind": "image"}}).json()
               for i in range(2)]
    assert [p["preset"]["id"] for p in presets] == ["pre_0001", "pre_0002"]
    assert len(client.get("/api/asset-registry/presets", headers=AUTH).json()["presets"]) == 2
    assert client.delete("/api/asset-registry/presets/pre_0001", headers=AUTH).status_code == 200
    assert client.delete("/api/asset-registry/presets/pre_0001", headers=AUTH).status_code == 404

    created = client.post("/api/asset-registry/project-directory-templates", headers=AUTH,
                          json={"name": "标准", "project_type": "film",
                                "directory_tree": ["01_剧本", "02_素材"]})
    assert created.status_code == 200
    template = created.json()["template"]
    assert template["id"] == "dtpl_0001" and template["folder_count"] == 2
    assert client.post("/api/asset-registry/project-directory-templates/dtpl_0001/default",
                       headers=AUTH, json={"expected_version": 1}).status_code == 200
    archived = client.post("/api/asset-registry/project-directory-templates/dtpl_0001/archive",
                           headers=AUTH, json={"expected_version": 2})
    assert archived.json()["template"]["is_archived"] is True
    assert client.patch("/api/asset-registry/project-directory-templates/dtpl_9999",
                        headers=AUTH, json={}).status_code == 404


def test_a2_project_entities_gates_and_linking(client, assets):
    first = assets[0]
    project = client.post("/api/asset-registry/projects", headers=AUTH,
                          json={"name": "实体关联项目", "project_type": "film"})
    assert project.status_code == 201, project.text
    project_id = project.json()["project"]["project_id"]
    entity = client.post(f"/api/asset-registry/projects/{project_id}/entities", headers=AUTH,
                         json={"name": "主角", "entity_type": "character"})
    assert entity.status_code == 200
    assert entity.json()["entity"]["id"] == "ent_0001"
    assert client.patch("/api/asset-registry/project-entities/ent_0001", headers=AUTH,
                        json={"status": "approved"}).status_code == 200

    linked = client.post(f"/api/asset-registry/projects/{project_id}/assets", headers=AUTH,
                         json={"asset_ids": [first]})
    assert linked.status_code == 200 and linked.json()["linked"] == 1
    assert linked.json()["assets"][0]["asset_id"] == first
    missing = client.post(f"/api/asset-registry/projects/{project_id}/assets", headers=AUTH,
                          json={"asset_ids": ["ast_9999"]})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "ASSET_NOT_FOUND"

    # QIN-488: ProjectItem 返回权威真实的关联素材数 asset_count
    projects_resp = client.get("/api/asset-registry/projects", headers=AUTH)
    assert projects_resp.status_code == 200
    matched_proj = next((p for p in projects_resp.json()["projects"] if p["project_id"] == project_id), None)
    assert matched_proj is not None
    assert matched_proj["asset_count"] == 1


def test_a2_preferences_features_and_index_automation(client):
    prefs = client.patch("/api/asset-registry/preferences/team", headers=AUTH, json={"view": "list"})
    assert prefs.status_code == 200
    assert prefs.json()["preferences"]["view"] == "list"
    assert client.get("/api/asset-registry/preferences/team", headers=AUTH)\
        .json()["preferences"]["view"] == "list"

    feature = client.patch("/api/asset-registry/settings/features/thumbnail", headers=AUTH,
                           json={"enabled": False})
    assert feature.status_code == 200 and feature.json()["feature"]["enabled"] is False
    assert client.patch("/api/asset-registry/settings/features/nope", headers=AUTH,
                        json={"enabled": True}).status_code == 404

    automation = client.patch("/api/asset-registry/settings/index-automation", headers=AUTH,
                              json={"enabled": True, "interval_minutes": 30})
    assert automation.status_code == 200
    assert automation.json()["index_automation"]["interval_minutes"] == 30
    assert client.patch("/api/asset-registry/settings/index-automation", headers=AUTH,
                        json={"interval_minutes": 0}).status_code == 400


def test_a2_cas_conflict_returns_409_with_versions(client, assets):
    current = client.get("/api/asset-registry/status", headers=AUTH).json()["revision"]
    conflict = client.patch(f"/api/asset-registry/assets/{assets[0]}", headers=AUTH,
                            json={"name": "冲突", "expected_version": 999})
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["expected_version"] == 999 and detail["current_version"] == current
    ok = client.patch(f"/api/asset-registry/assets/{assets[0]}", headers=AUTH,
                      json={"name": "正常", "expected_version": current})
    assert ok.status_code == 200


def test_a2_pdf_and_archive_exports_are_real_bytes(client, assets, tmp_path, monkeypatch):
    pdf = client.post("/api/asset-registry/assets/export-pdf", headers=AUTH,
                      json={"asset_ids": [assets[0]], "name": "资产导出"})
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    assert pdf.content.rstrip().endswith(b"%%EOF")

    # 打包下载：未配置允许根目录 / 无可打包文件 → 503，不返回空假包
    bundle = client.post("/api/asset-registry/assets/archive", headers=AUTH,
                         json={"asset_ids": [assets[0]], "name": "资产包"})
    assert bundle.status_code == 503
    assert bundle.json()["detail"]["code"] == "NO_LOCAL_FILES"


def test_a2_media_and_index_fail_closed_without_dependencies(client, assets, monkeypatch):
    asset_id = assets[0]
    media = client.get(f"/api/asset-registry/assets/{asset_id}/media", headers=AUTH)
    assert media.status_code == 503
    assert media.json()["detail"]["code"] == "MEDIA_NOT_AVAILABLE"

    monkeypatch.delenv("GW_ALLOWED_ROOTS", raising=False)
    reindex = client.post("/api/asset-registry/reindex", headers=AUTH, json={})
    assert reindex.status_code == 503
    assert reindex.json()["detail"]["code"] == "INDEX_SOURCE_NOT_AVAILABLE"
    assert client.post("/api/asset-registry/index/sync", headers=AUTH, json={}).status_code == 503


def test_a2_path_admission_fails_closed_and_never_echoes_raw_path(client, assets, tmp_path, monkeypatch):
    raw_path = str(tmp_path / "outside.png")
    denied = client.post(f"/api/asset-registry/assets/{assets[0]}/image-versions", headers=AUTH,
                         json={"source_path": raw_path})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "LOCAL_FILE_ACCESS_NOT_ADMITTED"
    assert raw_path not in denied.text

    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(tmp_path / "allowed"))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    denied2 = client.post(f"/api/asset-registry/assets/{assets[0]}/image-versions", headers=AUTH,
                          json={"source_path": str(outside)})
    assert denied2.status_code == 403
    assert denied2.json()["detail"]["code"] == "PATH_OUTSIDE_ALLOWED_ROOTS"
    assert str(outside) not in denied2.text

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    inside = allowed_dir / "inside.png"
    inside.write_bytes(b"\x89PNG\r\n\x1a\n")
    ok = client.post(f"/api/asset-registry/assets/{assets[0]}/image-versions", headers=AUTH,
                     json={"source_path": str(inside)})
    assert ok.status_code == 200
    assert ok.json()["version"]["display_path"] == "inside.png"
    assert str(allowed_dir) not in ok.text


def test_a2_index_scans_allowed_roots_for_real(client, tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    (root / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "b.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(root))
    result = client.post("/api/asset-registry/reindex", headers=AUTH, json={})
    assert result.status_code == 202
    job_id = result.json()["job_id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        polled = client.get(
            f"/api/asset-registry/workspace-jobs/{job_id}", headers=AUTH
        )
        assert polled.status_code == 200, polled.text
        job = polled.json()["job"]
        if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            break
        time.sleep(0.02)
    else:
        pytest.fail("默认后台重索引任务未及时结束")
    assert job["status"] == "succeeded", job
    assert job["result"]["scanned"] == 2
    assert job["result"]["indexed"] == 2
    assert job["progress"]["phase"] == "completed"
    assert job["attempt"] == 1 and job["max_attempts"] == 5
    assert job["progress"]["scanned"] == 2
    assert job["progress"]["error_count"] == 0
    assert job["error_code"] is None
    sync = client.post("/api/asset-registry/index/sync", headers=AUTH, json={})
    assert sync.status_code == 200
    assert sync.json()["on_disk"] == 2
    assert sync.json()["missing_from_disk"] == []


def test_a2_state_survives_restart_within_same_data_dir(client, tmp_path):
    created = client.post("/api/asset-registry/assets/import", headers=AUTH,
                          json={"items": [{"name": "重启可见.png", "kind": "image"}]})
    asset_id = created.json()["asset_ids"][0]
    with _client() as fresh:
        reloaded = fresh.get(f"/api/asset-registry/assets/{asset_id}", headers=AUTH)
        assert reloaded.status_code == 200
        assert reloaded.json()["asset"]["name"] == "重启可见.png"


def test_a2_read_only_truth_endpoints_return_real_shapes(client, assets):
    for path in (
        "/api/asset-registry",
        "/api/asset-registry/status",
        "/api/asset-registry/assets?limit=10",
        "/api/asset-registry/facets",
        "/api/asset-registry/folders",
        "/api/asset-registry/preferences/team",
        "/api/asset-registry/presets",
        "/api/asset-registry/project-directory-templates",
        "/api/asset-registry/recycle-bin",
        "/api/asset-registry/remote-assets",
    ):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200, (path, response.text)
    status = client.get("/api/asset-registry/status", headers=AUTH).json()
    assert status["assets_count"] == 2
    assert status["overview"]["features"] and status["backend"] == "json-state"


def test_a2_governance_boundaries_and_unknown_entity_404(client):
    assert client.get("/api/asset-registry/workspace-jobs/job_404", headers=AUTH).status_code == 404
    assert client.post("/api/asset-registry/workspace-jobs/job_404/cancel", headers=AUTH,
                       json={}).status_code == 404
    assert client.post("/api/asset-registry/workspace-jobs/job_404/nope", headers=AUTH,
                       json={}).status_code == 400
    assert client.post("/api/asset-registry/governance/operations", headers=GOVERNOR,
                       json={"operation": "sweep"}).status_code == 200
    assert client.post("/api/asset-registry/governance/operations", headers=GOVERNOR,
                       json={}).status_code == 400
    result = client.post("/api/asset-registry/governance/canvases/cv_0001/restore",
                         headers=GOVERNOR, json={})
    assert result.status_code == 200
    assert "canvas_store_not_connected" in result.json()["data_gaps"]


def test_directory_template_fields_survive_update_and_new_app(client):
    path = "/api/asset-registry/project-directory-templates"
    created = client.post(path, headers=AUTH, json={"name": "初始模板", "project_type": "film",
        "directory_tree": ["剧本", "素材"], "slot_mapping": {"fallback": "素材"}})
    assert created.status_code == 200, created.text
    template = created.json()["template"]
    assert template["version"] == 1 and template["name"] == "初始模板"
    updated = client.patch(path + "/" + template["template_id"], headers=AUTH, json={
        "name": "更新模板", "project_type": "short", "directory_tree": ["新素材"],
        "slot_mapping": {"fallback": "新素材"}, "expected_version": 1})
    assert updated.status_code == 200, updated.text
    assert updated.json()["template"]["version"] == 2
    with _client() as fresh:
        found = fresh.get(path, headers=AUTH).json()["templates"][0]
    assert found["name"] == "更新模板" and found["directory_tree"] == ["新素材"]
    assert found["project_type"] == "short" and found["slot_mapping"] == {"fallback": "新素材"}
    assert found["version"] == 2
    rejected = client.patch(path + "/" + template["template_id"], headers=AUTH,
                            json={"expected_version": 2, "unknown_field": 9})
    assert rejected.status_code == 400
    assert client.get(path, headers=AUTH).json()["templates"][0] == found


@pytest.mark.parametrize("operation", ["archive", "default"])
def test_directory_template_mutations_validate_real_record_version(client, operation):
    path = "/api/asset-registry/project-directory-templates"
    first = client.post(path, headers=AUTH, json={"directory_tree": ["一"]}).json()["template"]
    second = client.post(path, headers=AUTH, json={"directory_tree": ["二"]}).json()["template"]
    target = path + "/" + first["template_id"] + "/" + operation
    before = client.get(path, headers=AUTH).json()
    for invalid in (None, True, "1"):
        payload = {} if invalid is None else {"expected_version": invalid}
        assert client.post(target, headers=AUTH, json=payload).status_code == 400
    stale = client.post(target, headers=AUTH, json={"expected_version": 999})
    assert stale.status_code == 409 and stale.json()["detail"]["current_version"] == 1
    assert client.get(path, headers=AUTH).json() == before
    good = client.post(target, headers=AUTH, json={"expected_version": 1})
    assert good.status_code == 200 and good.json()["template"]["version"] == 2
    assert client.post(target, headers=AUTH, json={"expected_version": 1}).status_code == 409
    if operation == "default":
        assert client.post(path + "/" + second["template_id"] + "/default", headers=AUTH,
                           json={"expected_version": 1}).status_code == 200
        records = client.get(path, headers=AUTH).json()["templates"]
        assert records[0]["is_default"] is False and records[0]["version"] == 3
        assert records[1]["is_default"] is True and records[1]["version"] == 2


def test_asset_query_filters_and_facets_use_actual_records(client):
    path = "/api/asset-registry/assets"
    created = client.post(path + "/import", headers=AUTH, json={"items": [
        {"name": "Zulu.png", "kind": "image", "size_bytes": 20},
        {"name": "Alpha.mp4", "kind": "video", "size_bytes": 90},
        {"name": "Beta.png", "kind": "image", "size_bytes": 30}]}).json()["asset_ids"]
    tags = client.post(path + "/tags", headers=AUTH,
        json={"asset_ids": [created[0], created[2]], "names": ["主角"]}).json()["tags"]
    client.post(path + "/tags", headers=AUTH, json={"asset_ids": [created[2]], "names": ["待审"]})
    project = client.post("/api/asset-registry/projects", headers=AUTH, json={"name": "查询回归项目", "project_type": "film"})
    assert project.status_code == 201, project.text
    project_id = project.json()["project"]["project_id"]
    assert client.post(f"/api/asset-registry/projects/{project_id}/assets", headers=AUTH,
        json={"asset_ids": [created[1]]}).status_code == 200
    def ids(**params):
        response = client.get(path, headers=AUTH, params=params)
        assert response.status_code == 200, response.text
        return [item["asset_id"] for item in response.json()["assets"]]
    assert ids(tag=tags[0]["tag_id"]) == [created[0], created[2]]
    assert ids(tag="主角") == [created[0], created[2]]
    assert ids(include_tags="主角,待审") == [created[2]]
    assert ids(include_tags="主角", exclude_tags="待审") == [created[0]]
    assert ids(category="image") == [created[0], created[2]]
    assert ids(category="unknown-category") == []
    assert ids(project_id=project_id) == [created[1]]
    assert ids(sort="name") == [created[1], created[2], created[0]]
    assert ids(sort="size_desc") == [created[1], created[2], created[0]]
    assert ids(sort="oldest") == created
    assert set(ids(sort="newest")) == set(created)
    assert client.get(path, headers=AUTH, params={"sort": "invented"}).status_code == 400
    assert client.get(path, headers=AUTH, params={"view": "invented"}).status_code == 400
    facets = client.get("/api/asset-registry/facets", headers=AUTH).json()
    assert facets["categories"] == [{"name": "image", "count": 2}, {"name": "video", "count": 1}]
    assert facets["kinds"] == facets["categories"]
    assert facets["tags"][0]["count"] == 2

    # QIN-488: 验证删除素材后，项目 asset_count 同步递减
    projects = client.get("/api/asset-registry/projects", headers=AUTH).json()["projects"]
    assert next(p for p in projects if p["project_id"] == project_id)["asset_count"] == 1
    registry_revision = client.get("/api/asset-registry/status", headers=AUTH).json()["revision"]
    del_resp = client.request("DELETE", f"/api/asset-registry/assets/{created[1]}", headers=AUTH,
                              json={"expected_version": registry_revision})
    assert del_resp.status_code == 200
    projects_after = client.get("/api/asset-registry/projects", headers=AUTH).json()["projects"]
    assert next(p for p in projects_after if p["project_id"] == project_id)["asset_count"] == 0


def test_asset_categories_infer_imported_types_from_filename_and_extension_metadata(client):
    path = "/api/asset-registry/assets"
    items = [
        {"name": "无扩展名图片", "kind": "indexed", "metadata": {
            "original_filename": r"C:\catalog\frames\frame.PNG"}},
        {"name": "clip.MOV", "kind": "indexed"},
        {"name": "voice.aiff", "kind": "indexed"},
        {"name": "字幕.VTT", "kind": "indexed"},
        {"name": "checkpoint.safetensors", "kind": "indexed"},
        {"name": "brand-font", "kind": "indexed", "metadata": {"extension": ".WOFF2"}},
        {"name": "unclassified.unknown", "kind": "indexed"},
        {"name": "纯文本", "kind": "text"},
    ]
    response = client.post(path + "/import", headers=AUTH, json={"items": items})
    assert response.status_code == 200, response.text
    created = response.json()["asset_ids"]

    def ids(**params):
        result = client.get(path, headers=AUTH, params=params)
        assert result.status_code == 200, result.text
        return [item["asset_id"] for item in result.json()["assets"]]

    expected = {
        "image": [created[0]], "video": [created[1]], "audio": [created[2]],
        "document": [created[3], created[7]], "model": [created[4]],
        "font": [created[5]], "other": [created[6]],
    }
    facets = client.get("/api/asset-registry/facets", headers=AUTH).json()
    assert {item["name"]: item["count"] for item in facets["categories"]} == {
        name: len(asset_ids) for name, asset_ids in expected.items()
    }
    assert facets["kinds"] == facets["categories"]
    for category, asset_ids in expected.items():
        assert ids(category=category) == asset_ids
        assert ids(kind=category) == asset_ids


def test_asset_cursor_is_query_and_revision_bound_and_pages_without_duplicates(client):
    path = "/api/asset-registry/assets"
    created = client.post(path + "/import", headers=AUTH, json={"items": [
        {"name": name, "kind": "image"} for name in ["D.png", "A.png", "C.png", "B.png"]]}).json()["asset_ids"]
    first = client.get(path, headers=AUTH, params={"limit": 2, "sort": "name"}).json()
    assert [item["asset_id"] for item in first["assets"]] == [created[1], created[3]]
    cursor = first["next_cursor"]
    second = client.get(path, headers=AUTH, params={"limit": 2, "sort": "name", "cursor": cursor})
    assert second.status_code == 200, second.text
    assert [item["asset_id"] for item in second.json()["assets"]] == [created[2], created[0]]
    assert second.json()["next_cursor"] is None
    assert client.get(path, headers=AUTH, params={"sort": "size_desc", "cursor": cursor}).status_code == 400
    assert client.get(path, headers=AUTH, params={"sort": "name", "cursor": cursor, "offset": 3}).status_code == 400
    for bad in ["not-base64", "bnVsbA", "W10"]:
        assert client.get(path, headers=AUTH, params={"cursor": bad}).status_code == 400
    assert client.patch(path + "/" + created[0], headers=AUTH, json={"name": "新名字"}).status_code == 200
    stale = client.get(path, headers=AUTH, params={"sort": "name", "cursor": cursor})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_asset_recent_filter_and_unimplemented_scopes_are_not_silently_ignored(client):
    from gw.asset_registry import repository as repo
    path = "/api/asset-registry/assets"
    created = client.post(path + "/import", headers=AUTH, json={"items": [
        {"name": "旧条目"}, {"name": "当前条目"}]}).json()["asset_ids"]
    def age_record(raw):
        raw["assets"][created[0]]["created_at"] = "2000-01-01T00:00:00Z"
    repo.state().mutate(age_record)
    result = client.get(path, headers=AUTH, params={"recent_days": 7})
    assert result.status_code == 200
    assert [a["asset_id"] for a in result.json()["assets"]] == [created[1]]
    for days in (0, -1, 36501):
        assert client.get(path, headers=AUTH, params={"recent_days": days}).status_code == 400
    unknown_root = client.get(path, headers=AUTH, params={"root_id": "unknown"})
    assert unknown_root.status_code == 404
    assert unknown_root.json()["detail"]["code"] == "ROOT_NOT_FOUND"
    unknown_collection = client.get(path, headers=AUTH, params={"collection_id": "unknown"})
    assert unknown_collection.status_code == 200
    assert unknown_collection.json()["assets"] == []


def test_project_recycle_restore_uses_project_state_machine_and_cas(client):
    base = "/api/asset-registry"
    created = client.post(base + "/projects", headers=AUTH, json={"name": "回收恢复", "project_type": "film"})
    assert created.status_code == 201, created.text
    project_id = created.json()["project"]["project_id"]
    target = base + f"/project-recycle/{project_id}/restore"
    assert client.post(target, headers=GOVERNOR, json={"expected_version": 1}).status_code == 409
    assert client.request("DELETE", base + f"/projects/{project_id}", headers=GOVERNOR,
                          json={"expected_version": 1}).status_code == 200
    assert client.post(base + f"/projects/{project_id}/trash", headers=GOVERNOR,
                       json={"expected_version": 2}).status_code == 200
    assert client.post(target, json={"expected_version": 3}).status_code == 401
    assert client.post(target, headers=READONLY, json={"expected_version": 3}).status_code == 403
    for value in (None, True, "3", -1):
        assert client.post(target, headers=GOVERNOR, json={"expected_version": value}).status_code == 400
    assert client.post(base + "/project-recycle/nonexistent/restore", headers=GOVERNOR,
                       json={"expected_version": 3}).status_code == 404
    stale = client.post(target, headers=GOVERNOR, json={"expected_version": 2})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "VERSION_CONFLICT"
    read = client.get(f"/api/projects/{project_id}", headers=AUTH).json()["project"]
    assert read["deleted_at"] is not None and read["version"] == 3
    restored = client.post(target, headers=GOVERNOR, json={"expected_version": 3})
    assert restored.status_code == 200, restored.text
    assert restored.json()["restored"] is True and restored.json()["version"] == 4
    read = client.get(f"/api/projects/{project_id}", headers=AUTH).json()["project"]
    assert read["deleted_at"] is None and read["archived_at"] is None and read["version"] == 4
    assert client.post(target, headers=GOVERNOR, json={"expected_version": 4}).status_code == 409
