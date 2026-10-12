"""The dashboard is a module graph, and every edge of it has to resolve.

app.js used to be one <script> tag. It is now static/js/main.js importing nine
siblings through the /static mount. A single 404 in that graph takes the page down
silently - the browser writes it to the console, no request ever reaches the API,
and the user sees a form that does nothing - so the imports are walked over HTTP
rather than assumed, and any file in the directory that main.js does not reach is
reported as dead.
"""
import re
from urllib.parse import urljoin, urlsplit

import pytest
from fastapi.testclient import TestClient

from main import app, STATIC_DIR

IMPORT = re.compile(
    r"(?m)^import\s+(?:\{[^}]*\}|\*\s+as\s+\w+|[A-Za-z0-9_$]+)"
    r"\s+from\s+['\"]([^'\"]+)['\"]"
)
SCRIPT_SRC = re.compile(r"""<script[^>]*\bsrc=["']([^"']+)["']""")


@pytest.fixture()
def client():
    return TestClient(app)


def _reachables(client, start):
    seen, queue = set(), [start]
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        response = client.get(url)
        assert response.status_code == 200, f"{url} is imported but does not exist"
        assert "javascript" in response.headers["content-type"], url
        queue.extend(urljoin(url, spec) for spec in IMPORT.findall(response.text))
    return seen


def test_dashboard_loads_the_module_entry_point(client):
    page = client.get("/")
    assert page.status_code == 200
    sources = SCRIPT_SRC.findall(page.text)
    assert sources == ["/static/js/main.js"], sources
    assert 'type="module"' in page.text


def test_every_import_resolves_over_the_static_mount(client):
    reached = _reachables(client, "/static/js/main.js")
    assert len(reached) >= 2, reached
    # The page must not still ask for the file the modules replaced.
    assert "/static/app.js" not in SCRIPT_SRC.findall(client.get("/").text)


def test_no_module_is_orphaned(client):
    reached = {urlsplit(u).path.rsplit("/", 1)[-1]
               for u in _reachables(client, "/static/js/main.js")}
    on_disk = {p.name for p in (STATIC_DIR / "js").glob("*.js")}
    assert on_disk == reached, f"unreferenced modules: {sorted(on_disk - reached)}"
