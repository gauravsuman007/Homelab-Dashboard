# Homelab Dashboard

[![build](https://github.com/gauravsuman007/Homelab-Dashboard/actions/workflows/build.yml/badge.svg)](https://github.com/gauravsuman007/Homelab-Dashboard/actions/workflows/build.yml)

A single page at **http://\<your-server\>/** listing every service running on the
box, with a link to each web UI and, where the reverse proxy in front of it has a
hostname pointing at that service, a link to the public URL too.

Nothing is hard-coded and nothing needs filling in: the container is portable as
written. The Docker socket is the only required input.

```bash
docker compose up -d     # pull and start
./nginx/apply.sh         # make it the page served at the bare IP
```

Prebuilt multi-arch images are published on every push to
`ghcr.io/gauravsuman007/homelab-dashboard` for **linux/amd64,
linux/386, linux/arm/v7 and linux/arm64** — the same compose file works on a
server, a NAS, or a Raspberry Pi. Update with
`docker compose pull && docker compose up -d`. To build your own instead,
uncomment `build: .` in `docker-compose.yml` and run `up -d --build`.

## Auto-detection

| Question | Answered by | Configuration needed |
| --- | --- | --- |
| What's running, on which ports? | Docker Engine API | none |
| Which reverse proxy is in front? | container image match | none |
| Where is its config? | Docker API archive endpoint (`docker cp`) | none |
| Which addresses mean "this box"? | gateways + config inference + Host headers | none |
| What URL do I link to? | the Host header of each request | none |
| Pretty names, icons, categories | `config/apps.yml` | optional |

The two that are worth explaining:

**The proxy's config is read through the Docker API**, not a bind mount. The edge
container is found by image, then its generated server blocks are pulled with the
same endpoint `docker cp` uses. So there is no host path to know, and it works
even when the config lives in a named volume. If your socket is behind a proxy
that filters that endpoint, set `EDGE_CONFIG_DIR` and mount the directory
instead.

**LAN URLs come from the Host header** of the request being served, so links
always point back the way you arrived — browse by LAN IP and you get LAN links,
over Tailscale you get tailnet links, by hostname you get hostname links. No
address is stored anywhere.

Knowing *which* addresses are the box is a separate problem, because a
bridge-networked container cannot read the host's interfaces. Three sources
combine:

- **docker network gateways** — host addresses by definition;
- **the proxy config itself** — an upstream IP is taken as local when **two or
  more distinct** ports at that IP are also published here. Two is what
  separates a real local address from a coincidence. On the reference box
  `192.168.2.100` matches on a dozen ports and is accepted, while a NAS at `.39`
  and an LLM host at `.123` match on at most one and are correctly rejected —
  verified, their domains are not attached to any local container;
- **addresses visitors arrive on** — filtered to private and tailnet IPs, kept in
  memory only.

`HOST_IPS` exists as an override for the one case inference can't settle: a box
publishing a *single* service, where the two-port rule can't reach certainty
before anyone loads the page.

## How discovery decides what to show

A card appears when there's something to click: a published port, an `apps.yml`
port/url, or a proxy hostname aimed at one of the container's network aliases.
Databases, sidecars and VPN containers are filtered by an image denylist in
`app/discovery.py`; naming one in `apps.yml` overrides that.

Public hostnames match a container two ways — by host address + published port
(the usual case), or by container alias + *internal* port, which is how a
container that publishes nothing can still be public (`megang.mera.place →
megabasterd-ng:5800`), and how a host-network container like Jellyfin gets its
domain.

Status dots are a **TCP connect** to the host gateway, not an HTTP request:
several services answer 401/302 at `/`, so a status-code rule would show healthy
services as down.

Icons resolve in three tiers, all served from `/icon/<slug>` and cached in
`./cache/`:

1. **[dashboard-icons][icons]** — preferred, because they're consistent, square
   and already sized for tiles.
2. **the service's own favicon** — for anything the icon set doesn't cover. The
   server fetches the app's page, reads its `<link rel="icon">` tags (preferring
   `apple-touch-icon`, the largest thing most apps ship) and falls back to
   `/favicon.ico`, which plenty of services serve without declaring it — and
   often without requiring a login. Disable with `FAVICON_FALLBACK=false`.
3. **a letter tile** — the service's initial, when neither source has anything.

Fetching happens server-side, so the browser still makes **no third-party
requests** and the page works with the LAN alone. It also means favicons resolve
for services the visitor's own browser couldn't reach.

A "nothing found" result is cached for `ICON_MISS_TTL_HOURS` (24 by default)
rather than forever, so an icon isn't lost permanently just because the service
happened to be down or still starting when it was first looked up. Delete the
`.miss` file in `cache/icons/` to retry immediately, or drop your own
`cache/icons/<slug>.png` in to override any of this.

TLS verification is off for the favicon probe: these are LAN services,
overwhelmingly self-signed, and the payload is a decorative image whose bytes are
validated as an image (by content type or magic number) and capped at 512 KB
before use.

[icons]: https://github.com/homarr-labs/dashboard-icons

## Reverse-proxy support

**Nginx Proxy Manager** is supported: its generated blocks use a `set $server` /
`set $port` idiom that's stable across versions and installs.

**Plain nginx, Traefik, Caddy** are *not* parsed yet. Behaviour is graceful, not
broken: you get the full container list with links and status, just no public-URL
badges. Adding a mode means a parser that follows `include` chains and resolves
`upstream` blocks (nginx) or reads router labels (Traefik) — the config-fetching
half already works for any container, since it goes through the Docker API.

To add one, extend `EDGE_KINDS` in `app/discovery.py` with an image marker and
config path, and give `parse_proxy_hosts` a branch for that syntax.

## The nginx snippet

`./nginx/apply.sh` detects the host's address, its LAN subnet (from the
interface's real prefix, not an assumed /24), the tailnet address, the hostname,
and the proxy container — then renders `nginx/lan-landing.conf.template` and
installs it. `--show` prints what it would do and changes nothing; `--remove`
uninstalls.

It's idempotent and safe: it backs up the proxy's `custom/http.conf`, replaces
only its own `# BEGIN/END lan-landing` block, runs `nginx -t`, and restores the
backup without reloading if the test fails. Anything else in that file is
preserved byte for byte. It keeps the five most recent backups.

Why a snippet rather than the NPM GUI: NPM's proxy hosts are keyed on domain
names, and its "Default Site" setting offers only a redirect, a 404, or static
HTML — none of which can point a bare IP at a container. Why `server_name` rather
than `default_server`: an exact name match already beats the implicit default, so
NPM's fallback block keeps answering Let's Encrypt HTTP-01 challenges and junk
traffic to the public IP.

## Access and security

The page is **unauthenticated** and reveals the internal port map of the whole
server, so it must not be published. Two things keep it internal:

- No proxy host points a public domain at it.
- The nginx block matches only the box's own names, and its `location /` allows
  the detected LAN subnet, the Tailscale range (only if a tailnet exists), and
  loopback. This matters because if 80/443 are forwarded at the router, a forged
  Host header *does* reach this vhost, and the ACL is what stops it.

The docker bridge ranges are deliberately **not** allowed. NPM publishes port 80
over IPv6 through docker's userland proxy (ip6tables NAT is off by default),
which rewrites the peer address: a request to the box's routable IPv6 address
arrives at nginx as the bridge gateway, on the IPv4 socket — indistinguishable
from a container on that bridge. Allowing `172.16.0.0/12` would therefore expose
the page to the whole IPv6 internet. The cost is that `curl` from another
container gets 403; nothing needs that. (IPv4 is DNAT'd and preserves the real
source, so the LAN rule works as written.)

**Nothing here disables or degrades IPv6.** No host sysctl, no daemon config, and
no other vhost is touched — this block simply doesn't add an IPv6 listener of its
own. Container IPv6 to smart devices is unaffected.

**On the socket mount:** `:ro` protects the socket *file*, not the API — it does
not restrict which Docker API calls are possible. What limits this container is
that its code only lists, inspects, and reads archives; it never starts, stops or
execs anything. Anyone who can reach a docker socket effectively has root on the
host, which is the usual reason to keep this page internal.

Verified behaviour on the reference box:

| From | Result |
| --- | --- |
| LAN IPv4 | 200, landing page |
| Tailscale address | 200, landing page |
| Hostname resolving to IPv4 | 200, landing page |
| Hostname resolving to IPv6 | 403 — see below |
| Public IPv6 + forged `Host` | 403 |
| Another container on a docker bridge | 403 |
| Existing proxied domains | unchanged |
| Unknown hostname | unchanged, proxy's default site |

Note on the hostname: on the reference box `debian-parallels` resolves to IPv6
addresses only, so browsing it by name takes the IPv6 path and gets 403 like any
other IPv6 request. Browse the v4 address, or add an A record / `hosts` entry if
you want the name to work.

## Configuration

All optional, set in `docker-compose.yml`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SITE_TITLE` | `Home Server` | page heading |
| `REFRESH_SECONDS` | `30` | discovery + probe interval |
| `PROBE_TIMEOUT` | `2` | seconds per TCP reachability probe |
| `INCLUDE_STOPPED` | `false` | also list stopped containers, greyed out |
| `HOST_IPS` | inferred | override for "which addresses are this box" |
| `EDGE_CONFIG_DIR` | unset | read proxy config from a mount instead of the API |
| `FAVICON_FALLBACK` | `true` | ask services for their own favicon when the icon set misses |
| `ICON_MISS_TTL_HOURS` | `24` | how long a "no icon found" result is remembered |

`apply.sh` honours `HOST_IP`, `APP_PORT`, `NPM_CONTAINER`, `EXTRA_ALLOW`.

## Adding or relabelling a service

Edit `config/apps.yml` — re-read on every scan, no restart. See the comments at
the top of that file. The one case that *requires* an entry is a container on the
host network, which publishes nothing the Docker API can report: give it `port:`.

## CI

`.github/workflows/build.yml` runs on every push. A fast `check` job compiles the
sources and exercises the pure discovery functions — NPM config parsing, the
two-port host-IP inference, and the address filter — on a runner with **no Docker
socket at all**, which is what keeps that logic testable. Only if it passes does
the `build` job produce the four-platform manifest.

Publishing goes to **GHCR** using the built-in `GITHUB_TOKEN`, so a fresh clone or
fork needs no secrets. **Docker Hub** is published too, but only when
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are set — without them the run stays
green and notes that it published to GHCR only. Pull requests build all four
platforms and publish nothing.

Only amd64 builds natively; the other three go through QEMU. That is why the
Dockerfile is two-stage: PyYAML has no wheel for 386 or arm/v7, so it compiles
from source in a builder stage that carries gcc, and the runtime image copies only
the finished virtualenv.

## Files

```
.github/workflows/build.yml     checks, then the 4-platform image build
docker-compose.yml              service definition (portable as written)
Dockerfile                      python:3.12-slim + flask/gunicorn/docker/pyyaml
config/apps.yml                 display overrides (live-reloaded)
cache/                          downloaded icons (created on first run)
app/server.py                   HTTP surface, refresh loop, icon cache
app/discovery.py                docker + proxy parsing, host-IP inference
app/templates/index.html
app/static/{style.css,app.js,favicon.svg}
nginx/lan-landing.conf.template source for the server block
nginx/apply.sh                  detects, renders, installs, nginx -t, rollback
nginx/lan-landing.generated.conf  last rendered output (generated)
```
