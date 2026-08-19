# Launch copy

Drafts for sharing the project, plus where to post and in what order. Written to
be pasted with light edits — the numbers and claims in them are all things the
README backs up, so nothing here needs softening later.

The single positioning sentence everything else hangs off:

> Every other self-hosted dashboard asks you to tell it what you are running.
> This one reads it off the Docker socket and figures it out.

---

## Where to post, in order

Post in this sequence. Each stop is a bigger, less forgiving audience than the
last, and the feedback from the small ones is what makes the big one survive.

| # | Where | Why here | Notes |
| --- | --- | --- | --- |
| 1 | **r/selfhosted** (~600k) | the exact audience; dashboards do well there | Best on a weekday morning US time. Read the rules — they want the GitHub link in the post, not just a screenshot. Lead with the GIF. |
| 2 | **r/homelab** (~1M) | overlaps heavily, skews toward hardware | Post a few days later, not the same day. Frame it toward "I stopped maintaining a service list", not "look at my CSS". |
| 3 | **r/docker** (~250k) | the *mechanism* is the story here | The `get_archive()` trick and label reading are the hook. This crowd will actually care that `/proc` isn't namespaced. |
| 4 | **Hacker News** — Show HN | biggest reach, harshest audience | Go here **last**, once the first three have shaken out the obvious bugs. Weekday, ~8–10am ET. One technical hook, no marketing voice. |
| 5 | **r/HomeAssistant** (~200k) | narrower, genuinely useful angle | Only post the *iframe card* angle here, not the whole project. Anything else reads as off-topic. |
| 6 | **awesome-selfhosted** | long-tail discovery | A PR, not a post. Needs the project to be a few weeks old with some activity. |
| 7 | **r/Traefik**, **r/nginxproxymanager** | small but perfectly matched | Post only after the proxy support has survived a few strangers' setups. |
| 8 | **Lobsters** (if you have an invite) | thoughtful, low-volume | Tag `devops`, `web`. Same copy as HN works. |

Two things worth doing **before** any of it: make sure the repo has a
description and topics set (`docker`, `homelab`, `self-hosted`, `dashboard`,
`traefik`, `nginx-proxy-manager`), and be ready to answer within the first hour.
The first three comments decide whether a thread lives.

---

## r/selfhosted

**Title:** I got tired of maintaining a list of my own services, so I built a dashboard that just reads the Docker socket

**Body:**

Every dashboard I tried — Homepage, Homarr, Dashy, Homer — wanted me to tell it
what I'm running. Either a `homepage.*` label on every container, or a YAML file
listing each service, or dragging every tile into place by hand. It's not much
work per service, but it's work *forever*: every new container is a second job,
and mine drifted out of date within a month.

So I built one that derives everything from the daemon:

- **Names** come from the OCI title label, falling back to the image name, then
  the compose service. Two containers of the same image get disambiguated
  automatically (my two Decypharr instances became "Decypharr (Local)" and
  "Decypharr (TorBox)" without me typing anything).
- **Icons** come from dashboard-icons, and when there's no match it fetches the
  service's *own favicon* and caches it. Only then does it fall back to a
  generated letter tile.
- **Categories** come from a built-in table of common self-hosted apps.
- **Public URLs** come from the reverse proxy. For Nginx Proxy Manager it reads
  the generated server blocks **through the Docker API's archive endpoint** —
  the same thing `docker cp` uses — so there's no bind mount and no host path to
  configure, and it works when the config is in a named volume. For Traefik and
  caddy-docker-proxy it reads the routing labels off the containers themselves,
  via the socket. No compose files are parsed and the proxy's API is never
  queried, so it works even when the Traefik dashboard is disabled.
- **LAN links** are built from the `Host` header of each request, so browsing by
  LAN IP gives LAN links and browsing over Tailscale gives tailnet links. No
  address is stored anywhere and there's nothing to configure when it changes.
- **Host CPU/RAM/disk/uptime**, with no extra mounts — Docker doesn't namespace
  `/proc/stat`, `/proc/meminfo` or `/proc/uptime`, so a plain container reads
  the host's real numbers through its own `/proc`.

The whole config is a socket mount:

```yaml
services:
  dashboard:
    image: ghcr.io/gauravsuman007/homelab-dashboard:latest
    ports: ['8500:8500']
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

Where the guess is wrong, you fix it *in the page* — rename a service, change
the icon, drag it into another category, make new categories. That's stored and
permanent, and everything you didn't touch keeps deriving itself. So there's no
config file that goes stale, because there's no config file.

On my box it found 22 services with an empty config directory. Building the
auto-derivation actually turned up two mistakes in the hand-written list I'd
been maintaining, which is what convinced me the derived version was the better
one.

Multi-arch (amd64/386/armv7/arm64), MIT, no telemetry, no account, no cloud
anything. It's LAN-first: it ships an nginx snippet that restricts access to
your LAN plus your tailnet.

**It is not a Homepage replacement** — there are no per-service API widgets, no
Sonarr queue counts. If you want those, Homepage and Homarr are genuinely better
and I'd point you at them. This is for the case where you just want the list of
what's running to be correct without maintaining it.

[GitHub link] — happy to answer anything, and I'd like to know which reverse
proxies people want next.

---

## Hacker News (Show HN)

**Title:** Show HN: A homelab dashboard that configures itself from the Docker socket

**Body:**

Self-hosted dashboards all have the same shape: you keep a list of your services
in YAML, or you put a label on every container, or you place every tile by hand.
The list then drifts out of date, because maintaining it is a chore with no
deadline.

This one keeps no list. It derives the page from the Docker daemon on every
scan, so the page is a *view* of the machine rather than a document about it.

Three implementation details that might be interesting:

1. **Reading the reverse proxy's config without a bind mount.** Nginx Proxy
   Manager writes generated server blocks inside its own container. Rather than
   asking the user to mount that path, it pulls them out through the Docker
   API's archive endpoint — what `docker cp` is built on. There's no host path
   to know, and it works when the config lives in a named volume. Traefik and
   caddy-docker-proxy don't need even that: their routing table is labels on the
   proxied containers, which already arrived with the container list, so it
   costs no extra API call and can't go stale between scans.

2. **Working out which IPs mean "this box"** — needed to tell "this proxy host
   points at a local service" from "it points at the NAS". A bridge-networked
   container can't read the host's interfaces. It combines the Docker network
   gateways, the addresses visitors actually arrive on (from the `Host` header),
   and a heuristic over the proxy config: an address is this host if **two or
   more** proxy entries point at it on ports this box publishes. One match is a
   coincidence; two is an identity.

3. **Host stats with no extra mounts.** Docker doesn't namespace `/proc/stat`,
   `/proc/meminfo` or `/proc/uptime`. An unprivileged container reads the host's
   real numbers straight out of its own `/proc`; CPU count and total memory come
   from `docker info`, which stays correct even where `/proc` is virtualised by
   lxcfs.

Service URLs are never stored. They're assembled per request from the `Host`
header, so the same page yields LAN links over LAN and tailnet links over
Tailscale, with nothing configured and nothing to update when an address
changes.

Derivation gets things wrong sometimes, so the page is editable — rename, re-icon,
drag between categories — and those edits are the only thing persisted. Notably,
building the derivation surfaced two errors in the hand-maintained list I'd been
using: one image shipped its OCI title as a pull reference (`hotio/whisparr:v3`)
rather than a product name, and two containers of one image collided into
identical cards.

Python/Flask, ~1,500 lines, MIT, multi-arch images. The one input is
`/var/run/docker.sock`.

[GitHub link]

---

## r/HomeAssistant (iframe angle only)

**Title:** Lean way to get a live view of all your Docker services as a dashboard card

**Body:**

I wanted my running containers as a Lovelace card without maintaining a second
copy of the list. Rather than write a custom card, I made the page itself
embeddable:

```yaml
type: iframe
url: http://<server>/?embed=1&compact=1
aspect_ratio: 75%
```

`embed=1` strips the header and footer, `compact=1` tightens the tiles, and
`theme=dark|light` pins the palette (it follows the device otherwise). You can
also pass `category=Media,Downloads` to get several small focused cards instead
of one big one.

Because the card *is* the live page rather than a synced copy, renames, icon
changes and category moves show up with no sync code at all. The page itself
gets its service list from the Docker socket, so new containers appear on their
own.

Two caveats worth knowing: an `http://` iframe inside an HTTPS Home Assistant
gets blocked by the browser as mixed content, and the iframe is fetched by your
*browser*, not by HA — so the viewing device has to be able to reach the
dashboard (fine on LAN or tailnet, empty card over Nabu Casa remote access).

[GitHub link]

---

## Answers to have ready

These will come up. Better to have the honest version written down now than to
improvise it defensively at hour two.

**"Why not just use Homepage?"** — If you want service widgets, do. Homepage's
discovery needs a `homepage.*` label per container; this needs nothing. Those
are different products for different itches, and I say so in the README.

**"Mounting the Docker socket is a security risk."** — Correct, and worth being
straight about: socket access is root-equivalent on the host, and `:ro` protects
the socket file, not the API. It only ever reads (list, inspect, archive), never
writes. If that's not acceptable, put a socket proxy in front of it and restrict
it to `GET`. The README says this rather than hiding it.

**"Does it work with Traefik/Caddy/Pangolin/plain nginx?"** — NPM, Traefik and
caddy-docker-proxy today. Traefik file-provider routes, hand-written Caddyfiles
and plain nginx aren't parsed. Adding a proxy is a table entry plus a parser, and
I'd rather add the one people actually want than guess.

**"Isn't this just X?"** — The closest things I found are two near-unknown
projects: one reads NPM's API but needs a service account plus hand-written tag
comments per proxy host, and another port-scans 1–65535 and screenshots each
service with Playwright. Neither derives identity from container metadata. Every
popular dashboard requires a manual list.

**"Does it phone home / need an account?"** — No, and no. It fetches icons from
the dashboard-icons CDN and can fetch a service's favicon from your own LAN;
both can be turned off.

**"Why Python and not Go?"** — A single binary would be nicer to ship. The
discovery logic is the interesting part and it's easier to read this way; the
image is multi-arch either way. Not religious about it.
