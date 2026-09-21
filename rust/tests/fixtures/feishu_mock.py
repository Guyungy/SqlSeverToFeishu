#!/usr/bin/env python3
"""飞书开放平台的本地桩服务，供 Rust 集成测试使用。

只实现同步流程真正会用到的那几个接口：
  POST /open-apis/auth/v3/tenant_access_token/internal
  GET  /open-apis/bitable/v1/apps/{app_token}/tables
  GET  /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields

行为由 app_token 的前缀决定，这样同一个桩能覆盖多种分支而不用重启：

  appPaging...     /tables 分两页返回（验证分页拼接）
  appRetry429...   /tables 首次返回 HTTP 429（验证退避重试）
  appStaleTkn...   /tables 首次返回 code 99991663（验证令牌失效后重新取号）
  appBizErr...     /tables 返回业务错误码（验证错误直接抛出而不是静默重试）
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

    def _record(self, status, code):
        auth = "yes" if self.headers.get("Authorization", "").startswith("Bearer ") else "no"
        log(f"{self.command} {self.path} -> {status} code={code} auth={auth}")

    # 令牌接口不校验 Authorization。
    def do_POST(self):
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
