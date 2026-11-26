"""只读英雄联盟本地 Live Client Data API。"""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl
from typing import Any
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://127.0.0.1:2999/liveclientdata"
HTTP_BASE_URL = "http://127.0.0.1:2999/liveclientdata"
_MAX_RESPONSE_BYTES = 1024 * 1024
_LOCAL_AUTHORITIES = {
    "127.0.0.1:2999": "127.0.0.1:2999",
    "localhost:2999": "127.0.0.1:2999",
    "[::1]:2999": "[::1]:2999",
}


class NotInGame(Exception):
    """当前无法读取有效的对局数据。"""


class LiveClient:
    """只向本机的游戏客户端发送 GET 请求。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 3.0,
        allow_http_fallback: bool = True,
    ) -> None:
        url = urlsplit((base_url or DEFAULT_BASE_URL).rstrip("/"))
        authority = _LOCAL_AUTHORITIES.get(url.netloc.lower())
        if (
            url.scheme not in {"http", "https"}
            or authority is None
            or url.path != "/liveclientdata"
            or url.query
            or url.fragment
        ):
            raise ValueError("base_url 必须指向本机 2999 端口的 /liveclientdata")
        self.base_url = f"{url.scheme}://{authority}{url.path}"
        self.timeout = max(0.5, float(timeout))
        self.allow_http_fallback = bool(allow_http_fallback)
        # 仅向固定的回环地址发请求，兼容游戏客户端的自签证书。
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        self._prefer_http = url.scheme == "http"

    @property
    def scheme(self) -> str:
        """最近成功使用的协议，连接自检会显示它。"""
        return "http" if self._prefer_http else "https"

    def _get_once(self, base: str, path: str) -> Any:
        url = urlsplit(base)
        if url.scheme == "https":
            conn = http.client.HTTPSConnection(url.netloc, timeout=self.timeout, context=self._ssl)
        else:
            conn = http.client.HTTPConnection(url.netloc, timeout=self.timeout)
        try:
            conn.request("GET", f"{url.path}{path}")
            with conn.getresponse() as response:
                # 直接处理状态码，不跟随重定向，也不因 HTTP 状态码切换协议。
                if response.status != 200:
                    raise NotInGame(f"HTTP {response.status}")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise NotInGame("对局数据响应过大")
            try:
                return json.loads(body)
            except (UnicodeError, ValueError) as exc:
                raise NotInGame("对局数据响应不是有效的 JSON") from exc
        finally:
            conn.close()

    def _get(self, path: str) -> Any:
        primary = self.base_url
        urls = [primary]
        if self.allow_http_fallback and primary.startswith("https://"):
            urls.append(primary.replace("https://", "http://", 1))
            if self._prefer_http:
                urls.reverse()

        last_error: Exception | None = None
        for base in urls:
            try:
                data = self._get_once(base, path)
            except (http.client.HTTPException, OSError) as exc:
                last_error = exc
                continue
            self._prefer_http = base.startswith("http://")
            return data
        raise NotInGame(str(last_error) if last_error else "unreachable") from last_error

    async def snapshot(self) -> tuple[list[dict], dict]:
        """在一次轮询内顺序读取事件和战况。"""
        return await asyncio.to_thread(self._snapshot)

    def _snapshot(self) -> tuple[list[dict], dict]:
        events = self._read_events()
        data = self._get("/allgamedata")
        return events, data if isinstance(data, dict) else {}

    def _read_events(self) -> list[dict]:
        data = self._get("/eventdata")
        events = data.get("Events") if isinstance(data, dict) else None
        return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []

    async def event_data(self) -> list[dict]:
        """供诊断脚本单独读取事件流。"""
        return await asyncio.to_thread(self._read_events)

    async def all_game_data(self) -> dict:
        """供连接自检单独读取战况。"""
        data = await asyncio.to_thread(self._get, "/allgamedata")
        return data if isinstance(data, dict) else {}
