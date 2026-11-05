"""QIN-246：验证上传资源首屏分页、失败重试与增量加载。"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from gw.asset_library import repository as local_repo
from workflow_support import goto_ready, settings_browser


def test_local_assets_show_error_retry_and_load_next_page(
    monkeypatch, tmp_path
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed))
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "data"))
    paths = []
    for index in range(101):
        path = allowed / f"asset-{index:03}.txt"
        path.write_text("fixture", encoding="utf-8")
        paths.append(str(path))
    local_repo.index_local_files(paths, "local")

    with settings_browser(monkeypatch, tmp_path) as (page, base, errors, _):
        project_response = page.context.request.post(
            base + "/api/asset-registry/projects",
            data={"name": "QIN-560 返回筛选回归项目", "project_type": "other"},
            headers={"Origin": base},
        )
        assert project_response.status == 201, project_response.text()
        project_id = project_response.json()["project"]["project_id"]
        failures = {"remaining": 1}

        def fail_once(route):
            if failures["remaining"]:
                failures["remaining"] -= 1
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps(
                        {"detail": {"code": "TEMPORARY", "message": "暂时不可用"}},
                        ensure_ascii=False,
                    ),
                )
            else:
                route.continue_()

        page.route("**/api/local-assets?*", fail_once)
        goto_ready(page, base + "/static/pages/assets.html")
        vault = page.frame_locator("#assetVaultFrame")
        system_tab = vault.locator('[data-asset-tree-tab="system"]')
        if system_tab.get_attribute("aria-selected") != "true":
            system_tab.click()
        project_branch = vault.locator('[data-asset-tree-node="system:projects"]')
        branch_state = vault.locator('[data-asset-tree-node-key="system:projects"]')
        project_branch.wait_for(state="visible")
        if branch_state.get_attribute("aria-expanded") != "true":
            project_branch.click()
        project_entry = vault.locator(f'[data-asset-tree-node="system:projects:{project_id}"]')
        project_entry.wait_for(state="visible")
        project_entry.click()
        filtered_project = vault.locator(f'[data-asset-tree-node-key="system:projects:{project_id}"]')
        filtered_project.wait_for(state="visible")
        assert filtered_project.get_attribute("aria-selected") == "true"
        assert vault.locator('[data-registry-clear="project"]').is_visible()

        vault.locator('[data-asset-tree-tab="mine"]').click()
        local_entry = vault.locator('[data-asset-tree-node="mine:board:local"]')
        local_entry.wait_for(state="visible")
        with page.expect_response(
            lambda response: "/api/local-assets?" in response.url
            and response.request.method == "GET"
        ) as failed_page:
            local_entry.click()
        assert failed_page.value.status == 503
        assert vault.locator("[data-localup-back-to-tree]").is_visible()
        assert vault.locator("[data-asset-tree-tab]").count() == 0
        vault.locator('[data-localup-retry]').wait_for(state="visible")

        with page.expect_response(
            lambda response: "/api/local-assets?" in response.url
            and response.request.method == "GET"
            and response.status == 200
        ) as first_page:
            vault.locator('[data-localup-retry]').click()
        params = parse_qs(urlparse(first_page.value.url).query)
        assert params["limit"] == ["100"]
        assert len(first_page.value.json()["items"]) == 100
        vault.locator('[data-localup-load-more]').wait_for(state="visible")
        assert vault.get_by_text("已加载 100 / 101 个素材").is_visible()

        with page.expect_response(
            lambda response: "/api/local-assets?" in response.url
            and response.request.method == "GET"
            and response.status == 200
            and "cursor=" in response.url
        ) as second_page:
            vault.locator('[data-localup-load-more]').click()
        params = parse_qs(urlparse(second_page.value.url).query)
        assert params["limit"] == ["100"]
        assert params["cursor"]
        assert len(second_page.value.json()["items"]) == 1
        vault.get_by_text("已加载全部 101 个素材").wait_for(state="visible")
        vault.locator("[data-localup-back-to-tree]").click()
        vault.locator("#assetManagerRoot.asset-manager-root-page-registry").wait_for(timeout=15000)
        assert vault.locator('[data-asset-tree-tab="mine"]').get_attribute("aria-selected") == "true"
        assert vault.locator('[data-registry-clear="project"]').count() == 0
        assert vault.locator("[data-asset-tree-node-key].is-selected").count() == 0
        assert errors == []
