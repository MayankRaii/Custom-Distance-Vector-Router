RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; RESET='\033[0m'

step() { echo -e "${CYAN}[STEP]${RESET} $1"; }
ok()   { echo -e "${GREEN}[ OK ]${RESET} $1"; }
info() { echo -e "${YELLOW}[INFO]${RESET} $1"; }
fail() { echo -e "${RED}[FAIL]${RESET} $1"; }

show_table() {
    echo ""
    info "Routing table on $1"
    docker exec "$1" ip route
    echo ""
}

show_log() {
    info "Last ${2:-30} log lines from $1"
    docker logs --tail "${2:-30}" "$1"
}

#  Build & Start
step "Building Docker image..."
docker compose build || { fail "Build failed"; exit 1; }
ok "Image built"

step "Starting all routers..."
docker compose up -d || { fail "Startup failed"; exit 1; }
ok "Containers started"

# Wait for convergence
step "Waiting 20 s for routing table convergence..."
sleep 20

info "=== CONVERGED ROUTING TABLES ==="
show_table router_a
show_table router_b
show_table router_c

# Connectivity (ping)
step "TEST 1: Router A → Router C (direct: net_ac)"
docker exec router_a ping -c 3 10.0.3.2 && ok "PASS: A→C" || fail "FAIL: A→C"

step "TEST 2: Router A → Router B (direct: net_ab)"
docker exec router_a ping -c 3 10.0.1.2 && ok "PASS: A→B" || fail "FAIL: A→B"

step "TEST 3: Router B → Router C (direct: net_bc)"
docker exec router_b ping -c 3 10.0.2.2 && ok "PASS: B→C" || fail "FAIL: B→C"

# Failure / Reconvergence

step "TEST 4: Stopping Router C to simulate link failure..."
docker stop router_c
info "Router C stopped. Waiting 20 s for reconvergence..."
sleep 20

info "=== POST-FAILURE ROUTING TABLES ==="
show_table router_a
show_table router_b

step "TEST 5: Router A should reach 10.0.2.0/24 via Router B"
ROUTE_A=$(docker exec router_a ip route show 10.0.2.0/24)
[ -n "$ROUTE_A" ] && ok "PASS: route = $ROUTE_A" || fail "FAIL: no route to 10.0.2.0/24"

step "TEST 6: Router A should reach 10.0.3.0/24 via alternate path"
ROUTE_A2=$(docker exec router_a ip route show 10.0.3.0/24)
[ -n "$ROUTE_A2" ] && ok "PASS: route = $ROUTE_A2" || fail "FAIL: no route to 10.0.3.0/24"

# Log evidence for report
step "Collecting log evidence for report..."
show_log router_a 50
show_log router_b 50

# CLEAN UP
step "Tearing down..."
docker compose down
ok "All done. Review output above for your report."
