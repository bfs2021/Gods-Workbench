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
        vault.locator('[data-asset-tree-tab="mine"]').click()
        local_entry = vault.locator('[data-asset-tree-node="mine:board:local"]')
        local_entry.wait_for(state="visible")
        with page.expect_response(
            lambda response: "/api/local-assets?" in response.url
            and response.request.method == "GET"
        ) as failed_page:
            local_entry.click()
        assert failed_page.value.status == 503
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
        assert errors == []
