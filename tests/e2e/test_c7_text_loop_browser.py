"""C7-P3：通过真实浏览器回读文本链六阶段产物与精确导出版本。"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright_support import open_playwright

from gw.api.agent_integration import build_agent_integration
from gw.api.app import create_app
from gw.core import local_accounts, session
from gw.core.auth import AuthContext
from gw.core.runtime_paths import resolve_runtime_paths
from gw.projects_hub.models import ProjectCreateRequest, ProjectType
from gw.projects_hub.service import ProjectsService, owner_key_for_context

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FakeGateway:
    """隔离测试网关：浏览器证据仍走真实 HTTP/UI，不触发外部模型调用。"""

    calls: list[Any]

    async def describe_capabilities(self, model_ref):
        from gw.agent_runtime.models import MessageRole, ModelCapabilities

        return ModelCapabilities(
            model_ref=model_ref,
            supported_roles=list(MessageRole),
            structured_output=True,
            tool_calls=False,
            streaming=False,
            multimodal=False,
            lookup=False,
            cancellation=False,
            usage_reporting=False,
            max_context_tokens=None,
        )

    async def invoke(self, request):
        from gw.agent_runtime.models import FinishReason, ModelInvocationResult

        self.calls.append(request)
        return ModelInvocationResult(
            content="synthetic browser fixture response",
            tool_calls=[],
            finish_reason=FinishReason.stop,
            upstream_request_id=None,
            usage=None,
            usage_unavailable_reason=None,
        )


def _contract_seed_helper():
    """复用已批准的 C7-T1 精确版本夹具，避免在 e2e 中复制领域写入逻辑。"""

    path = ROOT / "tests" / "contracts" / "agent" / "test_agent_integration.py"
    spec = importlib.util.spec_from_file_location("c7_agent_contract_helpers", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载文本链夹具：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _evidence_dir(tmp_path: Path) -> Path:
    configured = os.environ.get("GW_C7_P3_EVIDENCE_DIR", "").strip()
    target = Path(configured) if configured else tmp_path / "c7-p3-evidence"
    if not target.is_absolute():
        target = ROOT / target
    target.mkdir(parents=True, exist_ok=True)
    return target


def _wait_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "隔离 FastAPI 服务未启动"


def _launch_browser(chromium: Any, browser_error: type[BaseException]):
    """优先使用系统 Chrome，CI 没有时回退到 Playwright 自带 Chromium。"""

    try:
        return chromium.launch(channel="chrome", headless=True)
    except browser_error as chrome_error:
        try:
            return chromium.launch(headless=True)
        except browser_error as bundled_error:
            raise RuntimeError(
                "系统 Chrome 与 Playwright Chromium 均不可用；"
                f"Chrome={type(chrome_error).__name__}: {chrome_error}; "
                f"Chromium={type(bundled_error).__name__}: {bundled_error}"
            ) from bundled_error


def test_c7_p3_browser_launcher_falls_back_to_playwright_chromium():
    """系统 Chrome 不存在时，必须使用 Playwright 自带 Chromium。"""

    class BrowserLaunchError(Exception):
        pass

    bundled_browser = object()
    calls: list[dict[str, Any]] = []

    class ChromiumStub:
        def launch(self, **options):
            calls.append(options)
            if options.get("channel") == "chrome":
                raise BrowserLaunchError("system Chrome missing")
            return bundled_browser

    assert _launch_browser(ChromiumStub(), BrowserLaunchError) is bundled_browser
    assert calls == [{"channel": "chrome", "headless": True}, {"headless": True}]


def _seed_export_records_in_worker(
    helper, integration, run_id: str, stages: list[dict[str, str]]
):
    """Playwright 同步 API 持有事件循环时，把异步夹具放到独立线程执行。"""

    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def seed() -> None:
        try:
            result["review_ids"] = helper._seed_export_test_records(
                integration,
                run_id,
                mode="approval",
                records=stages,
                current_labels=[item["label"] for item in stages],
                released_labels=[item["label"] for item in stages],
            )
        except BaseException as exc:  # 让主线程保留原始失败堆栈
            errors.append(exc)

    worker = threading.Thread(target=seed, name="c7-p3-seed", daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "文本链精确版本夹具线程未结束"
    if errors:
        raise errors[0]
    return result["review_ids"]


def test_c7_p3_text_loop_real_browser_readback_and_export(monkeypatch, tmp_path):
    """C7-AC1/AC4：Chrome 创建任务、回读六阶段版本、刷新恢复并导出。"""

    browser_api = pytest.importorskip("playwright.sync_api")
    data_root = tmp_path / "data"
    auth_db = tmp_path / "auth" / "auth.sqlite3"
    video_root = tmp_path / "video"
    monkeypatch.setenv("GW_RUNTIME_MODE", "test")
    monkeypatch.setenv("GW_DATA_DIR", str(data_root))
    monkeypatch.setenv("GW_LOCAL_AUTH_DB", str(auth_db))
    monkeypatch.setenv("GW_VIDEO_DATA_DIR", str(video_root))
    monkeypatch.setenv("GW_AUTH_MODE", "local_account")
    monkeypatch.setenv("GW_DISABLE_AGENT_INTEGRATION", "0")
    monkeypatch.setenv("GW_AGENT_MAX_MODEL_CALLS_PER_RUN", "2")
    monkeypatch.setenv("GW_CLI_EXECUTION", "0")
    monkeypatch.setenv("GW_ALLOWED_ROOTS", "")

    session.reset_stores()
    principal, _session_token = local_accounts.create_first_admin(
        "c7browserowner", "C7Browser2026"
    )
    auth = AuthContext(
        role=principal["role"],
        subject=principal["user_id"],
        mode="local_account",
        identity_domain="local_account",
    )
    projects = ProjectsService(seed_golden_fixture=False, persistent=True)
    project = projects.create_project(
        ProjectCreateRequest(name="C7 文本链浏览器证据", project_type=ProjectType.FILM),
        owner_key=owner_key_for_context(auth),
    )

    # 浏览器页面通过项目列表接口读取同一个持久服务实例。
    from gw.api import routes_asset_registry, routes_projects
    from gw.projects_hub import service as projects_module

    monkeypatch.setattr(projects_module, "default_projects_service", projects)
    monkeypatch.setattr(routes_asset_registry, "default_projects_service", projects)
    monkeypatch.setattr(routes_projects, "default_projects_service", projects)

    gateway = FakeGateway(calls=[])
    config = {
        "configured": True,
        "default_provider_id": "fake-provider",
        "providers": [
            {
                "provider_id": "fake-provider",
                "configured": True,
                "models": ["fake-model"],
            }
        ],
    }

    def model_resolver(provider_id: str | None, model: str | None):
        if (provider_id or "fake-provider", model or "fake-model") != (
            "fake-provider",
            "fake-model",
        ):
            raise ValueError("not in browser fixture allowlist")
        return {"provider_id": "fake-provider", "model": "fake-model"}

    integration = build_agent_integration(
        gateway=gateway,
        runtime_paths=resolve_runtime_paths(),
        projects=projects,
        model_resolver=model_resolver,
        configuration_provider=lambda: config,
        start_worker=False,
    )
    app = create_app(agent_integration_factory=integration)
    # lifespan=off 用于保留测试网关与同一个 SQLite integration；路由仍走真实 app.state。
    app.state.agent_integration = integration

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    evidence = _evidence_dir(tmp_path)

    try:
        _wait_server(server, thread)
        with open_playwright(browser_api) as pw:
            try:
                browser = _launch_browser(pw.chromium, browser_api.Error)
            except RuntimeError as exc:
                pytest.skip(
                    f"Chrome 与 Playwright Chromium 均不可用，浏览器证据未取得：{exc}"
                )
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if route.request.url.startswith(base + "/")
                        else route.abort()
                    ),
                )
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                # 将真实本地账户会话注入浏览器 Cookie；后续页面/API 均由浏览器真实认证链路处理。
                context.add_cookies(
                    [
                        {
                            "name": "gw_session",
                            "value": _session_token,
                            "url": base,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                page.goto(
                    base + "/static/pages/agents.html", wait_until="domcontentloaded"
                )
                page.wait_for_function(
                    "window.HardwareDeck?.authState?.authenticated === true"
                )

                # QIN-506：项目下拉框只有一个加载/赋值负责人，默认不预选任何项目。
                page.wait_for_function(
                    "() => (document.querySelector('#agentTaskProject')?.options.length || 0) > 1",
                    timeout=15000,
                )
                assert (
                    page.eval_on_selector("#agentTaskProject", "el => el.value") == ""
                ), "无 URL project_id 时项目下拉框必须保持未选中"
                # 真实用户行为：等待选项加载后，通过下拉框主动选择测试项目。
                page.select_option("#agentTaskProject", project.project_id)
                page.wait_for_function(
                    "(expected) => document.querySelector('#agentTaskProject')?.value === expected",
                    arg=project.project_id,
                    timeout=15000,
                )
                page.fill(
                    "#agentTaskGoal", "完成一个可审核、可导出的六阶段文本链浏览器证据"
                )
                page.click("#agentTaskCreate")
                page.wait_for_function(
                    "document.querySelector('#agentTaskStatus')?.textContent.includes('任务已接受')",
                    timeout=15000,
                )
                job_id = page.locator("#agentTaskRunId").input_value()
                assert job_id.startswith("job_agent_"), job_id
                job_response = context.request.get(base + f"/api/jobs/{job_id}")
                assert job_response.status == 200, job_response.text()
                run_id = job_response.json()["run_id"]

                # 使用既有 C7-T1 精确版本夹具推进六阶段，之后所有回读仍由真实页面发起。
                helper = _contract_seed_helper()
                stages = [
                    {"label": "stage1-brief", "stage": "brief", "approval": "approve"},
                    {
                        "label": "stage2-outline",
                        "stage": "outline_characters",
                        "approval": "approve",
                    },
                    {
                        "label": "stage3-script",
                        "stage": "full_script",
                        "approval": "approve",
                    },
                    {
                        "label": "stage4-scene",
                        "stage": "scene_script",
                        "approval": "approve",
                    },
                    {
                        "label": "stage5-storyboard",
                        "stage": "storyboard_text",
                        "approval": "approve",
                    },
                    {
                        "label": "stage6-delivery",
                        "stage": "delivery_check",
                        "approval": "approve",
                    },
                ]
                review_ids = _seed_export_records_in_worker(
                    helper, integration, run_id, stages
                )
                assert set(review_ids) == {item["label"] for item in stages}

                page.wait_for_function(
                    "document.querySelector('#agentTaskStatus')?.textContent.includes('succeeded')",
                    timeout=30000,
                )
                page.wait_for_function(
                    "document.querySelector('#agentTraceStream [data-event-id]') !== null",
                    timeout=30000,
                )
                assert (
                    page.locator("#agentTraceStream").locator("[data-event-id]").count()
                    >= 1
                )
                exports = page.locator("#agentTaskExports button")
                page.wait_for_function(
                    "document.querySelector('#agentTaskExports button') !== null",
                    timeout=30000,
                )
                assert exports.count() >= 1
                assert page.locator("#agentTaskExports").inner_text().strip()
                page.screenshot(
                    path=str(evidence / "c7-p3-agents-text-loop.png"), full_page=True
                )

                # 刷新后由 localStorage 的 job_id 恢复同一个持久任务与导出目录。
                page.reload(wait_until="domcontentloaded")
                # AC3：显式选择后地址栏已带有效 project_id，刷新应据此预选同一项目。
                page.wait_for_function(
                    "(expected) => document.querySelector('#agentTaskProject')?.value === expected",
                    arg=project.project_id,
                    timeout=15000,
                )
                page.wait_for_function(
                    "document.querySelector('#agentTaskStatus')?.textContent.includes('succeeded')",
                    timeout=30000,
                )
                assert page.locator("#agentTaskRunId").input_value() == job_id
                page.wait_for_function(
                    "document.querySelector('#agentTaskExports button') !== null",
                    timeout=30000,
                )
                assert page.locator("#agentTaskExports button").count() >= 1

                # 项目页从真实项目接口读取六个精确版本，并显示可导出的当前版本入口。
                artifacts_response = context.request.get(
                    base + f"/api/agent/projects/{project.project_id}/artifacts"
                )
                assert artifacts_response.status == 200, artifacts_response.text()
                artifacts = artifacts_response.json()["items"]
                assert len(artifacts) == 6
                assert {item["review_id"] for item in artifacts} == set(
                    review_ids.values()
                )
                assert all(
                    item["run_id"] == run_id and item["version_id"]
                    for item in artifacts
                )

                page.goto(
                    base
                    + f"/static/pages/projects.html?project_id={project.project_id}",
                    wait_until="domcontentloaded",
                )
                page.wait_for_selector(
                    "[data-gw-artifact-version-slot] button[data-version-id]",
                    timeout=30000,
                )
                trigger = page.locator(
                    "[data-gw-artifact-version-slot] button[data-version-id]"
                ).first
                trigger.click()
                popover = page.locator("#gwArtifactVersionPopover")
                assert popover.is_visible()
                assert popover.locator("a[href^='/api/agent/runs/']").count() >= 1
                page.screenshot(
                    path=str(evidence / "c7-p3-project-artifact-versions.png"),
                    full_page=True,
                )

                # 点击 Markdown 导出，并由同一浏览器会话读取精确版本响应内容。
                page.goto(
                    base + "/static/pages/agents.html", wait_until="domcontentloaded"
                )
                page.wait_for_function(
                    "document.querySelector('#agentTaskStatus')?.textContent.includes('succeeded')",
                    timeout=30000,
                )
                page.wait_for_function(
                    "document.querySelector('#agentTaskExports button') !== null",
                    timeout=30000,
                )
                markdown_button = page.get_by_role("button", name="导出 Markdown").first
                markdown_button.click(force=True)
                stage3_artifact = next(
                    item
                    for item in artifacts
                    if item["review_id"] == review_ids["stage3-script"]
                )
                export_result = page.evaluate(
                    """async ({runId, reviewId, versionId}) => {
                        const query = new URLSearchParams({
                            format: 'markdown',
                            review_id: reviewId,
                            artifact_version_id: versionId,
                        });
                        const response = await fetch(`/api/agent/runs/${runId}/export?${query}`);
                        return {status: response.status, body: await response.text()};
                    }""",
                    {
                        "runId": run_id,
                        "reviewId": review_ids["stage3-script"],
                        "versionId": stage3_artifact["version_id"],
                    },
                )
                assert export_result["status"] == 200, export_result
                downloaded = evidence / "c7-p3-stage3-script.md"
                downloaded.write_text(export_result["body"], encoding="utf-8")
                assert "Contract Test Episode" in export_result["body"]

                # 未认证浏览器只能得到 401，不能读取项目文本产物。
                guest = browser.new_context(viewport={"width": 1000, "height": 800})
                try:
                    guest_response = guest.request.get(
                        base + f"/api/agent/projects/{project.project_id}/artifacts"
                    )
                    assert guest_response.status == 401
                finally:
                    guest.close()

                (evidence / "c7-p3-browser-proof.json").write_text(
                    json.dumps(
                        {
                            "project_id": project.project_id,
                            "job_id": job_id,
                            "run_id": run_id,
                            "review_ids": review_ids,
                            "artifact_versions": [
                                item["version_id"] for item in artifacts
                            ],
                            "export_format": "markdown",
                            "export_status": export_result["status"],
                            "guest_artifacts_status": guest_response.status,
                            "screenshots": [
                                "c7-p3-agents-text-loop.png",
                                "c7-p3-project-artifact-versions.png",
                            ],
                            "export_file": downloaded.name,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                assert not page_errors, page_errors
                context.close()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("隔离服务未能停止")
        session.reset_stores()
