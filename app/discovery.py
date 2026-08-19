"""Discovery of the services this box exposes, and how to reach each one.

Everything is derived at runtime from the Docker daemon; there is no per-host
configuration to fill in.

Three questions have to be answered, and each has its own source:

1. *What is running?* -- the Docker Engine API over the mounted socket. Also the
   authority on published ports and network aliases.

2. *What is public?* -- the reverse proxy's own routing table. The edge
   container is found by image name, and where that table lives depends on the
   proxy. Nginx Proxy Manager generates server blocks, which are read **through
   the Docker API's archive endpoint** (the same mechanism as `docker cp`), so
   no bind mount and no knowledge of host paths is needed -- this works even
   when the config sits in a named volume. Traefik and caddy-docker-proxy
   instead keep the table in labels on the proxied containers, which have
   already been fetched, so there is nothing extra to read at all.

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
# `admin_port` is the container port of its own UI: the edge publishes the ports
# it proxies *for other services* (80/443), so without this its card would link
# to the proxy itself rather than to its admin panel.
# ``source`` says where the routing table lives, and it differs by proxy:
#
# * "config" -- the proxy writes generated server blocks into its own filesystem
#   (NPM), so they are read out of the container at ``config_path``.
# * "labels" -- the routing table *is* the labels on the proxied containers
#   (Traefik, caddy-docker-proxy), so nothing has to be read from the proxy at
#   all; the data already arrived with the container list.
#
# Order matters: the first marker found in an image reference wins.
EDGE_KINDS = (
    {
        "marker": "nginx-proxy-manager",
        "kind": "npm",
        "source": "config",
        "config_path": "/data/nginx/proxy_host",
        "admin_port": 81,
    },
    {
        "marker": "traefik",
        "kind": "traefik",
        "source": "labels",
        "config_path": "",
        # Traefik's dashboard, when it is enabled at all. 80/443 are what it
        # serves for everything else.
        "admin_port": 8080,
    },
    {
        "marker": "caddy",
        "kind": "caddy",
        "source": "labels",
        "config_path": "",
        # Caddy exposes an admin *API* on 2019, not a UI worth linking to.
        "admin_port": None,
    },
)

# Category for a service, by the first pattern that matches its name, compose
# service or image. Shipped in code rather than in config because it is the same
# for everyone: these are the common self-hosted apps, not this user's choices.
# Anything unmatched lands in "Other"; apps.yml overrides any of it.
CATEGORY_RULES = (
    (r"jellyfin|plex|emby|audiobookshelf|immich|navidrome|photoprism|stash|kavita"
     r"|komga|calibre|airsonic|volumio|musicassistant|lms|jellyseerr|seerr|overseerr"
     r"|ombi|jellystat", "Media"),
    (r"radarr|sonarr|lidarr|readarr|whisparr|prowlarr|bazarr|jackett|flaresolverr"
     r"|riven|buildarr|maintainerr|autobrr|recyclarr", "Automation"),
    (r"qbittorrent|transmission|deluge|sabnzbd|nzbget|jdownloader|pyload|metube"
     r"|aria2|rdt|decypharr|blackhole|torbox|audiobookbay|downloader|megabasterd"
     r"|slskd|yt-dlp", "Downloads"),
    (r"paperless|wordpress|bookstack|hedgedoc|nextcloud|projectsend|stirling"
     r"|docuseal|wiki", "Documents"),
    (r"portainer|nginx-proxy-manager|traefik|caddy|uptime|guacamole|homeassistant"
     r"|home-assistant|watchtower|dozzle|grafana|prometheus|adguard|pihole"
     r"|vaultwarden|bookmark|linkding|scrcpy|dockge|semaphore", "Infrastructure"),
)


@dataclass
class Edge:
    """The reverse-proxy container in front of this box, if there is one."""

    container_name: str
    kind: str
    image: str
    # "config" (read generated server blocks out of the proxy) or "labels"
    # (the routing table is on the proxied containers themselves).
    source: str = "config"
    config_path: str = ""
    admin_port: int | None = None


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
    # What discovery would have called this with no overrides at all. Carried so
    # the UI can offer "reset to automatic" and show it instantly, without
    # waiting for a scan to recompute it.
    derived_name: str = ""
    derived_icon: str = ""
    derived_category: str = ""
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
            "derived": {
                "name": self.derived_name,
                "icon": self.derived_icon,
                "category": self.derived_category,
            },
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
                    image=image,
                    source=kind.get("source", "config"),
                    config_path=kind.get("config_path", ""),
                    admin_port=kind.get("admin_port"),
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
# Label-driven proxies (Traefik, caddy-docker-proxy)
# --------------------------------------------------------------------------
#
# These have no generated config to read: a container declares its own public
# hostname in its labels and the proxy watches the daemon for changes. That is
# convenient here, because the labels arrived with the container list -- the
# routing table costs no extra API call and cannot go stale between scans.
#
# The upstream is emitted as the *container name and internal port*, which is
# what the proxy itself dials. ``_match_public`` already resolves that shape via
# the alias path, so nothing downstream needs to know which proxy was found.

_RE_TRAEFIK_ROUTER = re.compile(r"^traefik\.http\.routers\.([^.]+)\.(.+)$", re.I)
_RE_TRAEFIK_PORT = re.compile(
    r"^traefik\.http\.services\.([^.]+)\.loadbalancer\.server\.port$", re.I)
# Host(`a.com`) -- backticks are Traefik's own quoting, but single and double
# quotes appear in hand-written compose files often enough to accept both.
_RE_HOST_RULE = re.compile(r"Host(?:SNI)?\(([^)]*)\)", re.I)
_RE_QUOTED = re.compile(r"[`'\"]([^`'\"]+)[`'\"]")


def _sole_web_port(facts: ContainerFacts) -> int | None:
    """The container's port, when it has exactly one that could serve HTTP.

    Both Traefik and Caddy make the port optional and fall back to the single
    exposed one. Guessing beyond that is worse than declining: a wrong port
    produces a card that looks right and 502s.
    """
    usable = [p for p in facts.internal_ports if p not in NON_HTTP_PORTS]
    return usable[0] if len(usable) == 1 else None


def _host_rule_domains(rule: str) -> list[str]:
    """Domains named by a Traefik router rule.

    Only ``Host(...)`` matchers yield a linkable name. A rule may hold several
    (``Host(`a`) || Host(`b`)``, or ``Host(`a`, `b`)``), and may combine them
    with ``PathPrefix`` and friends, which are ignored -- the root URL is still
    the right thing to put on a card.
    """
    domains: list[str] = []
    for match in _RE_HOST_RULE.finditer(rule or ""):
        for domain in _RE_QUOTED.findall(match.group(1)):
            domain = domain.strip()
            # A regexp or wildcard matcher has no single address to link to.
            if domain and not domain.startswith("*") and "{" not in domain:
                domains.append(domain)
    return domains


def _traefik_https(spec: dict[str, str]) -> bool:
    """Whether a router serves TLS, from its own keys alone.

    ``tls=true`` and any ``tls.*`` sub-key both mean yes; so does an entrypoint
    named for the HTTPS side. Assuming HTTPS by default would be wrong -- plenty
    of tailnet-only routers are plain HTTP, and a bad scheme is a broken link.
    """
    for key, value in spec.items():
        key = key.lower()
        if key.startswith("tls."):
            return True
        if key == "tls" and str(value).strip().lower() not in {"false", "0", ""}:
            return True
        if key in {"entrypoints", "entrypoint"}:
            entries = str(value).lower()
            if any(token in entries for token in ("websecure", "https", "443")):
                return True
    return False


def parse_traefik_labels(containers: list[ContainerFacts]) -> list[ProxyHost]:
    """Read Traefik's dynamic configuration off the containers it routes to.

    A container opts out with ``traefik.enable=false``; the opposite default
    (``exposedByDefault=false`` in Traefik's static config) cannot be seen from
    here, but a container with no router labels produces no hosts anyway, so the
    result is the same.
    """
    hosts: list[ProxyHost] = []
    for facts in containers:
        labels = facts.labels or {}
        if str(labels.get("traefik.enable", "true")).strip().lower() in {"false", "0"}:
            continue

        ports: dict[str, int] = {}
        routers: dict[str, dict[str, str]] = defaultdict(dict)
        for key, value in labels.items():
            port_match = _RE_TRAEFIK_PORT.match(key)
            if port_match:
                try:
                    ports[port_match.group(1)] = int(str(value).strip())
                except ValueError:
                    log.debug("%s: unreadable traefik port %r", facts.name, value)
                continue
            router_match = _RE_TRAEFIK_ROUTER.match(key)
            if router_match:
                routers[router_match.group(1)][router_match.group(2)] = str(value)
        if not routers:
            continue

        # With one service declared, every router on the container uses it --
        # naming it explicitly is only needed when there are several.
        fallback = next(iter(ports.values())) if len(ports) == 1 else None
        for name, spec in routers.items():
            port = ports.get(spec.get("service", name), fallback)
            if port is None:
                port = _sole_web_port(facts)
            if port is None:
                log.debug("%s: traefik router %s has no resolvable port", facts.name, name)
                continue
            https = _traefik_https(spec)
            for domain in _host_rule_domains(spec.get("rule", "")):
                hosts.append(ProxyHost(domain, facts.name, port, https))
    return hosts


# caddy-docker-proxy keys a site block on `caddy`, or `caddy_0`, `caddy_1`, ...
# when one container serves several. Directives hang off that same prefix.
_RE_CADDY_SITE = re.compile(r"^caddy(?:_\d+)?$", re.I)
_RE_CADDY_UPSTREAM = re.compile(r"\{\{\s*upstreams(?:\s+[a-z]+)?\s+(\d+)\s*\}\}", re.I)


def parse_caddy_labels(containers: list[ContainerFacts]) -> list[ProxyHost]:
    """Read caddy-docker-proxy site blocks off the containers they serve.

    The site address carries the scheme: Caddy issues certificates
    automatically, so a bare domain is HTTPS and only an explicit ``http://``
    prefix (or a ``:80`` port) is not.
    """
    hosts: list[ProxyHost] = []
    for facts in containers:
        labels = facts.labels or {}
        for key, value in labels.items():
            if not _RE_CADDY_SITE.match(key):
                continue
            upstream = _RE_CADDY_UPSTREAM.search(str(labels.get(f"{key}.reverse_proxy", "")))
            port = int(upstream.group(1)) if upstream else _sole_web_port(facts)
            if port is None:
                log.debug("%s: caddy site %s has no resolvable port", facts.name, key)
                continue
            for raw in str(value).split(","):
                raw = raw.strip()
                if not raw:
                    continue
                https = not raw.lower().startswith("http://")
                domain = raw.split("://", 1)[-1].split("/")[0].strip()
                if domain and not domain.startswith("*"):
                    hosts.append(ProxyHost(domain, facts.name, port, https))
    return hosts


LABEL_PARSERS = {
    "traefik": parse_traefik_labels,
    "caddy": parse_caddy_labels,
}


def proxy_hosts_for(
    client: docker.DockerClient,
    edge: Edge | None,
    containers: list[ContainerFacts],
    config_dir: str = "",
) -> list[ProxyHost]:
    """The public hostname table, however this box's proxy happens to store it.

    ``config_dir`` is the manual escape hatch and wins when set. With no edge at
    all the answer is an empty list, which is correct: the page then shows LAN
    links only.
    """
    if config_dir:
        return parse_proxy_hosts(read_config_dir(config_dir))
    if edge is None:
        return []
    if edge.source == "labels":
        return LABEL_PARSERS[edge.kind](containers)
    return parse_proxy_hosts(fetch_config_texts(client, edge))


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
    # Metadata that makes a decent display name without anyone writing one:
    # the image author's title, and the name the user gave the service in their
    # compose file.
    oci_title: str = ""
    compose_service: str = ""
    compose_project: str = ""


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

    labels = config.get("Labels") or {}
    host_network = (attrs.get("HostConfig", {}) or {}).get("NetworkMode") == "host"

    # A host-network container publishes nothing through the port bindings, but
    # it is still bound on the host's interfaces -- so what the image declares as
    # exposed *is* its host port. This is how Jellyfin's 8096 is found without
    # anyone configuring it.
    if host_network and not host_ports:
        host_ports = set(internal_ports)

    return ContainerFacts(
        name=container.name,
        image=image,
        running=container.status == "running",
        host_ports=sorted(host_ports),
        internal_ports=sorted(internal_ports),
        aliases=aliases,
        labels=labels,
        host_network=host_network,
        oci_title=labels.get("org.opencontainers.image.title", "") or "",
        compose_service=labels.get("com.docker.compose.service", "") or "",
        compose_project=labels.get("com.docker.compose.project", "") or "",
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
    edge: Edge | None = None,
    self_name: str = "",
) -> list[App]:
    """Produce the list of cards to render.

    Visibility rule: a container is shown when it has something to click. That
    means a published port, an apps.yml entry giving it a port or url, or a
    proxy hostname pointing at one of its aliases. Anything else is plumbing.
    Images on the infra denylist are hidden unless apps.yml mentions them
    explicitly, which is what keeps databases and sidecars off the page even
    when they do publish a port.

    ``overrides`` is entirely optional: names, icons and categories all have a
    derived default. It exists to correct a guess, not to enumerate services.
    """
    apps_cfg: dict = overrides.get("apps") or {}
    defaults: dict = overrides.get("defaults") or {}
    hide_keys = {str(k) for k in (overrides.get("hide") or [])}
    npm_ports = {h.upstream_port for h in proxy_hosts if h.upstream_host in host_ips}

    apps: list[App] = []
    for facts in containers:
        # This container is the page you are reading; listing it is noise.
        if self_name and facts.name == self_name:
            continue
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
        # The reverse proxy publishes the ports it serves for *other* services,
        # so ranking by port alone would link its card at the proxy itself.
        if not preferred and edge and facts.name == edge.container_name and edge.admin_port:
            preferred = edge.admin_port
        candidates = list(facts.host_ports)
        if preferred and preferred not in candidates:
            candidates.append(int(preferred))
        candidates.sort(key=lambda p: _rank_port(p, npm_ports, preferred))
        chosen_port = candidates[0] if candidates else None

        public_urls = _match_public(facts, chosen_port, proxy_hosts, host_ips)
        if not chosen_port and not cfg.get("url") and not public_urls:
            continue

        derived_name = display_name(facts)
        derived_icon = _icon_slug(facts)
        derived_category = categorise(facts) or defaults.get("category", "Other")

        apps.append(
            App(
                key=facts.name,
                name=cfg.get("name") or derived_name,
                icon=cfg.get("icon") or derived_icon,
                category=cfg.get("category") or derived_category,
                derived_name=derived_name,
                derived_icon=derived_icon,
                derived_category=derived_category,
                image=facts.image,
                running=facts.running,
                lan_port=chosen_port,
                lan_url_override=cfg.get("url"),
                scheme=cfg.get("scheme", "http"),
                public_urls=public_urls,
            )
        )

    _disambiguate(apps, {a.key for a in apps if apps_cfg.get(a.key, {}).get("name")})
    apps.sort(key=lambda a: (a.category.lower(), a.name.lower()))
    return apps


def _disambiguate(apps: list[App], pinned: set[str]) -> None:
    """Make duplicate card names distinct, in place.

    Two containers of one image derive the same name -- two Decypharr instances,
    two qBittorrents -- which leaves the page with cards nobody can tell apart.
    The container names still differ, so the tokens unique to each become the
    qualifier: decypharr_local and decypharr_torbox give "Decypharr (Local)" and
    "Decypharr (TorBox)". A name set explicitly in apps.yml is never touched.
    """
    by_name: dict[str, list[App]] = defaultdict(list)
    for item in apps:
        by_name[item.name.lower()].append(item)

    for group in by_name.values():
        if len(group) < 2:
            continue
        token_sets = [set(re.split(r"[-_.\s]+", a.key.lower())) for a in group]
        shared = set.intersection(*token_sets) if token_sets else set()
        for item, tokens in zip(group, token_sets):
            if item.key in pinned:
                continue
            unique = [t for t in re.split(r"[-_.\s]+", item.key) if t.lower() in tokens - shared]
            if unique:
                item.name = f"{item.name} ({' '.join(_titleise(u) for u in unique)})"


def _titleise(value: str) -> str:
    """Make a slug-ish string presentable without destroying real capitalisation.

    A value that already carries capitals is the author's own styling and is kept
    verbatim ("Paperless-ngx", "qBittorrent"); an all-lowercase one is split on
    separators and capitalised.
    """
    if any(c.isupper() for c in value):
        return value
    return " ".join(part.capitalize() for part in re.split(r"[-_.\s]+", value) if part)


def _usable_title(title: str) -> str | None:
    """Whether an OCI title label is a product name or just an image reference.

    Plenty of images set the label to their own pull reference -- hotio's
    whisparr ships ``hotio/whisparr:v3`` -- which makes a poor card title. A "/"
    or ":" is the giveaway, and a digest is never a name.
    """
    title = (title or "").strip()
    if not title or title.startswith("sha256"):
        return None
    if "/" in title or ":" in title:
        return None
    return title


def display_name(facts: ContainerFacts) -> str:
    """The best available name for a service, without anyone writing one down.

    In order: the image author's own title label, then the image's repository
    name, then the compose service name, then the container name. Image name
    before compose service because a service is often named for its role in a
    stack rather than for the product ("wp", "scraper", "app"), while the image
    repository is almost always the product itself.

    The container name is last because compose decorates it with a project prefix
    and a scale suffix, which is exactly the noise this is trying to avoid.
    """
    if _usable_title(facts.oci_title):
        return _titleise(facts.oci_title)

    image_name = _image_basename(facts.image)
    if image_name and image_name not in {"latest", "app", "server", "main"}:
        return _titleise(image_name)

    if facts.compose_service:
        return _titleise(facts.compose_service)

    cleaned = re.sub(r"-\d+$", "", facts.name)
    if facts.compose_project and cleaned.startswith(facts.compose_project):
        cleaned = cleaned[len(facts.compose_project):].strip("-_") or cleaned
    return _titleise(cleaned)


def categorise(facts: ContainerFacts) -> str:
    """Bucket a service using the built-in rules; "Other" when nothing matches."""
    haystack = " ".join(
        (facts.name, facts.compose_service, facts.image, facts.oci_title)
    ).lower()
    for pattern, category in CATEGORY_RULES:
        if re.search(pattern, haystack):
            return category
    return "Other"


def _image_basename(image: str) -> str:
    """"lscr.io/linuxserver/radarr:latest" -> "radarr"."""
    ref = image.split("@")[0]
    if "/" in ref.rsplit(":", 1)[-1]:  # a port in the registry host, not a tag
        pass
    else:
        ref = ref.rsplit(":", 1)[0]
    return ref.rsplit("/", 1)[-1]


def _icon_slug(facts: ContainerFacts) -> str:
    """Icon slug for the icon set, e.g. lscr.io/linuxserver/radarr:latest -> radarr.

    A slug the icon set does not have is not a failure: the server then asks the
    service for its own favicon, so this only has to be right often enough to
    prefer the nicer artwork.
    """
    slug = _image_basename(facts.image) or facts.compose_service or facts.name
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
