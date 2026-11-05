"""DIR-T1 浏览器验收：新建资产库、等待索引并按来源筛选。"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect
from workflow_support import goto_ready, settings_browser


def _reindex_post(response) -> bool:
    path = response.url.split("?", 1)[0].rstrip("/")
    return response.request.method == "POST" and path.endswith("/api/asset-registry/reindex")


def _storage_settings_get(response) -> bool:
    return response.request.method == "GET" and "/api/storage-settings" in response.url


def _click_asset_tree_board(vault, board_id: str, *, force_project_toggle: bool = False) -> None:
    root_class = vault.locator("#assetManagerRoot").get_attribute("class") or ""
    if "asset-manager-root-local" in root_class:
        vault.locator("[data-localup-back-to-tree]").click()
        vault.locator("#assetManagerRoot.asset-manager-root-page-registry").wait_for(timeout=15000)
    if board_id == "registry":
        vault.locator('[data-asset-tree-crumb="registry"]').click()
        return
    if board_id == "governance":
        _click_asset_tree_board(vault, "registry")
        vault.locator("[data-governance-open]").click()
        drawer = vault.locator("[data-governance-drawer-dialog]")
        expect(drawer).to_be_visible()
        drawer.locator("[data-governance-drawer-close]").click()
        return
    if board_id in {"projects", "scripts", "canvas-assets", "prompts"}:
        tab = vault.locator('[data-asset-tree-tab="system"]')
        root_class = vault.locator("#assetManagerRoot").get_attribute("class") or ""
        if "asset-manager-root-local" in root_class or tab.get_attribute("aria-selected") != "true":
            tab.click()
        tree_key = "system:projects" if board_id == "projects" else f"system:{board_id}"
    else:
        tab = vault.locator('[data-asset-tree-tab="mine"]')
        root_class = vault.locator("#assetManagerRoot").get_attribute("class") or ""
        if "asset-manager-root-local" in root_class or tab.get_attribute("aria-selected") != "true":
            tab.click()
        tree_key = f"mine:board:{board_id}"
    entry = vault.locator(f'[data-asset-tree-node="{tree_key}"]')
    entry.wait_for(state="visible", timeout=15000)
    if board_id == "projects":
        branch = vault.locator('[data-asset-tree-node-key="system:projects"]')
        if force_project_toggle and branch.get_attribute("aria-expanded") == "true":
            entry.click()
            expect(branch).to_have_attribute("aria-expanded", "false")
        if branch.get_attribute("aria-expanded") != "true":
            entry.click()
        expect(branch).to_have_attribute("aria-expanded", "true")
        expect(branch.locator(":scope > .asset-tree-children")).to_be_visible()
        expect(vault.locator('[data-asset-tree-node-key^="system:projects:"]').first).to_be_visible()
        return
    try:
        entry.click(timeout=5000)
    except PlaywrightTimeout as exc:
        layout = entry.evaluate("""row => {
            const rect = element => element?.getBoundingClientRect().toJSON() || null;
            const panel = row.closest('.asset-tree-panel');
            const root = row.closest('#assetManagerRoot');
            const bounds = row.getBoundingClientRect();
            const x = bounds.left + bounds.width / 2;
            const y = bounds.top + bounds.height / 2;
            return {
                row: rect(row), panel: rect(panel), root: rect(root),
                children: [...(root?.children || [])].map(child => ({className:child.className, rect:rect(child), gridRow:getComputedStyle(child).gridRow})),
                hitStack: document.elementsFromPoint(x, y).slice(0, 5).map(element => `${element.tagName}.${element.className}`),
            };
        }""")
        raise AssertionError(f"单栏树入口 {tree_key} 不可点击：{layout}") from exc


def _click_asset_tree_root(vault, root_id: str):
    tab = vault.locator('[data-asset-tree-tab="mine"]')
    root_class = vault.locator("#assetManagerRoot").get_attribute("class") or ""
    if "asset-manager-root-local" in root_class:
        vault.locator("[data-localup-back-to-tree]").click()
        vault.locator("#assetManagerRoot.asset-manager-root-page-registry").wait_for(timeout=15000)
    if tab.get_attribute("aria-selected") != "true":
        tab.click()
    entry = vault.locator(f'[data-asset-tree-node="mine:root:{root_id}"]')
    entry.wait_for(state="visible", timeout=15000)
    return entry


def _dump_confirm_failure(vault, errors, traffic) -> str:
    status_text = ""
    index_state = ""
    path_value = "<missing>"
    try:
        status = vault.locator("#assetStatus")
        if status.count():
            status_text = status.inner_text(timeout=2000)
            index_state = status.get_attribute("data-gw-index-state") or ""
    except Exception as exc:
        status_text = f"<unreadable: {exc}>"
    try:
        path = vault.locator("#boardCreatePath")
        if path.count():
            path_value = path.input_value(timeout=2000)
        else:
            path_value = "<input missing>"
    except Exception as exc:
        path_value = f"<unreadable: {exc}>"
    return (
        f"status={status_text!r} index_state={index_state!r} path={path_value!r} "
        f"errors={errors!r} traffic={traffic!r}"
    )


def test_dir_t1_create_library_index_and_filter_from_browser(monkeypatch, tmp_path: Path):
    allowed = tmp_path / "允许根"
    library = allowed / "浏览器测试素材库"
    library.mkdir(parents=True)
    (library / "浏览器素材-甲.txt").write_text("浏览器真实索引", encoding="utf-8")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", str(allowed))

    with settings_browser(monkeypatch, tmp_path) as (page, base, errors, _):
        project_response = page.context.request.post(
            base + "/api/asset-registry/projects",
            data={"name": "DIR-T1 树展开验证项目", "project_type": "other"},
            headers={"Origin": base},
        )
        assert project_response.status == 201, project_response.text()
        bad_asset_responses = []
        bootstrap = {"facets": False}
        page.on(
            "response",
            lambda response: bad_asset_responses.append(response.url)
            if "/api/asset-registry/assets?" in response.url and response.status == 400
            else None,
        )
        page.on(
            "response",
            lambda response: bootstrap.__setitem__("facets", True)
            if "/api/asset-registry/facets" in response.url
            else None,
        )
        goto_ready(page, base + "/static/pages/assets.html")
        vault = page.frame_locator("#assetVaultFrame")
        create_button = page.locator("#assetSidebarCreate")
        create_button.wait_for(state="visible")
        # 首屏会在一次 networkidle 之后再延迟请求 facets。确认建库前先等这轮结束，
        # 否则 storage-settings / reindex 会排在同步扫描后面，30s 内等不到 POST 响应。
        deadline = time.monotonic() + 30
        while not bootstrap["facets"] and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert bootstrap["facets"], "首屏 /api/asset-registry/facets 未返回"
        page.wait_for_load_state("networkidle", timeout=20_000)

        create_button.click()
        expect(vault.locator("#boardCreateName")).to_be_visible()
        assert vault.locator("[data-board-preset]").count() == 0
        vault.locator("#boardCreatePath").fill(str(library))
        expect(vault.locator("#boardCreateName")).to_have_value("浏览器测试素材库")
        vault.locator("#boardCreateName").fill("DIR-T1 中文素材库")
        vault.locator("#boardCreatePath").fill(str(library))
        expect(vault.locator("#boardCreateName")).to_have_value("DIR-T1 中文素材库")
        expect(vault.locator("#boardCreatePath")).to_have_value(str(library))

        traffic = []

        def _track(response):
            url = response.url
            if "/api/storage-settings" in url or "/api/asset-registry/reindex" in url:
                traffic.append(f"{response.request.method} {response.status} {url}")

        def _track_failed(request):
            url = request.url
            if "/api/storage-settings" in url or "/api/asset-registry/reindex" in url:
                traffic.append(f"FAILED {request.method} {url} {request.failure}")

        page.on("response", _track)
        page.on("requestfailed", _track_failed)
        frame_handle = page.locator("#assetVaultFrame").element_handle()
        frame = frame_handle.content_frame() if frame_handle else None
        assert frame is not None, "资产库 iframe 未挂载"
        try:
            # Playwright 的 Frame 没有 expect_response，由 page 接收 iframe 内的响应。
            # 确认点击落在当前 frame，避免 iframe 重载后点到另一份文档。
            with page.expect_response(_storage_settings_get, timeout=60_000) as settings_got, page.expect_response(_reindex_post, timeout=60_000) as registration:
                frame.locator("[data-board-create-confirm]").click()
        except PlaywrightTimeout as exc:
            raise AssertionError(
                "确认建库后未观察到 storage-settings 或 reindex 响应。"
                + _dump_confirm_failure(vault, errors, traffic)
            ) from exc
        assert settings_got.value.ok, settings_got.value.status
        response = registration.value
        assert response.status == 202, response.text()
        payload = response.request.post_data_json
        root_id = payload["register_root"]["id"]
        assert payload["register_root"]["path"] == str(library)
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job_response = page.context.request.get(base + f"/api/asset-registry/workspace-jobs/{job_id}")
            assert job_response.status == 200, job_response.text()
            job = job_response.json()["job"]
            if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            page.wait_for_timeout(100)
        assert job["status"] == "succeeded", job

        indexed = page.context.request.get(base + f"/api/asset-registry/assets?root_id={root_id}")
        assert indexed.status == 200, indexed.text()
        asset_ids = [item["asset_id"] for item in indexed.json()["assets"]]
        assert [item["name"] for item in indexed.json()["assets"]] == ["浏览器素材-甲.txt"]
        # 页面若启动在项目上下文，关联同一真实素材，避免项目范围掩盖来源筛选结果。
        projects = page.context.request.get(base + "/api/asset-registry/projects")
        assert projects.status == 200, projects.text()
        for project in projects.json().get("projects", []):
            linked = page.context.request.post(
                base + f"/api/asset-registry/projects/{project['project_id']}/assets",
                data={"asset_ids": asset_ids},
                headers={"Origin": base},
            )
            assert linked.status == 200, linked.text()

        # 建库完成时默认项目范围内的空列表可能已写入查询缓存，且发生在关联之前。
        # 刷新会清掉这份缓存；再打开看板才会按已关联的项目重新请求。
        with page.expect_response(lambda item: "/api/asset-registry/assets?" in item.url, timeout=20000):
            vault.locator("[data-refresh-assets]").click()
        overview_url = page.url
        overview_stats = vault.locator(".registry-overview-stats")
        overview_toolbar = vault.locator(".registry-toolbar")
        expect(overview_stats).to_be_visible()
        expect(overview_toolbar).to_be_visible()
        def position(locator):
            return locator.evaluate("""element => {
                const rect = element.getBoundingClientRect();
                const root = document.querySelector('#assetManagerRoot').getBoundingClientRect();
                return {x:rect.x - root.x,y:rect.y - root.y};
            }""")

        def wait_for_fixed_rows(expected, board_id: str, viewport: str):
            try:
                frame.evaluate("delete window.__assetManagerFixedRowsProbe")
                frame.wait_for_function(
                    """({expected, boardId}) => {
                        const root = document.querySelector('#assetManagerRoot');
                        const rows = [
                            root?.querySelector(':scope > .registry-overview-shell'),
                            root?.querySelector(':scope > .registry-toolbar')
                        ];
                        const hasBoardClass = root?.classList.contains(`asset-manager-root-page-${boardId}`)
                            || root?.classList.contains(`asset-manager-root-${boardId}`);
                        if (!root || !rows.every(Boolean) || !hasBoardClass) {
                            delete window.__assetManagerFixedRowsProbe;
                            return false;
                        }
                        const rootRect = root.getBoundingClientRect();
                        const positions = rows.map(element => {
                            const rect = element.getBoundingClientRect();
                            return {x:rect.x - rootRect.x, y:rect.y - rootRect.y};
                        });
                        const now = performance.now();
                        const previous = window.__assetManagerFixedRowsProbe;
                        const stable = previous?.boardId === boardId && positions.every((position, index) =>
                            Math.abs(position.x - previous.positions[index].x) <= 0.25
                            && Math.abs(position.y - previous.positions[index].y) <= 0.25
                        );
                        const stableSince = stable ? previous.stableSince : now;
                        window.__assetManagerFixedRowsProbe = {boardId, positions, stableSince};
                        if (now - stableSince < 200) return false;
                        return positions.every((position, index) =>
                            Math.abs(position.x - expected[index].x) <= 1
                            && Math.abs(position.y - expected[index].y) <= 1
                        );
                    }""",
                    arg={"expected": expected, "boardId": board_id},
                    timeout=10000,
                )
            except PlaywrightTimeout as exc:
                layout = vault.locator("#assetManagerRoot").evaluate("""root => ({
                    className:root.className,
                    columns:getComputedStyle(root).gridTemplateColumns,
                    rows:[...root.querySelectorAll(':scope > .registry-overview-shell, :scope > .registry-toolbar')]
                        .map(element => ({className:element.className,rect:element.getBoundingClientRect().toJSON()}))
                })""")
                raise AssertionError(f"{viewport}切换 {board_id} 后固定栏渲染稳定时与基准位置不符：{layout}") from exc
            finally:
                frame.evaluate("delete window.__assetManagerFixedRowsProbe")

        overview_stats_box = position(vault.locator("#assetManagerRoot > .registry-overview-shell"))
        overview_toolbar_box = position(overview_toolbar)
        library_board = frame.evaluate("""() => {
            const state = JSON.parse(localStorage.getItem('asset_board_navigation_v1') || '{}');
            return (state.customBoards || []).find(board => board.name === 'DIR-T1 中文素材库') || null;
        }""")
        assert library_board and library_board["id"].startswith("library_"), library_board
        _click_asset_tree_board(vault, library_board["id"])
        expect(vault.locator(".registry-overview-stats")).to_be_visible()
        expect(vault.locator(".registry-toolbar")).to_be_visible()
        expect(vault.locator("#assetManagerRoot")).to_have_class(re.compile("asset-manager-root-library"))
        assert page.url == overview_url, "切换资源库不应导航离开当前页面"
        wait_for_fixed_rows((overview_stats_box, overview_toolbar_box), "library", "桌面自定义库")
        vault.locator("[data-registry-card]").filter(has_text="浏览器素材-甲.txt").wait_for(timeout=15000)
        collapsed_layout = vault.locator("#assetManagerRoot").evaluate("""root => {
            const content = root.querySelector('.registry-content').getBoundingClientRect();
            const detail = root.querySelector('.registry-detail-panel').getBoundingClientRect();
            const bounds = root.getBoundingClientRect();
            return {
                width: root.style.getPropertyValue('--registry-detail-width'),
                contentGap: detail.left - content.right,
                rightGap: bounds.right - detail.right,
            };
        }""")
        assert collapsed_layout["width"] == "44px", collapsed_layout
        assert abs(collapsed_layout["contentGap"]) <= 1, collapsed_layout
        assert abs(collapsed_layout["rightGap"]) <= 1, collapsed_layout

        _click_asset_tree_board(vault, "registry")
        with page.expect_response(lambda item: "/api/asset-registry/assets?" in item.url
                                  and f"root_id={root_id}" in item.url) as filtered:
            _click_asset_tree_root(vault, root_id).click()
        assert filtered.value.status == 200, filtered.value.text()
        vault.locator("[data-registry-card]").filter(has_text="浏览器素材-甲.txt").wait_for(timeout=15000)
        fixed_url = page.url
        fixed_stats = vault.locator("#assetManagerRoot > .registry-overview-shell")
        fixed_toolbar = vault.locator("#assetManagerRoot > .registry-toolbar")
        expect(fixed_stats).to_be_visible()
        expect(fixed_toolbar).to_be_visible()
        reference_boxes = (position(fixed_stats), position(fixed_toolbar))
        for board_id in ("registry", "assets", "scripts", "prompts", "canvas-assets", "local"):
            _click_asset_tree_board(vault, board_id)
            expect(vault.locator("#assetManagerRoot")).to_have_class(re.compile(f"asset-manager-root-page-{board_id}"))
            expect(fixed_stats).to_be_visible()
            expect(fixed_toolbar).to_be_visible()
            assert page.url == fixed_url, f"切换 {board_id} 不应导航离开当前页面"
            wait_for_fixed_rows(reference_boxes, board_id, "桌面")
        for board_id, route_name in (("projects", "项目树"), ("governance", "治理建议")):
            _click_asset_tree_board(vault, board_id, force_project_toggle=board_id == "projects")
            assert page.url == fixed_url, f"打开 {route_name} 不应导航离开当前页面"
            wait_for_fixed_rows(reference_boxes, "registry", f"桌面{route_name}")
        page.set_viewport_size({"width": 500, "height": 900})
        _click_asset_tree_board(vault, "registry")
        expect(fixed_stats).to_be_visible()
        expect(fixed_toolbar).to_be_visible()
        narrow_reference = (position(fixed_stats), position(fixed_toolbar))
        for board_id in ("assets", "scripts", "prompts", "canvas-assets", "local"):
            _click_asset_tree_board(vault, board_id)
            expect(fixed_stats).to_be_visible()
            expect(fixed_toolbar).to_be_visible()
            assert page.url == fixed_url, f"窄屏切换 {board_id} 不应导航离开当前页面"
            expect(vault.locator("#assetManagerRoot")).to_have_class(re.compile(f"asset-manager-root-page-{board_id}"))
            expect(vault.locator(".asset-tree-panel")).to_be_visible()
            wait_for_fixed_rows(narrow_reference, board_id, "窄屏")
        for board_id, route_name in (("projects", "项目树"), ("governance", "治理建议")):
            _click_asset_tree_board(vault, board_id, force_project_toggle=board_id == "projects")
            assert page.url == fixed_url, f"窄屏打开 {route_name} 不应导航离开当前页面"
            wait_for_fixed_rows(narrow_reference, "registry", f"窄屏{route_name}")
        assert bad_asset_responses == []
        assert errors == []
