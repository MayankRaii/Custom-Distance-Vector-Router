$RED    = "`e[31m"
$GREEN  = "`e[32m"
$YELLOW = "`e[33m"
$CYAN   = "`e[36m"
$RESET  = "`e[0m"

function Log-Step { param([string]$msg) Write-Host "${CYAN}[STEP]${RESET} $msg" }
function Log-Ok   { param([string]$msg) Write-Host "${GREEN}[ OK ]${RESET} $msg" }
function Log-Info { param([string]$msg) Write-Host "${YELLOW}[INFO]${RESET} $msg" }
function Log-Fail { param([string]$msg) Write-Host "${RED}[FAIL]${RESET} $msg" }

# print routing table for a container
function Show-Table {
    param([string]$container)
    Write-Host ""
    Log-Info "Routing table on $container"
    docker exec $container ip route
    Write-Host ""
}

# show last N lines of router log 
function Show-Log {
    param([string]$container, [int]$lines = 30)
    Log-Info "Last $lines log lines from $container"
    docker logs --tail $lines $container
}

# Build & Start
Log-Step "Building Docker image..."
docker compose build
if ($LASTEXITCODE -ne 0) { Log-Fail "Build failed"; exit 1 }
Log-Ok "Image built"

Log-Step "Starting all routers..."
docker compose up -d
if ($LASTEXITCODE -ne 0) { Log-Fail "Startup failed"; exit 1 }
Log-Ok "Containers started"

# Wait for convergence
Log-Step "Waiting 20 s for routing table convergence..."
Start-Sleep -Seconds 20

Log-Info "=== CONVERGED ROUTING TABLES ==="
Show-Table "router_a"
Show-Table "router_b"
Show-Table "router_c"

#  Connectivity test (ping across subnets)
Log-Step "TEST 1: Router A  →  Router C (direct: net_ac)"
docker exec router_a ping -c 3 10.0.3.2
if ($LASTEXITCODE -eq 0) { Log-Ok "PASS: A can reach C directly" }
else                      { Log-Fail "FAIL: A cannot reach C" }

Log-Step "TEST 2: Router A  →  Router B (direct: net_ab)"
docker exec router_a ping -c 3 10.0.1.2
if ($LASTEXITCODE -eq 0) { Log-Ok "PASS: A can reach B directly" }
else                      { Log-Fail "FAIL: A cannot reach B" }

Log-Step "TEST 3: Router B  →  Router C (direct: net_bc)"
docker exec router_b ping -c 3 10.0.2.2
if ($LASTEXITCODE -eq 0) { Log-Ok "PASS: B can reach C directly" }
else                      { Log-Fail "FAIL: B cannot reach C" }

#  Failure / Reconvergence test
Log-Step "TEST 4: Stopping Router C to simulate link failure..."
docker stop router_c
Log-Info "Router C stopped. Waiting 20 s for routers A & B to reconverge..."
Start-Sleep -Seconds 20

Log-Info "=== POST-FAILURE ROUTING TABLES ==="
Show-Table "router_a"
Show-Table "router_b"

Log-Step "TEST 5: Router A should still know about net_bc via Router B"
$routeA = docker exec router_a ip route show 10.0.2.0/24
if ($routeA) {
    Log-Ok "PASS: Router A has a route to 10.0.2.0/24: $routeA"
} else {
    Log-Fail "FAIL: Router A lost route to 10.0.2.0/24"
}

Log-Step "TEST 6: Router A should still know about net_ac via Router B (alternate path)"
$routeA2 = docker exec router_a ip route show 10.0.3.0/24
if ($routeA2) {
    Log-Ok "PASS: Router A still has a route to 10.0.3.0/24: $routeA2"
} else {
    Log-Fail "FAIL: Router A lost route to 10.0.3.0/24"
}

#  Show logs for evidence
Log-Step "Collecting log evidence for report..."
Show-Log "router_a" 50
Show-Log "router_b" 50

#  CLEAN UP
Log-Step "Tearing down..."
docker compose down
Log-Ok "Done. Review output above for your report."
