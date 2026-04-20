import socket
import json
import threading
import time
import os
import subprocess
import ipaddress
import re

MY_IP      = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS  = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT       = 5000

UPDATE_INTERVAL  = 5    # seconds between broadcasts
ROUTE_TIMEOUT    = 15   # seconds before an unrefreshed route is expired
INFINITY         = 16   # RIP-style infinity (max hop count)

routing_table: dict = {}
routing_lock  = threading.Lock()

def get_local_subnets() -> list[str]:
    """Return every directly-connected subnet by parsing 'ip addr show'."""
    subnets = []
    try:
        result = subprocess.run(["ip", "addr", "show"],
                                capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if m:
                ip_str = m.group(1)
                prefix  = int(m.group(2))
                if not ip_str.startswith("127."):
                    net = ipaddress.IPv4Network(f"{ip_str}/{prefix}", strict=False)
                    subnets.append(str(net))
    except Exception as e:
        print(f"[INIT] Error reading local subnets: {e}")
    return subnets

def initialize_routing_table():
    now = time.time()
    subnets = get_local_subnets()
    with routing_lock:
        for subnet in subnets:
            routing_table[subnet] = {
                "distance": 0,
                "next_hop": "0.0.0.0",
                "source":   "self",
                "updated":  now,
            }
    print(f"[INIT] Routing table initialised with {len(subnets)} local subnet(s): {subnets}")
    print_routing_table()

def build_packet_for(neighbor_ip: str) -> bytes:
    """
    Build a DV-JSON packet applying Split Horizon:
    Do NOT advertise a route back to the neighbour it was learned from.
    """
    with routing_lock:
        routes = []
        for subnet, info in routing_table.items():
            # Split Horizon: skip routes learned from this exact neighbour
            if info["source"] == neighbor_ip:
                continue
            if info["distance"] < INFINITY:
                routes.append({"subnet": subnet, "distance": info["distance"]})

    packet = {
        "router_id": MY_IP,
        "version":   1.0,
        "routes":    routes,
    }
    return json.dumps(packet).encode()


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    while True:
        for neighbor in NEIGHBORS:
            try:
                data = build_packet_for(neighbor)
                sock.sendto(data, (neighbor, PORT))
            except Exception as e:
                print(f"[TX] Error sending to {neighbor}: {e}")

        print(f"[TX] Broadcasted routes to {NEIGHBORS}")
        time.sleep(UPDATE_INTERVAL)

def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1.0)
    print(f"[RX] Listening for DV updates on UDP port {PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            packet = json.loads(data.decode())
            # Use the real source IP as next-hop (addr[0]) so it is
            # always reachable on the same subnet as us.
            neighbor_ip = addr[0]
            routes      = packet.get("routes", [])
            print(f"[RX] {len(routes)} route(s) from {neighbor_ip}")
            update_logic(neighbor_ip, routes)

        except socket.timeout:
            continue
        except json.JSONDecodeError as e:
            print(f"[RX] Malformed packet: {e}")
        except Exception as e:
            print(f"[RX] Unexpected error: {e}")

def update_logic(neighbor_ip: str, routes_from_neighbor: list):
    """
    Bellman-Ford:   new_cost = advertised_cost + link_cost (link_cost = 1)

    Split Horizon is enforced on the TX side (build_packet_for).
    Here we also handle route withdrawal: if a neighbour re-advertises
    a route it previously taught us but with cost >= INFINITY, we remove it.
    """
    changed = False
    now     = time.time()

    with routing_lock:
        for route in routes_from_neighbor:
            subnet            = route.get("subnet")
            received_distance = int(route.get("distance", INFINITY))
            if not subnet:
                continue

            new_distance = min(received_distance + 1, INFINITY)
            current      = routing_table.get(subnet)

            if current is None:
                # Brand-new subnet discovered
                if new_distance < INFINITY:
                    routing_table[subnet] = {
                        "distance": new_distance,
                        "next_hop": neighbor_ip,
                        "source":   neighbor_ip,
                        "updated":  now,
                    }
                    _add_kernel_route(subnet, neighbor_ip)
                    print(f"[BF] NEW  {subnet:20s}  via {neighbor_ip}  dist={new_distance}")
                    changed = True

            elif current["distance"] == 0 and current["source"] == "self":
                # Directly connected – never overwrite
                pass

            elif new_distance < current["distance"]:
                # Strictly better path
                routing_table[subnet] = {
                    "distance": new_distance,
                    "next_hop": neighbor_ip,
                    "source":   neighbor_ip,
                    "updated":  now,
                }
                _add_kernel_route(subnet, neighbor_ip)
                print(f"[BF] BETTER {subnet:20s}  via {neighbor_ip}  dist={new_distance}")
                changed = True

            elif current["source"] == neighbor_ip:
                # Same neighbour – accept updated distance (may be worse or same)
                current["updated"] = now
                if new_distance != current["distance"]:
                    if new_distance >= INFINITY:
                        # Route withdrawal / link failure
                        _del_kernel_route(subnet)
                        routing_table[subnet]["distance"] = INFINITY
                        routing_table[subnet]["next_hop"] = "0.0.0.0"
                        print(f"[BF] UNREACH {subnet:20s}  (withdrawn by {neighbor_ip})")
                    else:
                        routing_table[subnet]["distance"] = new_distance
                        _add_kernel_route(subnet, neighbor_ip)
                        print(f"[BF] UPDATE {subnet:20s}  via {neighbor_ip}  dist={new_distance}")
                    changed = True

    if changed:
        print_routing_table()

def expire_routes():
    """
    Background thread: remove routes not refreshed within ROUTE_TIMEOUT seconds.
    Directly connected (source == 'self') routes are never expired.
    """
    while True:
        time.sleep(UPDATE_INTERVAL)
        now     = time.time()
        changed = False

        with routing_lock:
            for subnet, info in list(routing_table.items()):
                if info["source"] == "self":
                    continue
                if info["distance"] < INFINITY and (now - info["updated"]) > ROUTE_TIMEOUT:
                    print(f"[EXPIRE] {subnet}  via {info['next_hop']} timed out")
                    _del_kernel_route(subnet)
                    routing_table[subnet]["distance"] = INFINITY
                    routing_table[subnet]["next_hop"] = "0.0.0.0"
                    changed = True

        if changed:
            print_routing_table()

def _add_kernel_route(subnet: str, via: str):
    ret = os.system(f"ip route replace {subnet} via {via} 2>/dev/null")
    if ret != 0:
        print(f"[KERN] Warning: could not replace route {subnet} via {via}")


def _del_kernel_route(subnet: str):
    os.system(f"ip route del {subnet} 2>/dev/null")

def print_routing_table():
    print("\n┌─────────────────────────────────────────────────────┐")
    print(f"│  Routing Table  –  {MY_IP:<34s}│")
    print("├────────────────────┬──────────┬─────────────────────┤")
    print("│ Subnet             │ Distance │ Next Hop            │")
    print("├────────────────────┼──────────┼─────────────────────┤")
    with routing_lock:
        for subnet, info in sorted(routing_table.items()):
            dist = "INF" if info["distance"] >= INFINITY else str(info["distance"])
            hop  = info["next_hop"]
            print(f"│ {subnet:<18s} │ {dist:^8s} │ {hop:<19s} │")
    print("└────────────────────┴──────────┴─────────────────────┘\n")

if __name__ == "__main__":
    print(f"[BOOT] Router starting  MY_IP={MY_IP}  NEIGHBORS={NEIGHBORS}")

    # Give Docker a moment to finish configuring network interfaces
    time.sleep(3)

    initialize_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=expire_routes,     daemon=True).start()

    # Main thread blocks here receiving updates
    listen_for_updates()
