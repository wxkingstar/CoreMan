"""假的企微服务端 API（gettoken + openuserid_to_userid），httpx.MockTransport。"""

from __future__ import annotations

import json

import httpx

from coreman.core.platforms.wecom import BASE_URL


class FakeWeComApi:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = dict(mapping)
        self.token_calls = 0
        self.convert_calls = 0
        self.fail_token = False
        self.transport = httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=BASE_URL, transport=self.transport)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi-bin/gettoken":
            self.token_calls += 1
            if self.fail_token:
                return httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})
            return httpx.Response(
                200,
                json={"errcode": 0, "errmsg": "ok", "access_token": "tok", "expires_in": 7200},
            )
        if path == "/cgi-bin/batch/openuserid_to_userid":
            self.convert_calls += 1
            asked = json.loads(request.content or b"{}").get("open_userid_list") or []
            found = [
                {"open_userid": o, "userid": self.mapping[o]} for o in asked if o in self.mapping
            ]
            invalid = [o for o in asked if o not in self.mapping]
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "errmsg": "ok",
                    "userid_list": found,
                    "invalid_open_userid_list": invalid,
                },
            )
        return httpx.Response(404)
