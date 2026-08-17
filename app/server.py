"""LAN landing page for everything running on this box.

Serves a single page listing the running containers with a link to each web UI,
plus its public hostname when the reverse proxy in front of this box has one
pointing at it. Discovery lives in ``discovery.py``; this module is the HTTP
surface, the refresh loop, and the icon cache.

Zero required configuration: the Docker socket is the only input. The reverse
proxy is found by image, its config is read through the Docker API, and LAN URLs
are built from the Host header of the request being served -- so the same
container works unchanged on any host, and links follow however the visitor got
here (LAN address, Tailscale address, or hostname).

The page is unauthenticated and lists the whole internal port map, so it must
stay internal. That is enforced at the nginx edge (see ``nginx/lan-landing.conf``),
not here: this app has no reliable notion of who is asking.

Endpoints
    GET /              the page
    GET /api/apps      the same data as JSON, for the page's own polling
    GET /icon/<slug>   an icon, cached on disk, falling back to a letter tile
    GET /healthz       liveness, plus what discovery currently believes
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import docker
import requests
import urllib3
import yaml
from flask import Flask, Response, jsonify, render_template, request

import discovery

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lan-landing")

# Every setting below is optional. HOST_IPS exists only for the one case
# discovery cannot resolve on its own (a box publishing a single service, before
# anyone has loaded the page) and for overriding a wrong guess.
HOST_IPS = {ip.strip() for ip in os.environ.get("HOST_IPS", "").split(",") if ip.strip()}
# Escape hatch: point this at a bind-mounted config directory if the Docker
# socket is behind a proxy that blocks the archive endpoint.
CONFIG_DIR = os.environ.get("EDGE_CONFIG_DIR", "")
OVERRIDES_PATH = os.environ.get("OVERRIDES_PATH", "/config/apps.yml")
ICON_CACHE_DIR = Path(os.environ.get("ICON_CACHE_DIR", "/cache/icons"))
REFRESH_SECONDS = float(os.environ.get("REFRESH_SECONDS", "30"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "2.0"))
INCLUDE_STOPPED = os.environ.get("INCLUDE_STOPPED", "false").lower() in {"1", "true", "yes"}
SITE_TITLE = os.environ.get("SITE_TITLE", "Home Server")

ICON_SOURCES = (
    "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/webp/{slug}.webp",
    "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{slug}.svg",
)
# When the icon set has no match, ask the service itself for its favicon.
FAVICON_FALLBACK = os.environ.get("FAVICON_FALLBACK", "true").lower() in {"1", "true", "yes"}
# How long a "no icon anywhere" result is remembered. Bounded rather than
# permanent because a favicon lookup can fail for temporary reasons -- the
# service was still starting, or briefly down -- and should be retried later.
ICON_MISS_TTL = float(os.environ.get("ICON_MISS_TTL_HOURS", "24")) * 3600
FAVICON_MAX_BYTES = 512 * 1024

app = Flask(__name__)

_state_lock = threading.Lock()
_state: dict = {
    "apps": [],
    "updated": 0.0,
    "error": None,
    "edge": None,
    "host_ips": [],
    # Address of the host as seen from this container; where services are
    # probed, and where their favicons are fetched from.
    "probe_target": None,
}

# Addresses visitors have reached this page by. Each one is, by definition, an
# address of this box, which is exactly what upstream matching needs. Kept in
# memory only, so a restart re-learns rather than carrying a stale guess.
_seen_hosts: set[str] = set()


def _load_overrides() -> dict:
    """Read apps.yml, treating any problem with it as "no overrides".

    A typo in a hand-edited YAML file must not take the landing page down: the
    page falls back to auto-discovered names and icons and logs the reason.
    """
    path = Path(OVERRIDES_PATH)
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return {}


def _self_container(client: docker.DockerClient):
    """This container, found via the hostname docker sets to its own id.

    Used to hide this service from its own listing and to locate the host
    gateway. Returns None when that lookup fails -- running outside docker, or
    with a hostname overridden -- and every caller degrades rather than fails.
    """
    try:
        return client.containers.get(os.environ.get("HOSTNAME", ""))
    except (docker.errors.NotFound, docker.errors.NullResource, docker.errors.APIError):
        return None


def _self_gateway(me) -> str | None:
    """The docker gateway for this container -- i.e. the host, from in here.

    Used as the probe target: published ports are bound on all host interfaces,
    so connecting to the gateway tests the same listener the visitor will hit,
    without needing to know the box's LAN address. Host-network containers are
    also covered, since they too are bound on this interface.
    """
    if me is None:
        return None
    networks = (me.attrs.get("NetworkSettings", {}) or {}).get("Networks") or {}
    for cfg in networks.values():
        if cfg.get("Gateway"):
            return cfg["Gateway"]
    return None


def _refresh() -> None:
    """Rebuild the app list and probe each service. Called by the refresh loop."""
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    try:
        containers = discovery.list_containers(client, INCLUDE_STOPPED)

        edge = discovery.find_edge(client)
        if CONFIG_DIR:
            texts = discovery.read_config_dir(CONFIG_DIR)
        elif edge:
            texts = discovery.fetch_config_texts(client, edge)
        else:
            texts = {}
        proxy_hosts = discovery.parse_proxy_hosts(texts)

        published = {port for facts in containers for port in facts.host_ports}
        host_ips = discovery.infer_host_ips(
            proxy_hosts=proxy_hosts,
            published_ports=published,
            gateways=discovery.docker_gateways(client),
            seen_hosts=set(_seen_hosts),
            configured=HOST_IPS,
        )

        me = _self_container(client)
        apps = discovery.build_apps(
            containers=containers,
            proxy_hosts=proxy_hosts,
            overrides=_load_overrides(),
            host_ips=host_ips,
            edge=edge,
            self_name=me.name if me else "",
        )
        probe_target = _self_gateway(me) or "127.0.0.1"
    finally:
        client.close()

    # Probe concurrently: a handful of unreachable ports would otherwise add
    # their full timeout each to every refresh.
    threads = []
    for item in apps:
        if not item.lan_port:
            item.online = None
            continue

        def check(target=item) -> None:
            target.online = discovery.probe_port(probe_target, target.lan_port, PROBE_TIMEOUT)

        thread = threading.Thread(target=check, daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=PROBE_TIMEOUT + 1)

    with _state_lock:
        _state["apps"] = apps
        _state["updated"] = time.time()
        _state["error"] = None
        _state["edge"] = (
            {"container": edge.container_name, "kind": edge.kind, "image": edge.image}
            if edge else None
        )
        _state["host_ips"] = sorted(host_ips)
        _state["probe_target"] = probe_target


def _refresh_loop() -> None:
    while True:
        try:
            _refresh()
        except Exception as exc:  # keep serving the last good snapshot
            log.exception("refresh failed")
            with _state_lock:
                _state["error"] = str(exc)
        time.sleep(REFRESH_SECONDS)


def _snapshot() -> dict:
    with _state_lock:
        return dict(_state)


def _request_host() -> str:
    """The bare host the visitor used, without the port.

    This is what LAN links are built from, so browsing over Tailscale or by
    hostname produces links that work from where the visitor actually is. An
    IPv6 literal keeps its brackets so the URL stays valid.
    """
    value = request.host or ""
    if value.startswith("["):
        head, _, _ = value.partition("]")
        return head + "]"
    return value.split(":")[0]


@app.before_request
def _remember_host() -> None:
    """Record the address this page was reached by, for upstream matching.

    Filtered to private IP literals -- a Host header is client-supplied, and
    without that check a visitor could name any address and have a public link
    attached to the wrong service.
    """
    host = _request_host()
    if host and host not in _seen_hosts and discovery.acceptable_request_host(host):
        _seen_hosts.add(host)
        log.info("learned local address %s from a request", host)


@app.route("/")
def index() -> str:
    snap = _snapshot()
    host = _request_host()
    items = [a.as_dict(host) for a in snap["apps"]]
    categories: dict[str, list[dict]] = {}
    for item in items:
        categories.setdefault(item["category"], []).append(item)
    return render_template(
        "index.html",
        title=SITE_TITLE,
        categories=categories,
        total=len(items),
        online=sum(1 for a in items if a["online"]),
        public=sum(1 for a in items if a["public_urls"]),
        updated=snap["updated"],
        error=snap["error"],
    )


@app.route("/api/apps")
def api_apps() -> Response:
    snap = _snapshot()
    host = _request_host()
    return jsonify({
        "apps": [a.as_dict(host) for a in snap["apps"]],
        "updated": snap["updated"],
        "error": snap["error"],
        "edge": snap["edge"],
        "host_ips": snap["host_ips"],
    })


@app.route("/healthz")
def healthz():
    snap = _snapshot()
    ok = snap["updated"] > 0
    return jsonify({
        "ok": ok,
        "apps": len(snap["apps"]),
        "edge": snap["edge"],
        "host_ips": snap["host_ips"],
        "error": snap["error"],
    }), (200 if ok else 503)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_MIME_BY_SUFFIX = {
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}
_SUFFIX_BY_MIME = {
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}
# Magic numbers, for servers that hand back a favicon as octet-stream.
_IMAGE_MAGIC = (
    (b"\x00\x00\x01\x00", ".ico"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
    (b"\xff\xd8\xff", ".jpg"),
)
_MISS_SUFFIX = ".miss"


def _cached_icon(slug: str) -> tuple[bytes, str] | str | None:
    """Look up ``slug`` in the cache.

    Returns the bytes and mime type on a hit, the string ``"miss"`` when a
    still-valid negative marker exists, or None when nothing is cached and the
    upstream sources should be tried.
    """
    for path in sorted(ICON_CACHE_DIR.glob(f"{slug}.*")):
        # A zero-byte file is a negative marker: the old format wrote it with an
        # image suffix, the current one uses .miss. Both are honoured, and both
        # expire so a transient failure is not remembered forever.
        if path.suffix == _MISS_SUFFIX or path.stat().st_size == 0:
            if time.time() - path.stat().st_mtime < ICON_MISS_TTL:
                return "miss"
            path.unlink(missing_ok=True)
            continue
        mime = _MIME_BY_SUFFIX.get(path.suffix)
        if mime:
            return path.read_bytes(), mime
    return None


def _fetch_from_icon_set(slug: str) -> tuple[bytes, str] | None:
    """Try the dashboard-icons set, which is the preferred source."""
    for template in ICON_SOURCES:
        url = template.format(slug=slug)
        try:
            resp = requests.get(url, timeout=6)
        except requests.RequestException as exc:
            log.debug("icon set fetch failed for %s: %s", slug, exc)
            continue
        if resp.status_code == 200 and resp.content:
            return resp.content, ("image/svg+xml" if url.endswith(".svg") else "image/webp")
    return None


_RE_ICON_LINK = re.compile(
    r"""<link\b[^>]*\brel\s*=\s*["']?([^"'>]*icon[^"'>]*)["']?[^>]*>""",
    re.I,
)
_RE_HREF = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.I)


def _favicon_candidates(base: str, html: str) -> list[str]:
    """URLs worth trying for a service's own icon, best first.

    Parsed with a regex rather than an HTML library to avoid a dependency for
    one tag. Precision is not important here: a wrong candidate simply fails the
    image check and the next one is tried. apple-touch-icons come first because
    they are the largest and squarest thing most apps ship, which suits a tile.
    """
    from urllib.parse import urljoin

    touch, regular = [], []
    for match in _RE_ICON_LINK.finditer(html or ""):
        tag = match.group(0)
        rel = match.group(1).lower()
        href = _RE_HREF.search(tag)
        if not href:
            continue
        url = urljoin(base + "/", href.group(1).strip())
        (touch if "apple-touch" in rel else regular).append(url)

    # /favicon.ico last but always tried: plenty of services ship one without
    # ever declaring it, and it is often reachable even when / needs a login.
    return [*touch, *regular, urljoin(base + "/", "/favicon.ico")]


def _fetch_favicon(scheme: str, host: str, port: int) -> tuple[bytes, str] | None:
    """Ask the service itself for its icon.

    Fetched server-side from the host address this container can reach, so it
    works for services the visitor's browser could not query directly, and the
    result is cached like any other icon -- the page still makes no third-party
    requests.

    TLS verification is off on purpose: these are LAN services, overwhelmingly
    with self-signed certificates, and the payload is a decorative image whose
    bytes are validated as an image before use.
    """
    base = f"{scheme}://{host}:{port}"
    session = requests.Session()
    session.verify = False
    session.headers["User-Agent"] = "homelab-dashboard/1.0 (favicon probe)"
    # Suppressed for this session only: verify=False is intentional above, and
    # the warning would otherwise repeat for every https service on every miss.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    html = ""
    try:
        landing = session.get(base + "/", timeout=4, allow_redirects=True)
        if "html" in landing.headers.get("Content-Type", "").lower():
            html = landing.text[:200_000]
    except requests.RequestException as exc:
        log.debug("favicon: cannot read %s: %s", base, exc)

    for url in _favicon_candidates(base, html):
        try:
            # Streamed with a hard byte cap so a service that answers /favicon.ico
            # with a video file, or an endless stream, cannot fill the cache.
            with session.get(url, timeout=4, allow_redirects=True, stream=True) as resp:
                if resp.status_code != 200:
                    continue
                data = resp.raw.read(FAVICON_MAX_BYTES + 1, decode_content=True) or b""
                content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        except requests.RequestException:
            continue

        if not data or len(data) > FAVICON_MAX_BYTES:
            continue

        suffix = _SUFFIX_BY_MIME.get(content_type)
        if suffix is None:
            # Content-Type was wrong or generic: identify by magic instead, and
            # accept SVG only when it really opens like markup.
            for magic, guessed in _IMAGE_MAGIC:
                if data.startswith(magic):
                    suffix = guessed
                    break
            else:
                head = data[:200].lstrip().lower()
                if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:500].lower()):
                    suffix = ".svg"
        if suffix is None:
            continue

        log.info("favicon: using %s for %s", url, base)
        return data, _MIME_BY_SUFFIX[suffix]
    return None


def _service_endpoint(app_key: str) -> tuple[str, str, int] | None:
    """Where to fetch ``app_key``'s favicon from: (scheme, host, port)."""
    snap = _snapshot()
    host = snap.get("probe_target")
    if not host:
        return None
    for item in snap["apps"]:
        if item.key == app_key and item.lan_port:
            return item.scheme, host, item.lan_port
    return None


@app.route("/icon/<slug>")
def icon(slug: str) -> Response:
    """Serve a service icon: the icon set, then the service's own favicon, then a tile.

    ``slug`` is validated against a strict pattern before being used as a
    filename or interpolated into an upstream URL -- it comes from image names
    and a hand-edited YAML file, so it is not trusted as a path. The optional
    ``?app=`` parameter names the container to ask for a favicon, and is only
    ever compared against the known app list, never used to build a path.
    """
    if not _SLUG_RE.match(slug) or ".." in slug:
        return _letter_tile("?")

    ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cached_icon(slug)
    if cached == "miss":
        return _letter_tile(slug[0])
    if isinstance(cached, tuple):
        return _serve_icon(*cached)

    found = _fetch_from_icon_set(slug)

    if not found and FAVICON_FALLBACK:
        endpoint = _service_endpoint(request.args.get("app", ""))
        if endpoint:
            found = _fetch_favicon(*endpoint)

    if found:
        data, mime = found
        suffix = _SUFFIX_BY_MIME.get(mime, ".webp")
        (ICON_CACHE_DIR / f"{slug}{suffix}").write_bytes(data)
        return _serve_icon(data, mime)

    # Remember the miss so a name with no icon anywhere is not looked up on every
    # page load. The marker expires (ICON_MISS_TTL); delete it to retry sooner.
    (ICON_CACHE_DIR / f"{slug}{_MISS_SUFFIX}").write_bytes(b"")
    return _letter_tile(slug[0])


def _serve_icon(data: bytes, mime: str) -> Response:
    return Response(data, mimetype=mime,
                    headers={"Cache-Control": "public, max-age=604800"})


def _letter_tile(letter: str) -> Response:
    """Fallback icon: the service's initial on a neutral tile."""
    safe = (letter or "?").upper().replace("&", "").replace("<", "")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#2a3550"/>'
        f'<text x="32" y="43" font-family="system-ui,sans-serif" font-size="32" '
        f'font-weight="600" fill="#8fa6d8" text-anchor="middle">{safe}</text></svg>'
    )
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# Started at import so it runs under gunicorn as well as `flask run`.
threading.Thread(target=_refresh_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8500")))
