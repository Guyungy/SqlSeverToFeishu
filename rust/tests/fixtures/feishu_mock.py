#!/usr/bin/env python3
"""飞书开放平台的本地桩服务，供 Rust 集成测试使用。

只实现同步流程真正会用到的那几个接口：
  POST /open-apis/auth/v3/tenant_access_token/internal
  GET  /open-apis/bitable/v1/apps/{app_token}/tables
  GET  /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields

行为由 app_token 的前缀决定，这样同一个桩能覆盖多种分支而不用重启：

  appPaging...     /tables 分两页返回（验证分页拼接）
  appRetry429...   /tables 首次返回 HTTP 429（验证退避重试）
  appGateway5xx... /tables 首次返回 HTTP 502 + HTML 错误页（验证非 JSON 的
                   5xx 也要走重试，而不是当成硬失败）
  appStaleTkn...   /tables 首次返回 code 99991663（验证令牌失效后重新取号）
  appBizErr...     /tables 返回业务错误码（验证错误直接抛出而不是静默重试）
  appStuckCursor.. /tables 永远 has_more 且把游标原样退回（验证游标未推进
                   时中止，避免翻到上限的死循环）
  appNoToken...    /tables 宣告 has_more 却不给 page_token（验证缺游标时报错，
                   而不是当作读完了）
  其它             /tables 单页返回

用法: feishu_mock.py <日志文件路径>
启动后在标准输出打印一行 `PORT <n>`，测试据此拿到监听端口。
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOG_PATH = sys.argv[1] if len(sys.argv) > 1 else "/dev/null"
LOG_LOCK = threading.Lock()
STATE = {"auth": 0, "tables_hit": {}, "fields_hit": {}}


def log(line):
    with LOG_LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def app_token_of(path):
    parts = [p for p in path.split("/") if p]
    if "apps" in parts:
        return parts[parts.index("apps") + 1]
    return ""


def kind_of(path):
    if path.endswith("/internal"):
        return "auth"
    if path.endswith("/tables"):
        return "tables"
    if path.endswith("/fields"):
        return "fields"
    return "other"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音默认 stderr 访问日志
        pass

    def _send(self, status, payload, code=None):
        """先记日志、再回响应。

        顺序很关键：反过来写（先回响应后记日志）会引入竞态 —— 客户端拿到
        响应就返回，测试端读日志时那一行**可能还没落盘**。这在 macOS 上
        大概率侥幸通过，在 Windows 上必然翻车（实测就是这么暴露出来的）。
        先记日志后回响应，则「客户端收到了响应」就蕴含「日志已写好」。
        """
        if code is None:
            code = payload.get("code", 0) if isinstance(payload, dict) else 0
        self._record(status, code)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status, raw_body, code=None):
        """回一段非 JSON 的原始响应体（模拟网关 / 反向代理的错误页）。

        与 `_send` 一样先记日志再回响应，理由见那边的注释。
        """
        self._record(status, code if code is not None else 0)

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw_body)))
        self.end_headers()
        self.wfile.write(raw_body)

    def _drain_body(self):
        """读掉并丢弃请求体，保持长连接的下一个请求对齐。

        桩只关心请求行与头，不解析 body，但**必须**按 Content-Length 读走
        字节：留着不读，它们就会污染同一条连接上的下一个请求。
        """
        length = self.headers.get("Content-Length")
        if not length:
            return
        try:
            remaining = int(length)
        except ValueError:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _record(self, status, code):
        auth = "yes" if self.headers.get("Authorization", "").startswith("Bearer ") else "no"
        log(f"{self.command} {self.path} -> {status} code={code} auth={auth}")

    # 令牌接口不校验 Authorization。
    def do_POST(self):
        # HTTP/1.1 默认长连接：必须把请求体读干净，否则残留的字节会被
        # 下一个 handle_one_request 当成请求行（实测会解析出
        # `{"app_id":...}GET` 这种 command），BaseHTTPRequestHandler
        # 便回 501 Unsupported method，测试随即失败。
        self._drain_body()

        path = urlparse(self.path).path
        if kind_of(path) != "auth":
            self._send(404, {"code": 404})
            return
        STATE["auth"] += 1
        self._send(
            200,
            {
                "code": 0,
                "msg": "ok",
                "tenant_access_token": f"tkn-{STATE['auth']}",
                "expire": 7200,
            },
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        app_token = app_token_of(path)
        kind = kind_of(path)

        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(401, {"code": 99991661, "msg": "missing authorization"})
            return

        if kind == "tables":
            hit = STATE["tables_hit"].get(app_token, 0) + 1
            STATE["tables_hit"][app_token] = hit

            if app_token.startswith("appRetry429") and hit == 1:
                self._send(429, {"code": 1254290, "msg": "too many requests"})
                return

            # 网关 / 反向代理在 5xx 时回的是 HTML 错误页，而不是飞书的 JSON
            # 信封。客户端必须在解析响应体**之前**就把它当成可重试的服务端
            # 状态，否则这个本可自愈的瞬时故障会变成一次硬失败。
            if app_token.startswith("appGateway5xx") and hit == 1:
                self._send_raw(
                    502,
                    b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>",
                    code=502,
                )
                return

            if app_token.startswith("appStaleTkn") and hit == 1:
                self._send(200, {"code": 99991663, "msg": "token expired"})
                return

            if app_token.startswith("appBizErr"):
                self._send(200, {"code": 1254005, "msg": "app not found"})
                return

            if app_token.startswith("appPaging"):
                page_token = (query.get("page_token") or [""])[0]
                if page_token == "page-2":
                    payload = {
                        "has_more": False,
                        "page_token": "",
                        "items": [{"table_id": "tbl3", "name": "第三张表"}],
                    }
                else:
                    payload = {
                        "has_more": True,
                        "page_token": "page-2",
                        "items": [
                            {"table_id": "tbl1", "name": "第一张表"},
                            {"table_id": "tbl2", "name": "第二张表"},
                        ],
                    }
                self._send(200, {"code": 0, "msg": "ok", "data": payload})
                return

            # 服务端不认游标、却一直说「还有下页」并把游标原样退回：
            # 客户端若不加判断就会一路翻到 MAX_PAGES 才停，而且每页都拿到
            # 同样的数据。必须在游标没前进的那一刻就中止。
            if app_token.startswith("appStuckCursor"):
                returned = (query.get("page_token") or ["page-1"])[0]
                payload = {
                    "has_more": True,
                    "page_token": returned,
                    "items": [{"table_id": "tblLoop", "name": "重复页"}],
                }
                self._send(200, {"code": 0, "msg": "ok", "data": payload})
                return

            # 宣告 has_more 却没有给出 page_token：无法继续翻页，
            # 必须报错而不是当作「读完了」静默少读数据。
            if app_token.startswith("appNoToken"):
                payload = {
                    "has_more": True,
                    "items": [{"table_id": "tblA", "name": "唯一一张表"}],
                }
                self._send(200, {"code": 0, "msg": "ok", "data": payload})
                return

            payload = {
                "has_more": False,
                "page_token": "",
                "items": [{"table_id": "tblA", "name": "唯一一张表"}],
            }
            self._send(200, {"code": 0, "msg": "ok", "data": payload})
            return

        if kind == "fields":
            STATE["fields_hit"][app_token] = STATE["fields_hit"].get(app_token, 0) + 1
            payload = {
                "has_more": False,
                "page_token": "",
                "items": [
                    {"field_id": "fld1", "field_name": "订单号", "type": 1},
                    {"field_id": "fld2", "field_name": "金额", "type": 2},
                ],
            }
            self._send(200, {"code": 0, "msg": "ok", "data": payload})
            return

        self._send(404, {"code": 404, "msg": "not found"})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    print(f"PORT {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
