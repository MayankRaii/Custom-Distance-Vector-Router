# failure-tests.ps1
# Do alag tests: (1) NODE FAIL  (2) LINK FAIL
# Pehle routers chal rahe hon:  docker compose up -d
# Folder se run karo:  .\failure-tests.ps1

$CYAN = "`e[36m"; $YELLOW = "`e[33m"; $GREEN = "`e[32m"; $RESET = "`e[0m"
function Say($m) { Write-Host "${CYAN}[TEST]${RESET} $m" }
function Info($m) { Write-Host "${YELLOW}[INFO]${RESET} $m" }
function Ok($m) { Write-Host "${GREEN}[ OK ]${RESET} $m" }

# Compose project ke networks ka naam *net_ac jaisa hota hai
$netAc = docker network ls --format "{{.Name}}" | Where-Object { $_ -match "net_ac$" } | Select-Object -First 1
if (-not $netAc) {
    Write-Host "net_ac wala network nahi mila. Pehle 'docker compose up -d' chalao isi folder se."
    exit 1
}
Info "Using Docker network for link-fail test: $netAc"

function Show-Routes {
    param([string]$name)
    Info "ip route on $name"
    docker exec $name ip route 2>$null
}

# ─── Baseline ─────────────────────────────────────────
Say "Baseline (sab healthy) — 8s wait for prints..."
Start-Sleep -Seconds 2
Show-Routes "router_a"
Show-Routes "router_b"

# ═══ 1) NODE FAIL  — poora Router C band ═══════════════════════════
Say "NODE FAIL: docker stop router_c (C poora down)"
docker stop router_c
Info "15s wait — DV expire / neighbour silent..."
Start-Sleep -Seconds 15
Show-Routes "router_a"
Show-Routes "router_b"
Info "NOTE: Agar A ab bhi '10.0.3.0/24 dev eth1' dikhata hai, ye isliye hai ki A ka net_ac interface ab bhi juda hai — ye LINK fail nahi, sirf C process band hai."
Say "NODE FAIL restore: docker start router_c"
docker start router_c
Start-Sleep -Seconds 18
Ok "C wapas up. Baseline restore hone do (next step se pehle)."
Show-Routes "router_a"

# ═══ 2) LINK FAIL  — sirf A–C wala link (interface hatao) ═══════════
Say "LINK FAIL: A ko net_ac se disconnect — A↔C direct link tutega; B aur C still connected"
docker network disconnect $netAc router_a 2>$null
if ($LASTEXITCODE -ne 0) {
    Info "disconnect fail — shayad pehle se disconnected; 'docker network connect' se wapas jod sakte ho"
}
Info "18s wait — B se 10.0.3.0/24 ka alternate path A ko milna chahiye (agar DV table me update ho)"
Start-Sleep -Seconds 18
Show-Routes "router_a"
Show-Routes "router_b"

Say "LINK RESTORE: A ko net_ac par wapas jodo (IP assignment)"
docker network connect $netAc router_a --ip 10.0.3.1 2>$null
Start-Sleep -Seconds 12
Ok "Link restore attempt complete."
Show-Routes "router_a"

Write-Host ""
Info "Report me likho:"
Write-Host "  - NODE FAIL = docker stop router_c  (poora node)"
Write-Host "  - LINK FAIL = docker network disconnect <net_ac> router_a  (sirf ek link)"
Write-Host ""
