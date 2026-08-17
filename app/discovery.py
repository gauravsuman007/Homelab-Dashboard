"""Discovery of the services this box exposes, and how to reach each one.

Everything is derived at runtime from the Docker daemon; there is no per-host
configuration to fill in.

Three questions have to be answered, and each has its own source:

1. *What is running?* -- the Docker Engine API over the mounted socket. Also the
   authority on published ports and network aliases.

2. *What is public?* -- the reverse proxy's own generated config. The edge
   container is found by image name, and its config is read **through the Docker
   API's archive endpoint** (the same mechanism as `docker cp`), so no bind mount
   and no knowledge of host paths is needed. This also works when the config
   lives in a named volume rather than on the host filesystem.

3. *Which addresses mean "this box"?* -- needed to tell "this proxy host points
   at a service here" from "it points at the NAS". Inferred from the docker
   network gateways, from the proxy config itself (see ``infer_host_ips``), and
   from the addresses visitors actually reach the page by.

The LAN URL of a service is deliberately *not* stored here. It is built per
request from the Host header, so visiting over Tailscale yields Tailscale links
and visiting by hostname yields hostname links, with nothing configured.

A hand-written overrides file (apps.yml) supplies display names, icons,
categories, and the one fact that cannot be discovered: the port of a container
on the host network, which publishes nothing the Docker API can report.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import re
import socket
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import docker

log = logging.getLogger(__name__)

# Ports that are never a web UI. Used only to *rank* candidate ports for a
# container that publishes several; never to hide a container outright.
NON_HTTP_PORTS = {22, 53, 3306, 5432, 6379, 9092, 11211, 27017, 51820}

# Images whose containers are infrastructure with no browsable UI. Matched as a
# substring of the image reference, case-insensitively. An explicit apps.yml
# entry always wins over this list.
INFRA_IMAGE_MARKERS = (
    "redis", "valkey", "mariadb", "mysql", "postgres", "pgvecto", "mongo",
    "memcached", "rabbitmq", "watchtower", "gluetun", "tailscale",
    "flaresolverr", "buildarr", "cloudflared",
)

# Reverse proxies we know how to read, most specific match first. `config_path`
# is the directory *inside that container* holding the generated server blocks.
EDGE_KINDS = (
    {"marker": "nginx-proxy-manager", "kind": "npm", "config_path": "/data/nginx/proxy_host"},
)


@dataclass
class Edge:
    """The reverse-proxy container in front of this box, if there is one."""

    container_name: str
    kind: str
    config_path: str
    image: str


@dataclass
class ProxyHost:
    """One public hostname served by the edge, and the upstream it forwards to."""

    domain: str
    upstream_host: str
    upstream_port: int
    https: bool

    @property
    def url(self) -> str:
        return f"{'https' if self.https else 'http'}://{self.domain}"


@dataclass
class App:
    """A single card on the landing page.

    ``lan_port`` rather than a LAN URL: the URL is assembled per request from
    the Host header. ``lan_url_override`` holds an absolute URL from apps.yml for
    the rare service that lives somewhere else entirely.
    """

    key: str  # container name; stable identity used by apps.yml
    name: str
    icon: str
    category: str
    image: str
    running: bool
    lan_port: int | None = None
    lan_url_override: str | None = None
    scheme: str = "http"
    public_urls: list[str] = field(default_factory=list)
    online: bool | None = None  # None = not probed

    def lan_url(self, request_host: str | None) -> str | None:
        """The LAN URL as seen from ``request_host`` (a bare host, no port)."""
        if self.lan_url_override:
            return self.lan_url_override
        if not (self.lan_port and request_host):
            return None
        return f"{self.scheme}://{request_host}:{self.lan_port}"

    def as_dict(self, request_host: str | None = None) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "icon": self.icon,
            "category": self.category,
            "image": self.image,
            "running": self.running,
            "lan_port": self.lan_port,
            "lan_url": self.lan_url(request_host),
            "public_urls": self.public_urls,
            "online": self.online,
        }


# --------------------------------------------------------------------------
# Reading the edge proxy's config, without a bind mount
# --------------------------------------------------------------------------

def find_edge(client: docker.DockerClient) -> Edge | None:
    """Locate the reverse-proxy container by image, or None if there is none.

    Matching on the image rather than the container name means a compose project
    called anything at all is still found. With no edge, the page simply shows
    LAN links -- which is the correct result for a box that publishes nothing.
    """
    for container in client.containers.list():
        image = (container.image.tags or [""])[0] if container.image else ""
        reference = f"{image} {container.name}".lower()
        for kind in EDGE_KINDS:
            if kind["marker"] in reference:
                return Edge(
                    container_name=container.name,
                    kind=kind["kind"],
                    config_path=kind["config_path"],
                    image=image,
                )
    return None


def fetch_config_texts(client: docker.DockerClient, edge: Edge) -> dict[str, str]:
    """Pull ``edge.config_path`` out of the edge container as {filename: text}.

    Uses the Docker API's archive endpoint -- what `docker cp` is built on -- so
    the config is readable with the socket alone: no bind mount, no host paths,
    and it works when the config sits in a named volume. The files are a few kB
    in total, so refetching them each scan costs nothing and means an edit made
    in the NPM UI shows up within one cycle.
    """
    try:
        container = client.containers.get(edge.container_name)
        bits, _ = container.get_archive(edge.config_path)
    except docker.errors.APIError as exc:
        log.warning("cannot read %s from %s: %s", edge.config_path, edge.container_name, exc)
        return {}

    texts: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(b"".join(bits))) as archive:
            for member in archive.getmembers():
                if not (member.isfile() and member.name.endswith(".conf")):
                    continue
                handle = archive.extractfile(member)
                if handle:
                    texts[member.name] = handle.read().decode("utf-8", "replace")
    except tarfile.TarError as exc:
        log.warning("malformed archive from %s: %s", edge.container_name, exc)
    return texts


def read_config_dir(directory: str | Path) -> dict[str, str]:
    """Read server blocks from a bind-mounted directory instead of the API.

    Retained as an escape hatch: if the socket is proxied through something that
    filters the archive endpoint, mounting the config read-only still works.
    """
    path = Path(directory)
    if not path.is_dir():
        return {}
    texts = {}
    for conf in sorted(path.glob("*.conf")):
        try:
            texts[conf.name] = conf.read_text(errors="replace")
        except OSError as exc:
            log.warning("cannot read %s: %s", conf, exc)
    return texts


# --------------------------------------------------------------------------
# NPM config parsing
# --------------------------------------------------------------------------

_RE_SERVER_NAME = re.compile(r"^\s*server_name\s+([^;]+);", re.M)
_RE_UPSTREAM_HOST = re.compile(r"^\s*set\s+\$server\s+\"?([^\";]+?)\"?\s*;", re.M)
_RE_UPSTREAM_PORT = re.compile(r"^\s*set\s+\$port\s+\"?(\d+)\"?\s*;", re.M)
_RE_SSL_CERT = re.compile(r"^\s*ssl_certificate\s+\S+;", re.M)


def parse_proxy_hosts(texts: dict[str, str]) -> list[ProxyHost]:
    """Turn NPM's generated server blocks into public-hostname mappings.

    NPM writes the upstream as ``set $server``/``set $port`` and one
    ``server_name`` line that may carry several names; each name becomes its own
    mapping over the same upstream. A block counts as HTTPS only when it really
    references a certificate, so a plain-HTTP host is never advertised as https
    and left broken in the UI.
    """
    hosts: list[ProxyHost] = []
    for name, text in sorted(texts.items()):
        upstream = _RE_UPSTREAM_HOST.search(text)
        port = _RE_UPSTREAM_PORT.search(text)
        names = _RE_SERVER_NAME.search(text)
        if not (upstream and port and names):
            log.debug("%s is not a recognisable proxy host, skipping", name)
            continue
        https = bool(_RE_SSL_CERT.search(text))
        for domain in names.group(1).split():
            hosts.append(ProxyHost(domain, upstream.group(1).strip(), int(port.group(1)), https))
    return hosts


# --------------------------------------------------------------------------
# "Which addresses are me?"
# --------------------------------------------------------------------------

def docker_gateways(client: docker.DockerClient) -> set[str]:
    """Every docker network gateway -- each one is an address of this host."""
    gateways: set[str] = set()
    for network in client.networks.list():
        for config in (network.attrs.get("IPAM") or {}).get("Config") or []:
            if config.get("Gateway"):
                gateways.add(config["Gateway"])
    return gateways


# Tailscale hands out addresses from 100.64.0.0/10. Python's is_private used to
# include that range but stopped in 3.12.4 (shared address space is not private),
# so it has to be named explicitly or tailnet addresses are rejected.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_local_scope_ip(value: str) -> bool:
    """True for an address that could plausibly belong to this box or its LAN.

    RFC1918, loopback and link-local via ``is_private``, plus the Tailscale
    range. Anything routable on the public internet is excluded: such an address
    is never a sensible upstream for a service on this machine.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_private or address in _CGNAT


def infer_host_ips(
    proxy_hosts: list[ProxyHost],
    published_ports: set[int],
    gateways: set[str],
    seen_hosts: set[str],
    configured: set[str],
) -> set[str]:
    """Work out which addresses refer to this machine.

    A container's bridge address is not the address a proxy host is written
    against, and the box's LAN address cannot be read from inside a
    bridge-networked container -- so it is inferred from three angles:

    * docker network gateways, which are host addresses by definition;
    * the proxy config itself: an upstream IP is taken as local when **two or
      more distinct** ports at that IP are also published on this host. Two is
      what separates a real local address (many matching ports) from a
      coincidence -- another machine that happens to run a service on a port
      also published here. On this box, 192.168.2.100 matches on a dozen ports
      while a NAS at .39 and an LLM host at .123 match on at most one, so they
      are correctly left out;
    * addresses visitors actually reach the page by (see ``note_request_host``),
      which by definition belong to this box.

    ``configured`` (from the environment) is always trusted and exists only for
    the case where a box publishes a single service, leaving the rule above one
    port short of certainty until someone loads the page.
    """
    ips = {"127.0.0.1", "localhost"} | set(configured) | set(gateways) | set(seen_hosts)

    ports_by_ip: dict[str, set[int]] = defaultdict(set)
    for host in proxy_hosts:
        if host.upstream_port in published_ports and _is_local_scope_ip(host.upstream_host):
            ports_by_ip[host.upstream_host].add(host.upstream_port)
    for ip, ports in ports_by_ip.items():
        if len(ports) >= 2:
            ips.add(ip)
    return ips


def acceptable_request_host(value: str) -> bool:
    """Whether a Host header may be remembered as an address of this box.

    Only private and tailnet IP literals are kept. A Host header is
    client-supplied and cannot be verified from inside the container, so without
    this filter a visitor could name any address and cause a public link to be
    attached to the wrong service. With it, and on a page only the LAN can reach,
    the worst case is a cosmetic mis-attribution by someone who already has LAN
    access -- and one that clears on restart, since the set is never persisted.

    This path is only a safety net anyway: ``infer_host_ips`` already resolves
    the box's address from the proxy config before anyone loads the page. It
    matters for a host publishing a single service, where the two-port rule
    cannot reach certainty.
    """
    return _is_local_scope_ip(value)


# --------------------------------------------------------------------------
# Docker inspection
# --------------------------------------------------------------------------

@dataclass
class ContainerFacts:
    """The subset of a container's state that matters for building a card."""

    name: str
    image: str
    running: bool
    host_ports: list[int]
    internal_ports: list[int]
    aliases: set[str]
    labels: dict[str, str]
    host_network: bool


def _container_facts(container) -> ContainerFacts:
    attrs = container.attrs
    net = attrs.get("NetworkSettings", {}) or {}
    config = attrs.get("Config", {}) or {}

    host_ports: set[int] = set()
    internal_ports: set[int] = set()
    for spec, bindings in (net.get("Ports") or {}).items():
        if not spec.endswith("/tcp"):
            continue
        try:
            internal_ports.add(int(spec.split("/")[0]))
        except ValueError:
            continue
        for binding in bindings or []:
            try:
                host_ports.add(int(binding["HostPort"]))
            except (KeyError, TypeError, ValueError):
                continue
    for spec in (config.get("ExposedPorts") or {}):
        if spec.endswith("/tcp"):
            try:
                internal_ports.add(int(spec.split("/")[0]))
            except ValueError:
                pass

    aliases = {container.name}
    for netcfg in (net.get("Networks") or {}).values():
        for alias in (netcfg.get("Aliases") or []) + (netcfg.get("DNSNames") or []):
            aliases.add(alias)

    if container.image and container.image.tags:
        image = container.image.tags[0]
    else:
        image = config.get("Image", "") or ""

    return ContainerFacts(
        name=container.name,
        image=image,
        running=container.status == "running",
        host_ports=sorted(host_ports),
        internal_ports=sorted(internal_ports),
        aliases=aliases,
        labels=config.get("Labels") or {},
        host_network=(attrs.get("HostConfig", {}) or {}).get("NetworkMode") == "host",
    )


def list_containers(client: docker.DockerClient, include_stopped: bool) -> list[ContainerFacts]:
    return [_container_facts(c) for c in client.containers.list(all=include_stopped)]


# --------------------------------------------------------------------------
# Joining it together
# --------------------------------------------------------------------------

def _is_infra(facts: ContainerFacts) -> bool:
    image = facts.image.lower()
    return any(marker in image for marker in INFRA_IMAGE_MARKERS)


def _rank_port(port: int, npm_ports: set[int], preferred: int | None) -> tuple:
    """Sort key for choosing which published port is *the* web UI.

    Preference order: the port apps.yml names, then a port the edge already
    proxies (someone deliberately pointed a hostname at it, so it is certainly a
    UI), then anything that is not a known non-HTTP service port, then lowest.
    """
    return (port != preferred, port not in npm_ports, port in NON_HTTP_PORTS, port)


def _match_public(facts: ContainerFacts, chosen_port: int | None,
                  proxy_hosts: list[ProxyHost], host_ips: set[str]) -> list[str]:
    """Public URLs that resolve to this container.

    Two ways a proxy host can point at a container:

    * by host address and published port -- the usual case, where the edge runs
      on its own bridge network and reaches services via the LAN address;
    * by container name/alias and *internal* port -- used when the service
      shares a docker network with the edge, which is the only way a container
      publishing nothing can still be public.
    """
    urls: list[str] = []
    for host in proxy_hosts:
        by_host = (
            host.upstream_host in host_ips
            and chosen_port is not None
            and host.upstream_port == chosen_port
        )
        by_alias = (
            host.upstream_host in facts.aliases
            and host.upstream_port in facts.internal_ports
        )
        if by_host or by_alias:
            urls.append(host.url)
    return sorted(set(urls))


def build_apps(
    containers: list[ContainerFacts],
    proxy_hosts: list[ProxyHost],
    overrides: dict,
    host_ips: set[str],
) -> list[App]:
    """Produce the list of cards to render.

    Visibility rule: a container is shown when it has something to click. That
    means a published port, an apps.yml entry giving it a port or url, or a
    proxy hostname pointing at one of its aliases. Anything else is plumbing.
    Images on the infra denylist are hidden unless apps.yml mentions them
    explicitly, which is what keeps databases and sidecars off the page even
    when they do publish a port.
    """
    apps_cfg: dict = overrides.get("apps") or {}
    defaults: dict = overrides.get("defaults") or {}
    hide_keys = {str(k) for k in (overrides.get("hide") or [])}
    npm_ports = {h.upstream_port for h in proxy_hosts if h.upstream_host in host_ips}

    apps: list[App] = []
    for facts in containers:
        cfg = apps_cfg.get(facts.name)
        if cfg is None:
            # Allow matching on a prefix so compose-generated names such as
            # "audiobookbay_downloader-jackett-1" can be configured under a
            # readable key without pinning the project's scale suffix.
            for key, value in apps_cfg.items():
                if facts.name.startswith(str(key)):
                    cfg = value
                    break
        cfg = dict(cfg or {})

        if facts.name in hide_keys or cfg.get("hidden"):
            continue
        explicit = bool(cfg)
        if _is_infra(facts) and not explicit:
            continue

        preferred = cfg.get("port")
        candidates = list(facts.host_ports)
        if preferred and preferred not in candidates:
            candidates.append(int(preferred))
        candidates.sort(key=lambda p: _rank_port(p, npm_ports, preferred))
        chosen_port = candidates[0] if candidates else None

        public_urls = _match_public(facts, chosen_port, proxy_hosts, host_ips)
        if not chosen_port and not cfg.get("url") and not public_urls:
            continue

        apps.append(
            App(
                key=facts.name,
                name=cfg.get("name") or _prettify(facts.name),
                icon=cfg.get("icon") or _icon_slug(facts),
                category=cfg.get("category") or defaults.get("category", "Other"),
                image=facts.image,
                running=facts.running,
                lan_port=chosen_port,
                lan_url_override=cfg.get("url"),
                scheme=cfg.get("scheme", "http"),
                public_urls=public_urls,
            )
        )

    apps.sort(key=lambda a: (a.category.lower(), a.name.lower()))
    return apps


def _prettify(name: str) -> str:
    """Turn a container name into something readable.

    Compose names carry a project prefix and a numeric scale suffix
    ("audiobookbay_downloader-jackett-1"); both are noise on a card, so the most
    specific segment is kept.
    """
    cleaned = re.sub(r"-\d+$", "", name)
    parts = re.split(r"[-_]", cleaned)
    if len(parts) > 1:
        parts = [parts[-1]] if len(parts[-1]) > 2 else parts
    return " ".join(p.capitalize() for p in parts if p)


def _icon_slug(facts: ContainerFacts) -> str:
    """Best-effort icon slug from the image, e.g. lscr.io/linuxserver/radarr:latest -> radarr."""
    ref = facts.image.split("@")[0]
    ref = ref.rsplit(":", 1)[0] if "/" not in ref.rsplit(":", 1)[-1] else ref
    slug = ref.rsplit("/", 1)[-1] or facts.name
    return re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-")


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------

def probe_port(host: str, port: int, timeout: float) -> bool:
    """True when something is accepting TCP connections on ``host:port``.

    A TCP connect is deliberate rather than an HTTP request: several services
    answer 401/302/upgrade-required at ``/``, and some are slow to render, so any
    HTTP-status rule would mark healthy services down. Reachability is what a
    link needs.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
