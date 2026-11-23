#!/usr/bin/env python3
"""Create a private GitHub App from a manifest, with one click in the browser.

Usage: create-app.py <app-name> <homepage-url> <out-dir>
Opens http://localhost:18765, which posts the manifest to GitHub. After you
click "Create GitHub App", GitHub redirects back here with a code that is
exchanged for the app's ID, slug and private key. The key is written to
<out-dir>/<slug>.pem (mode 600) and never printed.
"""
import html
import json
import os
import sys
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 18765
name, homepage, out_dir = sys.argv[1:4]
manifest = {
    "name": name,
    "url": homepage,
    "hook_attributes": {"url": homepage, "active": False},
    "redirect_url": f"http://localhost:{PORT}/callback",
    "public": False,
    "default_permissions": {"pull_requests": "write", "metadata": "read"},
    "default_events": [],
}
done = {}


class Handler(BaseHTTPRequestHandler):
    def _page(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._page(
                '<form id="f" method="post" action="https://github.com/settings/apps/new">'
                f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">'
                "</form><script>document.getElementById('f').submit()</script>"
            )
        elif url.path == "/callback":
            code = parse_qs(url.query).get("code", [""])[0]
            req = urllib.request.Request(
                f"https://api.github.com/app-manifests/{code}/conversions",
                method="POST",
                headers={"Accept": "application/vnd.github+json"},
            )
            app = json.load(urllib.request.urlopen(req))
            pem_path = os.path.join(out_dir, f"{app['slug']}.pem")
            fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(app["pem"])
            done.update(id=app["id"], slug=app["slug"], pem=pem_path,
                        install_url=f"{app['html_url']}/installations/new")
            self._page(f"<p>App {html.escape(app['slug'])} created. You can close this tab.</p>")
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", PORT), Handler)
webbrowser.open(f"http://localhost:{PORT}/")
while not done:
    server.handle_request()
print(json.dumps(done))
