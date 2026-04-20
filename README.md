# Assignment 4 – Custom Distance-Vector Router

## Project Structure
```
dv-router/
├── router.py          # Main routing daemon (all logic here)
├── Dockerfile         # Builds the Alpine-based router image
├── docker-compose.yml # Defines the 3-router triangle topology
├── test.ps1           # PowerShell test script (Windows)
├── test.sh            # Bash test script (Linux/Mac)
└── README.md
```

---

## Triangle Topology

```
       Router A
      /   10.0.1.1 (net_ab)
     /    10.0.3.1 (net_ac)
    /                    \
Net_AB (10.0.1.0/24)   Net_AC (10.0.3.0/24)
    \                    /
     \                  /
   Router B          Router C
  10.0.1.2 (net_ab)  10.0.2.2 (net_bc)
  10.0.2.1 (net_bc)  10.0.3.2 (net_ac)
          \          /
        Net_BC (10.0.2.0/24)
```

---

## Quick Start

### Prerequisites
- Docker Desktop (with Compose v2)
- Windows: run PowerShell as Administrator for `ip route` to work inside containers

### Docker bridge gateway (important)

On a default user-defined bridge, Docker usually reserves **the first usable address** in each subnet (often `10.0.x.1`) as the bridge gateway. That conflicts with assigning Router A at `10.0.1.1`, etc., and produces `failed to set up container networking: Address already in use`.

This project sets `gateway: 10.0.x.254` in `docker-compose.yml`. If you create networks by hand, use:

```bash
docker network create --subnet=10.0.1.0/24 --gateway=10.0.1.254 net_ab
docker network create --subnet=10.0.2.0/24 --gateway=10.0.2.254 net_bc
docker network create --subnet=10.0.3.0/24 --gateway=10.0.3.254 net_ac
```

### 1. Build & run everything
```powershell
cd dv-router
docker compose up --build
```

### 2. Watch logs in a second terminal
```powershell
docker logs -f router_a
docker logs -f router_b
docker logs -f router_c
```

### 3. Run the automated test suite
```powershell
.\test.ps1
```

### 4. Manual inspection commands
```powershell
# See the kernel routing table inside a container
docker exec router_a ip route

# Ping across subnets
docker exec router_a ping 10.0.3.2   # A -> C (direct)
docker exec router_a ping 10.0.2.2   # A -> C via B (after C stops)

# Simulate Router C failure
docker stop router_c
# Wait 20 seconds, then check router_a's table
docker exec router_a ip route
```

### 5. Tear down
```powershell
docker compose down
```

---

## Design Report

### 1. Overview
The routing daemon implements a simplified Distance-Vector (RIP-like) protocol
using the Bellman-Ford algorithm over UDP/5000.  Each router periodically
broadcasts its routing table as a DV-JSON packet to all configured neighbours
and updates its own table whenever a better path is received.

### 2. Key Components

| Component | Description |
|-----------|-------------|
| `initialize_routing_table()` | Parses `ip addr show` to seed distance-0 entries for all locally attached subnets |
| `broadcast_updates()` | Every 5 s, serialises the routing table as DV-JSON and sends it via UDP to each neighbour |
| `listen_for_updates()` | Blocking UDP socket reader; hands each received packet to `update_logic()` |
| `update_logic()` | Core Bellman-Ford: `new_cost = received_cost + 1`. Installs/updates kernel routes via `ip route replace` |
| `expire_routes()` | Background garbage-collector: marks routes unseen for >15 s as INFINITY and deletes them from the kernel |

### 3. Packet Format (DV-JSON)
```json
{
  "router_id": "10.0.1.1",
  "version": 1.0,
  "routes": [
    {"subnet": "10.0.1.0/24", "distance": 0},
    {"subnet": "10.0.2.0/24", "distance": 1}
  ]
}
```

### 4. Bellman-Ford Implementation
For every route in the incoming packet:
```
new_distance = received_distance + 1   (link cost = 1)

if subnet not in table:          → add entry, install kernel route
if new_distance < current:       → update entry, install better kernel route
if source == neighbor and cost ↑ → update or withdraw kernel route
```
Directly-connected subnets (distance = 0) are never overwritten.

### 5. Loop Prevention – Split Horizon
When building the advertisement for neighbour **N**, the daemon skips any route
whose `source` field equals **N**.  This means:

- If Router A learned about `10.0.2.0/24` from Router B, it will **not**
  advertise that route back to B.
- If Router C goes down, Router B stops receiving updates for `10.0.3.0/24`
  from C.  After `ROUTE_TIMEOUT` (15 s) B expires the route.  Router A, which
  learned `10.0.3.0/24` from C, does the same.  Because A never told B it
  reached `10.0.3.0/24` via B (split horizon), B cannot falsely believe A
  still has a route, so the count-to-infinity loop is broken immediately.

### 6. Convergence Log (example)
```
[BOOT]  Router A starting  MY_IP=10.0.1.1  NEIGHBORS=[10.0.1.2, 10.0.3.2]
[INIT]  Routing table initialised: ['10.0.1.0/24', '10.0.3.0/24']
[RX]    2 route(s) from 10.0.1.2
[BF]    NEW   10.0.2.0/24   via 10.0.1.2  dist=1
[RX]    2 route(s) from 10.0.3.2
── Routing Table – 10.0.1.1 ──────────────────────
  10.0.1.0/24   dist=0   next_hop=0.0.0.0
  10.0.2.0/24   dist=1   next_hop=10.0.1.2
  10.0.3.0/24   dist=0   next_hop=0.0.0.0
─────────────────────────────────────────────────
... (Router C stops) ...
[EXPIRE] 10.0.2.0/24 via 10.0.2.1 timed out     ← on Router B
[RX]     1 route(s) from 10.0.1.2                ← Router A hears update
[BF]     UPDATE 10.0.3.0/24  via 10.0.1.2 dist=2 ← longer path accepted
```

### 7. Testing
- **Normal convergence**: all three routers exchange updates; after 2 rounds
  (≈10 s) every router knows all three subnets.
- **Failure test**: `docker stop router_c` → within 15–20 s routers A and B
  expire C's directly-advertised routes and reconverge via the alternate path.
- **Verification**: `docker exec router_a ip route` shows updated kernel routes.
