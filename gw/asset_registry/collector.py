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

"""网络素材收集：对远程地址和来源执行失败关闭校验。"""

from ipaddress import ip_address
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urljoin, urlsplit

from gw.asset_registry import repository
from gw.core import storage

_store_lock = threading.RLock()

#: AC1：不支持来源（blob:、超限内联数据、HLS/DASH 清单、DRM 标记）的统一错误码。
UNSUPPORTED_SOURCE_CODE = "COLLECT_UNSUPPORTED_SOURCE"
#: 内联 data: 载荷上限；超过即按不支持来源拒绝，引导用户改用上传。
MAX_INLINE_DATA_BYTES = 8 * 1024 * 1024
#: 流媒体清单后缀：清单本身不是可收集的素材文件。
STREAM_MANIFEST_SUFFIXES = (".m3u8", ".mpd")
#: 已知 DRM 保护标记，命中即拒绝，避免下载到加密分片。
DRM_SOURCE_MARKERS = ("drm", "widevine", "playready", "fairplay")


class CollectError(ValueError):
    """收集失败及可供界面展示的稳定错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def find_ytdlp() -> str:
    """查找用户配置的可执行文件，不安装或导入第三方包。"""
    configured = os.environ.get("GW_YTDLP_PATH", "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute() or not path.is_file():
            raise CollectError("ytdlp_unavailable", "未检测到 yt-dlp，请安装后重试")
        return str(path)
    executable = shutil.which("yt-dlp")
    if not executable:
        raise CollectError("ytdlp_unavailable", "未检测到 yt-dlp，请安装后重试")
    return executable


class _PublicProxy(socketserver.ThreadingTCPServer):
    """临时回环代理：所有目标重新解析，拒绝非公网地址。"""
    allow_reuse_address = True
    daemon_threads = True


class _PublicProxyHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        try:
            line = self.rfile.readline(8193)
            method, target, version = line.decode("ascii").strip().split(" ", 2)
            headers = []
            while True:
                header = self.rfile.readline(8193)
                if not header or len(header) > 8192:
                    return
                if header in {b"\r\n", b"\n"}:
                    break
                if not header.lower().startswith((b"proxy-connection:", b"proxy-authorization:")):
                    headers.append(header)
                if sum(map(len, headers)) > 65536:
                    return
            if method == "CONNECT":
                parsed = urlsplit("https://" + target)
                port = parsed.port or 443
            else:
                if method not in {"GET", "HEAD"}:
                    return
                parsed = urlsplit(validate_collect_url(target))
                if parsed.scheme != "http":
                    return
                port = parsed.port or 80
            if not parsed.hostname:
                return
            records = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(record[4][0] for record in records))
            validate_public_addresses(addresses)
            with socket.create_connection((addresses[0], port), timeout=15) as upstream:
                if method == "CONNECT":
                    self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self.wfile.flush()
                else:
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    upstream.sendall(f"{method} {path} {version}\r\n".encode("ascii") + b"".join(headers) + b"\r\n")
                sockets = [self.connection, upstream]
                while True:
                    readable, _, _ = select.select(sockets, [], [], 15)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, UnicodeError, CollectError):
            return


def download_with_ytdlp(url: str, *, timeout: int = 120, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, str, str]:
    """使用已安装的外部工具下载单项；超时及失败均返回稳定错误码。"""
    url = validate_collect_url(url)
    executable = find_ytdlp()
    with tempfile.TemporaryDirectory(prefix="gw-collect-") as tmp_dir, _PublicProxy(("127.0.0.1", 0), _PublicProxyHandler) as proxy:
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        output = str(Path(tmp_dir) / "media.%(ext)s")
        args = [executable, "--ignore-config", "--no-playlist", "--no-progress", "--no-write-info-json",
                "--proxy", f"http://127.0.0.1:{proxy.server_address[1]}", "--downloader", "native",
                "--max-filesize", str(max_bytes), "--print", "after_move:%(title)j", "--output", output, "--", url]
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CollectError("ytdlp_timeout", "素材下载超时") from exc
        except OSError as exc:
            raise CollectError("ytdlp_unavailable", "未检测到 yt-dlp，请安装后重试") from exc
        finally:
            proxy.shutdown()
            proxy_thread.join()
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace").lower()
            if "drm" in error:
                raise CollectError("drm_protected", "该内容受 DRM 版权保护，无法收集")
            raise CollectError("ytdlp_failed", "素材下载失败")
        candidates = [path for path in Path(tmp_dir).iterdir() if path.is_file() and not path.is_symlink()]
        if len(candidates) != 1:
            raise CollectError("ytdlp_failed", "素材下载结果不可用")
        path = candidates[0]
        if path.stat().st_size > max_bytes:
            raise CollectError("ytdlp_failed", "素材超过大小限制")
        data = path.read_bytes()
        if not data:
            raise CollectError("ytdlp_failed", "素材为空")
        try:
            title = json.loads(result.stdout.decode("utf-8", errors="replace").strip())
        except (json.JSONDecodeError, AttributeError):
            title = ""
        if not isinstance(title, str) or not title.strip() or title.strip().casefold() == "na":
            title = path.stem
        return data, path.name, title.strip()[:300]


def validate_collect_url(value: str) -> str:
    """仅接受不含凭据的 HTTP(S) URL，拒绝浏览器内部地址。"""
    url = str(value or "").strip()
    if url.lower().startswith("blob:"):
        raise CollectError("blob_url", "浏览器内部地址无法收集，请改用原始链接")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CollectError("unsupported_url", "链接格式不支持") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in url)
    ):
        raise CollectError("unsupported_url", "链接格式不支持")
    return url


def _inline_data_bytes(value: str) -> int:
    """按 data: URI 头部声明估算载荷字节数，不真正解码超大载荷。"""
    header, _, payload = value.partition(",")
    if ";base64" in header.lower():
        return len(payload) * 3 // 4
    return len(payload)


def unsupported_source_reason(value: str) -> str | None:
    """返回不支持来源的中文原因；支持的来源返回 None，全程不发起网络请求。"""
    url = str(value or "").strip()
    lowered = url.lower()
    if lowered.startswith("blob:"):
        return "浏览器内部地址无法收集，请改用原始链接"
    if lowered.startswith("data:"):
        if _inline_data_bytes(url) > MAX_INLINE_DATA_BYTES:
            return f"内联数据超过 {MAX_INLINE_DATA_BYTES // (1024 * 1024)} MiB 上限，请改为上传文件"
        return "内联数据地址无法收集，请改为上传文件"
    try:
        path = urlsplit(lowered).path
    except ValueError:
        # 结构异常的地址交由 validate_collect_url 统一按链接格式不支持处理。
        return None
    if path.endswith(STREAM_MANIFEST_SUFFIXES):
        return "不支持 HLS/DASH 流媒体清单地址，请改用可直接下载的文件链接"
    for marker in DRM_SOURCE_MARKERS:
        if re.search(rf"(?:^|[^a-z0-9]){marker}(?:[^a-z0-9]|$)", lowered):
            return "该来源带有 DRM 保护标记，无法收集"
    return None


def reject_unsupported_source(value: str) -> str:
    """收集入口的 fail-closed 前哨：命中不支持来源即抛统一错误码。"""
    reason = unsupported_source_reason(value)
    if reason:
        raise CollectError(UNSUPPORTED_SOURCE_CODE, reason)
    return str(value or "").strip()


def validate_public_addresses(addresses: list[str]) -> None:
    """解析结果必须全部为公网地址，避免借 DNS 访问本机或内网。"""
    if not addresses:
        raise CollectError("unsupported_url", "无法解析素材来源")
    for value in addresses:
        try:
            address = ip_address(value)
        except ValueError as exc:
            raise CollectError("unsupported_url", "无法解析素材来源") from exc
        if not address.is_global or getattr(address, "ipv4_mapped", None) is not None:
            raise CollectError("unsupported_url", "不允许访问本机或内网地址")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接经过校验的地址，TLS 主机名仍使用原始域名。"""

    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=15, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=15)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def download_direct(url: str, *, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, str]:
    """有界下载直链；每次重定向均重新校验并固定 DNS 解析结果。"""
    current = validate_collect_url(reject_unsupported_source(url))
    for _ in range(6):
        parsed = urlsplit(current)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        try:
            records = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(record[4][0] for record in records))
            validate_public_addresses(addresses)
            connection_type = _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection
            connection = connection_type(parsed.hostname, port, addresses[0])
            try:
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                connection.request("GET", target, headers={"Accept": "*/*", "User-Agent": "GodsWorkbench-Collector/1"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise CollectError("unsupported_url", "重定向缺少目标")
                    current = validate_collect_url(
                        reject_unsupported_source(urljoin(current, location))
                    )
                    continue
                if response.status != 200:
                    raise CollectError("unsupported_url", "素材来源不可下载")
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                if content_type in {"text/html", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}:
                    raise CollectError("ytdlp_required", "页面或流媒体需要 yt-dlp")
                data = response.read(max_bytes + 1)
                if not data or len(data) > max_bytes:
                    raise CollectError("unsupported_url", "素材为空或超过大小限制")
                return data, current
            finally:
                connection.close()
        except CollectError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise CollectError("unsupported_url", "素材来源连接失败") from exc
    raise CollectError("unsupported_url", "重定向次数过多")


def store_content(data: bytes, name: str, source_url: str = "", title: str = "", *, owner: str) -> dict:
    """按内容哈希登记到独立收集目录，重复内容复用资产并追加来源。"""
    if not data:
        raise CollectError("unsupported_url", "素材为空")
    digest = hashlib.sha256(data).hexdigest()
    owner_directory = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    directory = storage.data_root() / "registry_uploads" / "collect_inbox" / owner_directory
    source = {"url": source_url, "title": title or name, "captured_at": storage.now_iso()}
    with _store_lock:
        raw = repository.state().read()
        for item in raw["assets"].values():
            if item.get("metadata", {}).get("collect_sha256") == digest and item.get("workflow_owner") == owner:
                asset_id = item["asset_id"]

                def add_source(state):
                    asset = state["assets"][asset_id]
                    asset["metadata"].setdefault("collect_sources", []).append(source)
                    asset["version"] += 1
                    asset["updated_at"] = storage.now_iso()
                    repository._bump(state)
                    return dict(asset)

                return {"asset": repository.state().mutate(add_source), "duplicate": True}
        directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        existing = sorted(directory.glob(digest + ".*"))
        path = existing[0] if existing else directory / (digest + suffix)
        if path.exists():
            if path.read_bytes() != data:
                raise CollectError("unsupported_url", "收集文件校验失败")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".collect-", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        relative = f"registry_uploads/collect_inbox/{owner_directory}/{path.name}"
        kind = ("image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".jxl"}
                else "video" if suffix in {".mp4", ".mov", ".webm", ".mkv"}
                else "audio" if suffix in {".mp3", ".wav", ".flac", ".m4a"}
                else "unknown")
        metadata = {"collect_sha256": digest, "collect_sources": [source]}
        duration = None
        if kind == "video":
            from gw.asset_registry.index_jobs import _video_index_data
            video_metadata, duration = _video_index_data(path)
            metadata.update(video_metadata)
        elif kind == "image" and suffix == ".jxl":
            from gw.asset_registry.media_probe import probe_jxl_metadata

            jxl_metadata = probe_jxl_metadata(path)
            if jxl_metadata is not None:
                metadata["jxl_metadata"] = jxl_metadata

        def register(state):
            asset_id = repository._seq(state, "ast_", state["assets"].keys())
            item = repository._plain_asset(
                asset_id, name or path.name, kind, size_bytes=len(data),
                display_path=relative, workflow_owner=owner,
                metadata=metadata, duration=duration,
            )
            state["assets"][asset_id] = item
            state["sequences"][asset_id] = True
            repository._upsert_asset_search_documents([item])
            rows, _ = repository._asset_shot_rows(item)
            if rows:
                from gw.core.sqlite_vec_engine import SQLiteVecEngine
                with SQLiteVecEngine(storage.data_root() / repository.ASSET_SEARCH_INDEX_FILENAME) as engine:
                    engine.batch_upsert_shots(rows)
            repository._bump(state)
            return dict(item)

        return {"asset": repository.state().mutate(register), "duplicate": False}
