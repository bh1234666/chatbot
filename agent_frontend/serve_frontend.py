from __future__ import annotations

import argparse
import http.client
import mimetypes
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    backend_host = "127.0.0.1"
    backend_port = 8000

    def log_message(self, fmt: str, *args) -> None:
        print("[agent_frontend]", fmt % args)

    def do_GET(self) -> None:
        if self.path.startswith("/v1/"):
            self.proxy()
            return
        self.serve_static()

    def do_POST(self) -> None:
        if self.path.startswith("/v1/"):
            self.proxy()
            return
        self.send_error(404)

    def do_PATCH(self) -> None:
        if self.path.startswith("/v1/"):
            self.proxy()
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        if self.path.startswith("/v1/"):
            self.proxy()
            return
        self.send_error(404)

    def serve_static(self) -> None:
        parsed = urlsplit(self.path)
        rel = parsed.path.lstrip("/") or "index.html"
        target = (ROOT / rel).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            self.send_error(403)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else None
        headers = {
            key: val for key, val in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=600)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for key, val in resp.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-encoding"}:
                    continue
                self.send_header(key, val)
            self.end_headers()
            content_type = resp.getheader("Content-Type") or ""
            if "text/event-stream" in content_type.lower():
                # SSE events are small and latency-sensitive. Reading large
                # chunks here buffers the UI until enough bytes accumulate.
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            else:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except BrokenPipeError:
            pass
        except (ConnectionError, TimeoutError, socket.timeout, http.client.HTTPException, OSError) as exc:
            self.send_response(502, "Bad Gateway")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"backend unavailable: {exc}".encode("utf-8", errors="replace"))
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--backend", default=os.environ.get("BOT_BACKEND", "127.0.0.1:8000"))
    args = parser.parse_args()
    if ":" in args.backend:
        Handler.backend_host, port = args.backend.rsplit(":", 1)
        Handler.backend_port = int(port)
    else:
        Handler.backend_host = args.backend
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"agent frontend: http://{args.host}:{args.port}")
    print(f"proxy backend: http://{Handler.backend_host}:{Handler.backend_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
