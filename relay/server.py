"""标准库 HTTP 运行服务。

    python3 -m relay.server --samples samples --state var/state --port 8080

变更类接口接受可选 `at`（演练秒），缺省取服务时钟；`/v1/vendor/advance`
推进合成供应商时间并释放到期的回执与周期账单。状态经日志持久化，
重启进程后已发未决请求与已给出的决定保持不变。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from relay.core import Refused, RelayService
from relay.inventory import load_config
from relay.vendor import SimVendor


def _fields(body, *names):
    missing = [name for name in names if name not in body]
    if missing:
        raise Refused("missing_field", fields=missing)
    return [body[name] for name in names]


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "Relay/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise Refused("bad_json", detail=str(error))
        if not isinstance(body, dict):
            raise Refused("bad_json", detail="请求体应为对象")
        return body

    def log_message(self, format, *args):  # 静默访问日志
        pass

    # ------------------------------------------------------------ 路由

    def do_GET(self):
        path, _, query = urlparse(self.path).path.partition("?")
        parts = [part for part in path.split("/") if part]
        try:
            if parts == ["v1", "health"]:
                return self._respond(200, {"status": "ok", "clock": self._service().clock})
            if len(parts) == 4 and parts[:2] == ["v1", "tasks"]:
                view = self._locked(lambda svc: svc.customer_view(parts[2], parts[3]))
                return self._respond(200, view)
            if len(parts) == 4 and parts[:2] == ["v1", "quota"]:
                view = self._locked(lambda svc: svc.quota_view(parts[2], parts[3]))
                return self._respond(200, view)
            if len(parts) == 3 and parts[:2] == ["v1", "reconciliation"]:
                tenant = parse_qs(query).get("tenant", [None])[0]
                view = self._locked(lambda svc: svc.reconciliation(parts[2], tenant))
                return self._respond(200, view)
            self._respond(404, {"error": "not_found"})
        except Refused as error:
            self._respond(409, {"error": error.reason, "detail": error.detail})

    def do_POST(self):
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        try:
            body = self._body()
            handler = self._routes().get(tuple(parts))
            if handler is None:
                return self._respond(404, {"error": "not_found"})
            self._respond(200, handler(body))
        except Refused as error:
            self._respond(409, {"error": error.reason, "detail": error.detail})
        except TypeError as error:
            self._respond(400, {"error": "bad_request", "detail": str(error)})

    def _routes(self):
        svc_post = lambda method: lambda body: self._locked(
            lambda svc: method(svc, **body))
        return {
            ("v1", "intents"): svc_post(lambda s, **b: s.intent(**b)),
            ("v1", "dispatches"): svc_post(lambda s, **b: s.dispatch(**b)),
            ("v1", "cancels"): svc_post(lambda s, **b: s.cancel(**b)),
            ("v1", "conflict-decisions"): svc_post(lambda s, **b: s.decide_conflict(**b)),
            ("v1", "followups"): svc_post(lambda s, **b: s.followup(**b)),
            ("v1", "pauses"): svc_post(lambda s, **b: s.pause(**b)),
            ("v1", "resumes"): svc_post(lambda s, **b: s.resume(**b)),
            ("v1", "tracks"): svc_post(lambda s, **b: s.track(**b)),
            ("v1", "vendor", "receipts"): svc_post(lambda s, **b: s.receipt(**b)),
            ("v1", "vendor", "bills"): svc_post(lambda s, **b: s.bill(**b)),
            ("v1", "vendor", "advance"): self._advance,
        }

    # ------------------------------------------------------------ 依赖

    def _service(self) -> RelayService:
        return self.server.relay_service

    def _locked(self, fn):
        with self.server.relay_lock:
            return fn(self._service())

    def _advance(self, body):
        (to,) = _fields(body, "to")
        with self.server.relay_lock:
            return self.server.relay_vendor.advance(to)


def make_server(samples: Path, state: Path, port: int) -> ThreadingHTTPServer:
    config = load_config(samples)
    service = RelayService.restore(config, Path(state) / "journal.jsonl")
    vendor = SimVendor(service, config)
    service.gateway = vendor
    server = ThreadingHTTPServer(("127.0.0.1", port), RelayHandler)
    server.relay_service = service
    server.relay_vendor = vendor
    server.relay_lock = threading.Lock()
    return server


def main():
    parser = argparse.ArgumentParser(description="外发额度与客户结果服务")
    parser.add_argument("--samples", type=Path, default=Path("samples"), help="清册目录")
    parser.add_argument("--state", type=Path, default=Path("var/state"), help="状态目录")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = make_server(args.samples, args.state, args.port)
    print(f" listening on http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
