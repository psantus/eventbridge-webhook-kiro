"""Minimal AgentCore HTTP agent: acknowledges a GitHub pull request event.

Implements the AgentCore HTTP contract: GET /ping for health and
POST /invocations for requests, on port 8080. For each PR event it logs one
JSON line (CloudWatch) and returns an ack.

If GITHUB_APP_ID and GITHUB_APP_KMS_KEY are set, it also comments on the PR as
the GitHub App: it signs the App JWT with AWS KMS (the App's private key was
imported into KMS and never leaves it), exchanges it for a 1-hour installation
token limited to the PR's repository, and posts the comment. The execution role
needs kms:Sign on that key.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_ID = os.environ.get("GITHUB_APP_ID")
KMS_KEY = os.environ.get("GITHUB_APP_KMS_KEY")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB = "https://api.github.com"


def log(record):
    print(json.dumps(record), file=sys.stdout, flush=True)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt():
    import boto3  # imported lazily: only needed to comment

    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": APP_ID}).encode())
    signing_input = f"{header}.{claims}".encode()
    sig = boto3.client("kms").sign(
        KeyId=KMS_KEY, Message=signing_input, MessageType="RAW",
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )["Signature"]
    return f"{header}.{claims}.{b64url(sig)}"


def github(method, path, token, body=None):
    req = urllib.request.Request(
        GITHUB + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kiro-gh-ack-agent",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def comment_on_pr(repo, number, text):
    jwt = app_jwt()
    installation = github("GET", f"/repos/{repo}/installation", jwt)
    token = github(
        "POST", f"/app/installations/{installation['id']}/access_tokens", jwt,
        {"repositories": [repo.split("/", 1)[1]], "permissions": {"pull_requests": "write"}},
    )["token"]
    return github("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": text})["html_url"]


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
        repo, number = ack["repo"], ack["number"]
        if APP_ID and KMS_KEY and isinstance(repo, str) and REPO_RE.match(repo) and isinstance(number, int):
            try:
                ack["comment"] = comment_on_pr(
                    repo, number,
                    f"Acknowledged `{ack['action']}` on this pull request "
                    f"(delivery `{ack['delivery']}`), via EventBridge Webhook Events and AgentCore.",
                )
            except (urllib.error.URLError, KeyError, ValueError) as exc:
                ack["comment_error"] = str(exc)
            except Exception as exc:  # boto3/KMS errors
                ack["comment_error"] = f"{type(exc).__name__}: {exc}"
        log({"msg": "pull request acknowledged", **ack})
        self._send(200, ack)

    def log_message(self, *args):  # silence default access log
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
