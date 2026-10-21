"""Shared fixtures: a stub GitHub API, the built engine, and jq discovery.

Nothing here touches the network or consumes GitHub rate limit.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Three bounties: a $10K one, a $200 one written with a currency code (and an
# emoji, to keep the console encoding honest), and one with no amount at all.
ITEMS = [
    {
        "number": 1,
        "title": "[ Bounty $10K ] Fix the widget parser",
        "html_url": "https://github.com/acme/widget/issues/1",
        "repository_url": "https://api.github.com/repos/acme/widget",
        "comments": 2,
        "created_at": "2026-05-01T00:00:00Z",
        "labels": [{"name": "\U0001f48e Bounty"}],
    },
    {
        "number": 2,
        "title": "Bounty: USD 200 for docs \U0001f4dd",
        "html_url": "https://github.com/acme/docs/issues/2",
        "repository_url": "https://api.github.com/repos/acme/docs",
        "comments": 0,
        "created_at": "2026-05-02T00:00:00Z",
        "labels": [{"name": "bounty"}],
    },
    {
        "number": 3,
        "title": "Update the README",
        "html_url": "https://github.com/acme/docs/issues/3",
        "repository_url": "https://api.github.com/repos/acme/docs",
        "comments": 12,
        "created_at": "2026-05-03T00:00:00Z",
        "labels": [{"name": "bounty"}],
    },
]


class StubGitHub:
    """A tiny stand-in for the GitHub search and repo APIs."""

    def __init__(self, items=None):
        self.items = ITEMS if items is None else items
        self.requests: list[str] = []
        self.script: list[tuple[int, dict, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                outer.requests.append(self.path)

                if outer.script:
                    status, headers, body = outer.script.pop(0)
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if body:
                        self.wfile.write(body.encode())
                    return

                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]

                if parsed.path == "/search/issues":
                    matched = outer.matching(query)
                    payload = {
                        "total_count": len(matched),
                        "incomplete_results": False,
                        "items": matched,
                    }
                elif parsed.path.startswith("/repos/"):
                    payload = {"language": "Go"}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return

                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # keep test output clean
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def matching(self, query: str) -> list[dict]:
        if "\U0001f48e Bounty" in query:
            return [self.items[0]]
        if "in:title" in query or "label:bounty" in query:
            return self.items[1:]
        return []

    def fail_next(self, status: int, headers=None, body: str = ""):
        self.script.append((status, headers or {}, body))

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _binary_path() -> Path:
    override = os.environ.get("BOUNTY_CORE_BIN")
    if override:
        return Path(override)

    name = "bounty-core.exe" if os.name == "nt" else "bounty-core"
    built = ROOT / "bin" / name
    if built.exists():
        return built

    go = os.environ.get("GO") or shutil.which("go")
    if not go:
        pytest.fail(
            "bounty-core binary not found and no Go toolchain on PATH; "
            "build it with: cd core && go build -o ../bin/bounty-core ."
        )

    built.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-o", str(built), "."], cwd=ROOT / "core", check=True)
    return built


@pytest.fixture(scope="session")
def binary() -> Path:
    return _binary_path()


@pytest.fixture
def stub():
    with StubGitHub() as server:
        yield server


@pytest.fixture
def stub_factory():
    created: list[StubGitHub] = []

    def make(**kwargs) -> StubGitHub:
        server = StubGitHub(**kwargs)
        server.__enter__()
        created.append(server)
        return server

    yield make

    for server in created:
        server.__exit__(None, None, None)


@pytest.fixture(scope="session")
def jq() -> str:
    found = os.environ.get("JQ") or shutil.which("jq")
    if not found:
        pytest.skip("jq is not installed (set JQ=/path/to/jq to run these tests)")
    return found
