#!/usr/bin/env bash
# failure-tests.sh — NODE FAIL vs LINK FAIL (Linux / WSL / Git Bash)
# Prerequisites: docker compose up -d from this folder

set -e
CYAN='\033[0;36m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; RESET='\033[0m'
say()  { echo -e "${CYAN}[TEST]${RESET} $1"; }
info() { echo -e "${YELLOW}[INFO]${RESET} $1"; }
ok()   { echo -e "${GREEN}[ OK ]${RESET} $1"; }

net_ac="$(docker network ls --format '{{.Name}}' | grep -E 'net_ac$' | head -1)"
if [[ -z "$net_ac" ]]; then
  echo "No *net_ac network found. Run: docker compose up -d"
  exit 1
fi
info "Using network for link-fail: $net_ac"

routes() { info "ip route on $1"; docker exec "$1" ip route 2>/dev/null || true; }

say "Baseline"
routes router_a
routes router_b

say "NODE FAIL: docker stop router_c"
docker stop router_c
info "Waiting 15s..."
sleep 15
routes router_a
routes router_b
info "NOTE: Stopping only C may still leave 10.0.3.0/24 as 'direct' on A if eth1 (net_ac) exists."

say "Restore C"
docker start router_c
sleep 18
routes router_a

say "LINK FAIL: disconnect router_a from $net_ac"
docker network disconnect "$net_ac" router_a || true
info "Waiting 18s..."
sleep 18
routes router_a
routes router_b

say "Restore link: connect router_a to $net_ac with 10.0.3.1"
docker network connect "$net_ac" router_a --ip 10.0.3.1 || true
sleep 12
routes router_a
ok "Done."
