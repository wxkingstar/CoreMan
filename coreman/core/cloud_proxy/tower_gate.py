# ruff: noqa
# mypy: ignore-errors
# 原样复制自 Tower 仓库 sdk/python/tower_gate.py（Tower commit 1126d9f），只加了这几行文件头。
# 哈希规则必须与 Tower 字节级一致，改动请回 Tower 仓库改完再整份重新复制；
# tests/unit/cloud_proxy/tower_vectors.json 与该 commit 的 pkg/towergate/testdata/vectors.json 相同，一致性由 test_tower_vectors.py 守着。
"""业务系统侧的 Tower 审批令牌校验（Python 版）。

哈希规则与 Go 版 pkg/towergate 字节级一致，以 pkg/towergate/testdata/vectors.json 为准。
只用标准库：要接入的系统五花八门，不该因为一个校验中间件引入依赖。

哈希绑定的范围（接入方必读，绑不住的东西令牌就管不住）：
- 只绑定 method、path、query、body、caller；host 和所有请求头都不参与。
- 环境 / 租户 / 动作必须体现在 path、query 或 body 里；做不到就每个环境登记成独立目标系统、
  各用各的 API Key（如 nacos-test 与 nacos-prod 是两个目标）。
- 受保护接口不得认 method 覆盖头（X-HTTP-Method-Override、_method），也不得让未参与哈希的头
  （Content-Type、namespace 之类）改变请求语义。
"""

import asyncio
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

HEADER = "x-tower-approval"
DEFAULT_MAX_BODY = 1 << 20


class TowerGateError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code = code
        self.status = status


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_HEX = b"0123456789ABCDEFabcdef"
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")
# 与 Go net/url 的 defaultMaxParams 一致：超过它 Go 侧直接报错，两边都必须拒绝。
_MAX_QUERY_PARAMS = 10000


def _query_unescape(seg: bytes) -> bytes:
    # 对齐 Go url.QueryUnescape：'+' 变空格，'%' 后必须紧跟两位十六进制，否则报错。
    # 必须按字节做：urllib 的 unquote 会把非法 UTF-8 替换成 U+FFFD，
    # 让 %FF 与 %EF%BF%BD 算出同一个哈希，而 Go 把它们当成两个不同的值。
    out = bytearray()
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == 0x25:  # '%'
            if i + 2 >= len(seg) or seg[i + 1] not in _HEX or seg[i + 2] not in _HEX:
                raise ValueError("query 里有不合法的 % 转义")
            out.append(int(seg[i + 1:i + 3], 16))
            i += 3
            continue
        out.append(0x20 if c == 0x2B else c)
        i += 1
    return bytes(out)


def _query_escape(b: bytes) -> str:
    # 对齐 Go url.QueryEscape：只有 A-Za-z0-9-_.~ 原样保留，空格写成 '+'，其余一律 %XX（大写）。
    parts = []
    for c in b:
        if c in _UNRESERVED:
            parts.append(chr(c))
        elif c == 0x20:
            parts.append("+")
        else:
            parts.append("%%%02X" % c)
    return "".join(parts)


def _canon_query(raw_query) -> str:
    """对齐 Go url.ParseQuery + Values.Encode：按键的原始字节排序，同键保持原顺序。

    query 无法解析（含 ';'、坏的 % 转义、参数过多）时抛 ValueError——Go 侧同样报错，
    调用方必须据此拒绝请求，而不是拿一个 Go 永远算不出来的哈希去核销。
    """
    q = raw_query.encode("utf-8") if isinstance(raw_query, str) else bytes(raw_query)
    if q.count(b"&") + 1 > _MAX_QUERY_PARAMS:
        raise ValueError("query 参数过多")
    grouped = {}
    err = None
    for seg in q.split(b"&"):
        if b";" in seg:
            err = err or ValueError("query 里不允许用 ';' 分隔")
            continue
        if not seg:
            continue
        k, _, v = seg.partition(b"=")
        try:
            grouped.setdefault(_query_unescape(k), []).append(_query_unescape(v))
        except ValueError as e:
            err = err or e
    if err:
        raise err
    parts = []
    for k in sorted(grouped):
        for v in grouped[k]:
            parts.append(_query_escape(k) + "=" + _query_escape(v))
    return "&".join(parts)


def request_hash_parts(method, target, escaped_path, raw_query, body: bytes, caller) -> str:
    """raw_query 可以是 str（按 UTF-8 取字节）或 bytes（ASGI 的 query_string 原样传入）。
    query 无法解析时抛 ValueError。"""
    path = escaped_path or "/"
    q = _canon_query(raw_query or "")
    if q:
        path += "?" + q
    canonical = "\n".join([method.upper(), target, path, _sha(body or b""), caller])
    return _sha(canonical.encode("utf-8"))


def request_hash(method, target, url, body: bytes, caller) -> str:
    parts = urllib.parse.urlsplit(url)
    return request_hash_parts(method, target, parts.path, parts.query, body, caller)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def redeem(tower_url, api_key, token, req_hash, caller, timeout=10.0) -> str:
    payload = json.dumps({"token": token, "request_hash": req_hash, "caller": caller}).encode()
    req = urllib.request.Request(tower_url.rstrip("/") + "/v1/tokens/redeem", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": api_key})
    # 不吃环境里的 HTTP(S)_PROXY：请求里带着目标系统的 API Key 和令牌，不能让环境变量把它改道出去。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    rejected = None
    try:
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read(64 << 10) or b"{}")
                if resp.status == 200 and isinstance(data, dict) and isinstance(data.get("approval_id"), str) \
                        and data["approval_id"]:
                    return data["approval_id"]
        except urllib.error.HTTPError as e:
            with e:  # 读完就关，别把连接留给 GC
                raw = e.read(64 << 10) if e.code == 403 else b""
            if e.code == 403:
                data = json.loads(raw)
                if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"]:
                    rejected = data["error"]
    except Exception:  # noqa: BLE001
        # 这里故意兜住一切：非 dict 的 JSON、http.client.HTTPException、解码错误……
        # 任何意料之外的异常都必须落到下面的 503，而不是冒泡成 500 或别的状态让调用方误判。
        pass
    if rejected:
        raise TowerGateError(rejected, 403)
    # 任何拿不准的结果都按不可用处理：绝不因为审批服务出问题就放行高危操作。
    raise TowerGateError("tower_unavailable", 503)


async def _respond(send, status, body):
    raw = json.dumps(body, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")]})
    await send({"type": "http.response.body", "body": raw})


class TowerGate:
    """ASGI 中间件。protected(scope) 决定哪些请求要审批——由业务系统决定，不由 Agent 判断。"""

    def __init__(self, app, *, tower_url, api_key, target, caller_of, protected=lambda scope: True,
                 max_body=DEFAULT_MAX_BODY):
        self.app = app
        self.tower_url = tower_url
        self.api_key = api_key
        self.target = target
        self.caller_of = caller_of
        self.protected = protected
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.protected(scope):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        token = headers.get(HEADER.encode(), b"").decode().strip()
        if not token:
            await _respond(send, 428, {"error": "approval_required", "target": self.target,
                                       "message": "该操作需要经 Tower 审批，请提交审批并带上令牌重试"})
            return
        caller = self.caller_of(scope)
        if not caller:
            await _respond(send, 401, {"error": "unknown_caller"})
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if len(body) > self.max_body:
                await _respond(send, 413, {"error": "body_too_large"})
                return
            if not msg.get("more_body"):
                break
        raw_path = scope.get("raw_path")
        path = raw_path.decode("latin-1") if raw_path else urllib.parse.quote(scope.get("path", "/"))
        # query 按原始字节交给规范化，不先解码成 str，免得非 ASCII 字节被改写。
        query = scope.get("query_string") or b""
        try:
            req_hash = request_hash_parts(scope["method"], self.target, path, query, body, caller)
        except ValueError:
            # 与 Go 版一致：query 规范化不了是请求本身的问题，回 400，也不去 Tower 核销。
            await _respond(send, 400, {"error": "bad_query"})
            return
        try:
            approval_id = await asyncio.to_thread(redeem, self.tower_url, self.api_key, token, req_hash, caller)
        except TowerGateError as e:
            await _respond(send, e.status, {"error": e.code})
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(dict(scope, tower_approval_id=approval_id), replay, send)
