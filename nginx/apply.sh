#!/usr/bin/env bash
# Install (or refresh) the lan-landing server block in the reverse proxy.
#
# Self-configuring: the host's primary address, its LAN subnet, its tailnet
# address and hostname, and the reverse-proxy container are all detected here.
# This script runs on the host, where that information is directly readable --
# which is why detection lives at apply time rather than inside the container.
#
# Idempotent: any previously installed block is removed by its BEGIN/END markers
# before the freshly rendered one is appended, so running this twice leaves one
# copy. Anything else in http.conf (hand-managed rate limiting, for instance) is
# preserved byte for byte.
#
# Everything touching the proxy goes through `docker exec`, because its /data
# tree is root-owned on the host while the container already runs as root -- no
# sudo needed. `nginx -t` must pass before the reload; on failure the original
# file is restored and nothing is reloaded, so a bad edit cannot take live proxy
# hosts offline.
#
# Usage:
#   ./nginx/apply.sh              detect, render, install, reload
#   ./nginx/apply.sh --show       print what would be installed, change nothing
#   ./nginx/apply.sh --remove     uninstall the block and reload
#
# Overrides, all optional:
#   NPM_CONTAINER   reverse-proxy container name (default: found by image)
#   HOST_IP         address nginx should proxy to (default: default-route src)
#   APP_PORT        port the landing page listens on (default: 8500)
#   EXTRA_ALLOW     space-separated extra CIDRs to allow
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/lan-landing.conf.template"
RENDERED="$HERE/lan-landing.generated.conf"
TARGET=/data/nginx/custom/http.conf
BEGIN='# BEGIN lan-landing'
END='# END lan-landing'
APP_PORT="${APP_PORT:-8500}"

mode="${1:-install}"

# --- detection -------------------------------------------------------------

# The address the host uses to reach the world: also the address LAN clients use
# to reach the host, and the one nginx should proxy to.
detect_host_ip() {
  ip route get 1.1.1.1 2>/dev/null \
    | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}'
}

# The subnet that address sits on, taken from the interface's own prefix rather
# than assumed to be a /24.
detect_lan_cidr() {
  local ip="$1"
  ip -o -f inet addr show 2>/dev/null | awk -v want="$ip" '
    { split($4, a, "/"); if (a[1] == want) { print $4; exit } }' \
  | python3 -c 'import sys,ipaddress
raw = sys.stdin.read().strip()
print(ipaddress.ip_interface(raw).network if raw else "")'
}

detect_tailscale_ip() {
  ip -o -4 addr show tailscale0 2>/dev/null \
    | awk '{ split($4, a, "/"); print a[1]; exit }'
}

detect_edge_container() {
  docker ps --format '{{.Names}}\t{{.Image}}' \
    | awk -F'\t' 'tolower($2) ~ /nginx-proxy-manager/ { print $1; exit }'
}

HOST_IP="${HOST_IP:-$(detect_host_ip)}"
if [[ -z "$HOST_IP" ]]; then
  echo "error: could not detect this host's address; set HOST_IP=..." >&2
  exit 1
fi
LAN_CIDR="$(detect_lan_cidr "$HOST_IP")"
TS_IP="$(detect_tailscale_ip)"
CONTAINER="${NPM_CONTAINER:-$(detect_edge_container)}"

# server_name: every name that means this box. Duplicates are harmless in nginx
# but are dropped for readability.
names=("$HOST_IP" "$(hostname)" "$(hostname).local")
[[ -n "$TS_IP" ]] && names+=("$TS_IP")
SERVER_NAMES="$(printf '%s\n' "${names[@]}" | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ $//')"

# allow rules, one per line, indented to match the template's location block.
allows=()
[[ -n "$LAN_CIDR" ]] && allows+=("$LAN_CIDR|the LAN")
# 100.64.0.0/10 is Tailscale's CGNAT range; added whole rather than as a single
# address so other tailnet peers work too, and only when a tailnet exists here.
[[ -n "$TS_IP" ]] && allows+=("100.64.0.0/10|Tailscale (CGNAT range)")
# The whole loopback range, not just 127.0.0.1: Debian resolves the machine's own
# hostname to 127.0.1.1, so a curl to http://$(hostname)/ from the box arrives
# from that address.
allows+=("127.0.0.0/8|this box itself")
for extra in ${EXTRA_ALLOW:-}; do allows+=("$extra|from EXTRA_ALLOW"); done

ALLOW_RULES=""
for entry in "${allows[@]}"; do
  cidr="${entry%%|*}"; note="${entry##*|}"
  ALLOW_RULES+="$(printf '        allow %-18s # %s' "$cidr;" "$note")"$'\n'
done
ALLOW_RULES="${ALLOW_RULES%$'\n'}"

# --- render ----------------------------------------------------------------

python3 - "$TEMPLATE" "$RENDERED" <<PY
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
for key, value in {
    "@HOST_IP@": """$HOST_IP""",
    "@APP_PORT@": """$APP_PORT""",
    "@SERVER_NAMES@": """$SERVER_NAMES""",
    "@ALLOW_RULES@": """$ALLOW_RULES""",
}.items():
    text = text.replace(key, value)
open(dst, "w").write(text)
PY

cat <<EOF
detected:
  host address     $HOST_IP
  LAN subnet       ${LAN_CIDR:-(none -- LAN will not be allowed!)}
  tailnet address  ${TS_IP:-(none)}
  server_name      $SERVER_NAMES
  proxy target     http://$HOST_IP:$APP_PORT
  reverse proxy    ${CONTAINER:-(none found)}
  rendered         $RENDERED
EOF

if [[ "$mode" == "--show" ]]; then
  echo; sed -n "/$BEGIN/,/$END/p" "$RENDERED"
  exit 0
fi

if [[ -z "$CONTAINER" ]]; then
  echo "error: no nginx-proxy-manager container found; set NPM_CONTAINER=..." >&2
  exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "error: container '$CONTAINER' is not running" >&2
  exit 1
fi

# --- install ---------------------------------------------------------------

exec_npm() { docker exec -i "$CONTAINER" sh -c "$1"; }

stamp=$(date +%Y%m%d-%H%M%S)
echo "backing up $TARGET -> $TARGET.bak-$stamp"
exec_npm "cp $TARGET $TARGET.bak-$stamp 2>/dev/null || touch $TARGET"

# Drop any existing block. sed's range delete is safe when the markers are
# absent -- it simply matches nothing.
exec_npm "sed -i '/$BEGIN/,/$END/d' $TARGET"

if [[ "$mode" == "--remove" ]]; then
  echo "removed lan-landing block"
else
  echo "appending lan-landing block"
  docker exec -i "$CONTAINER" sh -c "cat >> $TARGET" < "$RENDERED"
fi

echo "testing nginx config"
if ! docker exec "$CONTAINER" nginx -t; then
  echo "nginx -t FAILED -- restoring backup, not reloading" >&2
  exec_npm "cp $TARGET.bak-$stamp $TARGET"
  exit 1
fi

echo "reloading nginx"
docker exec "$CONTAINER" nginx -s reload

# Keep the five most recent backups. They are the rollback path for a bad edit,
# but one per run would otherwise pile up.
exec_npm "ls -1t $TARGET.bak-* 2>/dev/null | tail -n +6 | xargs -r rm -f"

if [[ "$mode" != "--remove" ]]; then
  echo "done: http://$HOST_IP/ now serves the landing page"
fi
