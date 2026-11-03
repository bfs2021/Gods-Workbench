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

"""Phase 12 A4 媒体、缩略图与代理 API（真实接入）。

数据来源：``gw.media.repository``（命名空间 ``media_settings`` /
``media_jobs``）+ 允许根目录内的真实素材文件 + ffmpeg/ffprobe/PIL/httpx 真实能力。

安全口径：
- 本机文件读写必须经 ``resolve_within_roots()``；未配置 ``GW_ALLOWED_ROOTS`` → 403；
- 对外只用 ``relative_display()``，绝不回显调用方原始绝对路径；
- 外部依赖（ffmpeg/PIL/httpx）缺失或异常 → 503 失败关闭，绝不伪造；
- 远端抓取做 SSRF 防护（拒绝内网/回环/链路本地），带超时与 16MB 大小上限。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse

from gw.core.auth import require_authenticated, require_edit_access
from gw.core.errors import CleanroomException
from gw.media import proxy as media_proxy
from gw.media import repository as repo

router = APIRouter(tags=["media-b5"])


def _read(authorization: str | None, role: str):
    return require_authenticated(authorization, role)


def _write(authorization: str | None, role: str):
    return require_edit_access(authorization, role)


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _query_dict(request: Request) -> dict[str, Any]:
    return {key: request.query_params.get(key) for key in request.query_params}


# ---------------------------------------------------------------------------
# 代理 / 缩略图设置（JsonState 落盘，写后可读回）
# ---------------------------------------------------------------------------


@router.get("/api/asset-proxy/settings", status_code=status.HTTP_200_OK)
def get_proxy_settings(
    project_id: str | None = None,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """读取代理设置；凭据字段永不回显。"""
    _read(authorization, x_user_role)
    return repo.get_proxy_settings(project_id)


@router.patch("/api/asset-proxy/settings", status_code=status.HTTP_200_OK)
async def patch_proxy_settings(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """更新代理设置；凭据字段一律剥离，不落库不回显。"""
    _write(authorization, x_user_role)
    return repo.patch_proxy_settings(await _json(request))


@router.get("/api/asset-thumbnails/settings", status_code=status.HTTP_200_OK)
def get_thumb_settings(
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """读取缩略图设置；QIN-525 起一并返回预览缓存统计（分类文件数 / 占用）。"""
    _read(authorization, x_user_role)
    settings = dict(repo.get_thumbnail_settings())
    settings["cache"] = repo.cache_statistics()
    return settings


@router.patch("/api/asset-thumbnails/settings", status_code=status.HTTP_200_OK)
async def patch_thumb_settings(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    _write(authorization, x_user_role)
    return repo.patch_thumbnail_settings(
        await _json(request), require_expected_version=True
    )


@router.post("/api/asset-thumbnails/cache/switch", status_code=status.HTTP_200_OK)
async def switch_thumb_cache_settings(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """切换缓存目录，并按 cache_action 迁移 / 删除 / 保留旧缓存。"""
    _write(authorization, x_user_role)
    return repo.switch_thumbnail_cache(await _json(request))


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------


@router.post("/api/asset-thumbnails/generate", status_code=status.HTTP_200_OK)
async def generate_thumbs(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """真实生成缩略图，按设置格式编码并落盘到数据目录。"""
    _write(authorization, x_user_role)
    return repo.generate_thumbnail(await _json(request))


@router.post(
    "/api/asset-thumbnails/generate-background", status_code=status.HTTP_202_ACCEPTED
)
async def generate_thumbs_background(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """后台生成：真实执行后返回稳定 job_id + poll_hint，可被 GET /jobs/{job_id} 查到。

    ``scope="library"``（QIN-519）启动全库预览缓存预热：立即 202，由守护线程真实推进，
    进度经 ``GET /api/asset-thumbnails/jobs/{job_id}`` 回读。
    """
    _write(authorization, x_user_role)
    payload = await _json(request)
    if str((payload or {}).get("scope") or "").strip() == repo.PREVIEW_ALL_SCOPE:
        job_id, poll_hint, total = repo.start_preview_all_job(payload)
        return {
            "task": {
                "id": job_id,
                "job_id": job_id,
                "poll_hint": poll_hint,
                "scope": repo.PREVIEW_ALL_SCOPE,
                "total": total,
            },
            "job_id": job_id,
            "poll_hint": poll_hint,
            "scope": repo.PREVIEW_ALL_SCOPE,
            "total": total,
            "data_status": "ok",
            "data_gaps": [],
        }
    job_id, poll_hint = repo.run_background_thumbnail(payload)
    return {
        "task": {"id": job_id, "job_id": job_id, "poll_hint": poll_hint},
        "job_id": job_id,
        "poll_hint": poll_hint,
        "data_status": "ok",
        "data_gaps": [],
    }


@router.get("/api/asset-thumbnails/jobs/{job_id}", status_code=status.HTTP_200_OK)
def get_thumb_job(
    job_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """读取后台任务状态；不存在 404。"""
    _read(authorization, x_user_role)
    return repo.get_job(job_id)


@router.post(
    "/api/asset-thumbnails/jobs/{job_id}/control", status_code=status.HTTP_200_OK
)
async def control_thumb_job(
    request: Request,
    job_id: str,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """暂停 / 继续 / 取消全库预览预热任务（QIN-525）。

    只投递信号并返回任务快照；取消后飞行中的项跑完，``status`` 由预热线程在
    下一个安全点写为 ``cancelled``，调用方按既有轮询读取终态。
    """
    _write(authorization, x_user_role)
    return repo.control_preview_all_job(job_id, await _json(request))


@router.post("/api/asset-thumbnails/delete", status_code=status.HTTP_200_OK)
async def delete_thumbs(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """删除真实生成的缩略图缓存；删除后不再返回。"""
    _write(authorization, x_user_role)
    return repo.delete_outputs("thumbnails", await _json(request))


@router.post(
    "/api/asset-thumbnails/generate-storyboards", status_code=status.HTTP_202_ACCEPTED
)
async def generate_storyboards_background(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """后台预生成视频九宫格与片段缩略图：返回稳定 job_id + poll_hint。"""
    _write(authorization, x_user_role)
    job_id, poll_hint = repo.run_background_storyboards(await _json(request))
    return {
        "task": {"id": job_id, "job_id": job_id, "poll_hint": poll_hint},
        "job_id": job_id,
        "poll_hint": poll_hint,
        "data_status": "ok",
        "data_gaps": [],
    }


@router.post("/api/asset-thumbnails/delete-storyboards", status_code=status.HTTP_200_OK)
async def delete_storyboards(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """删除真实生成的分镜缓存；删除后不再返回。"""
    _write(authorization, x_user_role)
    return repo.delete_outputs("storyboards", await _json(request))


# ---------------------------------------------------------------------------
# 媒体读取
# ---------------------------------------------------------------------------


@router.get("/api/media-preview", status_code=status.HTTP_200_OK)
def media_preview(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """对允许根目录内的真实文件返回真实缩略字节；越界 403，依赖缺失 503。"""
    _read(authorization, x_user_role)
    payload = _query_dict(request)
    if str(payload.get("metadata") or "").lower() in {"1", "true", "yes"}:
        return media_proxy.descriptor(payload)
    result = repo.preview_bytes(payload)
    return Response(content=result["bytes"], media_type=result["media_type"])


# ---------------------------------------------------------------------------
# C11-T4 轻量代理：结构化状态与受控派生内容
# ---------------------------------------------------------------------------


@router.get("/api/media-proxy", status_code=status.HTTP_200_OK)
def media_proxy_descriptor(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """返回真实代理的 ready 状态，或不伪造媒体的规范降级状态。"""
    _read(authorization, x_user_role)
    return media_proxy.descriptor(_query_dict(request))


@router.post("/api/media-proxy", status_code=status.HTTP_200_OK)
async def media_proxy_descriptor_post(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """POST 形态供检索结果直接提交结构化代理请求。"""
    _read(authorization, x_user_role)
    return media_proxy.descriptor(await _json(request))


@router.get("/api/media-proxy/content", status_code=status.HTTP_200_OK)
def media_proxy_content(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """读取代理派生文件；源离线或依赖缺失时返回带动作的 503 错误包。"""
    _read(authorization, x_user_role)
    try:
        path, metadata = media_proxy.artifact_path(_query_dict(request))
        content = path.read_bytes()
    except CleanroomException as exc:
        return JSONResponse(
            status_code=exc.status_code, content=media_proxy.error_payload(exc)
        )
    except OSError:
        error = CleanroomException(503, "MEDIA_PROCESS_FAILED", "代理产物暂时不可读")
        return JSONResponse(status_code=503, content=media_proxy.error_payload(error))
    return Response(
        content=content,
        media_type=metadata.get("media_type") or repo._mime(path),
        headers={"X-Proxy-Artifact": path.name},
    )


@router.get("/api/media-transcode", status_code=status.HTTP_200_OK)
def media_transcode(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """真实 ffmpeg 转码并可下载输出；ffmpeg 缺失或失败 503。"""
    _read(authorization, x_user_role)
    result = repo.transcode(_query_dict(request))
    path = (repo.storage.data_root() / result["output"]).resolve()
    if not path.is_file():
        raise CleanroomException(404, "FILE_NOT_FOUND", "转码输出不存在")
    return Response(
        content=path.read_bytes(),
        media_type="video/mp4",
        headers={"X-Transcoded-Bytes": str(result["bytes"])},
    )


@router.get("/api/audio-waveform-data", status_code=status.HTTP_200_OK)
def waveform(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """真实 RMS 包络（ffmpeg 解码）；无音频流 400，ffmpeg 缺失 503。"""
    _read(authorization, x_user_role)
    return repo.waveform(_query_dict(request))


@router.get("/api/download-output", status_code=status.HTTP_200_OK)
def download_output(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """只允许下载数据目录内的输出文件；越界 403，不存在 404。"""
    _read(authorization, x_user_role)
    result = repo.download_path(_query_dict(request))
    # 响应读取前再次校验，避免把此前准入结果当作永久授权。
    path = repo.storage.resolve_output_path(result["relative_path"])
    from urllib.parse import quote

    safe = (
        "".join(ch for ch in result["filename"] if ch not in '\\/:*?"<>|\r\n').strip()
        or path.name
    )
    ascii_name = safe.encode("ascii", "ignore").decode("ascii").strip() or "output"
    return Response(
        content=path.read_bytes(),
        media_type=repo._mime(path),
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"
        },
    )


# ---------------------------------------------------------------------------
# 远端图片
# ---------------------------------------------------------------------------


@router.post("/api/online-image", status_code=status.HTTP_200_OK)
async def online_image(
    request: Request,
    authorization: str | None = Header(None),
    x_user_role: str = Header("editor", alias="X-User-Role"),
):
    """真实抓取远端图片；SSRF 受限地址 403，网络失败 503。"""
    _write(authorization, x_user_role)
    return repo.fetch_remote_image(await _json(request))
