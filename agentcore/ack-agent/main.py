"""Minimal AgentCore HTTP agent: acknowledges a GitHub pull request event.

Implements the AgentCore HTTP contract with the standard library only:
GET /ping for health, POST /invocations for requests, on port 8080.
It logs one JSON line per request (CloudWatch) and returns an ack right away.
"""
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(record):
    print(json.dumps(record), file=sys.stdout, flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            self._send(200, {"status": "Healthy"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/invocations":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            event = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "payload is not JSON"})
            return
        ack = {
            "ack": True,
            "at": datetime.now(timezone.utc).isoformat(),
            "session": self.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"),
            "repo": event.get("repo"),
            "number": event.get("number"),
            "action": event.get("action"),
            "url": event.get("url"),
            "delivery": event.get("delivery"),
        }
        log({"msg": "pull request acknowledged", **ack})
        self._send(200, ack)

    def log_message(self, *args):  # silence default access log
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
