import socket
import json
import threading
import time
import os
import subprocess
import ipaddress
import re
from typing import Optional

MY_IP      = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS  = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT       = 5000

UPDATE_INTERVAL  = 3    
ROUTE_TIMEOUT    = 12   
INFINITY         = 16   

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

def _sync_local_subnets_unlocked() -> bool:
    """
    Re-read 'ip addr' and align self routes with current interfaces. When
    Docker detaches a network, the kernel drops the on-link route but we must
    drop the stale *self* entry or Bellman–Ford will never re-learn the prefix
    and neighbors may keep a dead path that split horizon hides from you.
    """
    now = time.time()
    new_local = set(get_local_subnets())
    changed = False
    for subnet, info in list(routing_table.items()):
        if info.get("source") == "self" and subnet not in new_local:
            del routing_table[subnet]
            changed = True
            print(f"[LOCAL] Interface gone for {subnet} — removed self route")
    for subnet in new_local:
        ex = routing_table.get(subnet)
        if ex is None:
            routing_table[subnet] = {
                "distance": 0,
                "next_hop": "0.0.0.0",
                "source":   "self",
                "updated":  now,
            }
            changed = True
            print(f"[LOCAL] New on-link subnet {subnet}")
        elif ex.get("source") != "self":
            if ex["distance"] < INFINITY and ex.get("next_hop") and ex.get("next_hop") != "0.0.0.0":
                _del_kernel_route(subnet, str(ex.get("next_hop")))
            routing_table[subnet] = {
                "distance": 0,
                "next_hop": "0.0.0.0",
                "source":   "self",
                "updated":  now,
            }
            changed = True
            print(f"[LOCAL] Reconnected on-link: {subnet}")
    return changed

def sync_local_subnets() -> bool:
    with routing_lock:
        return _sync_local_subnets_unlocked()

def build_packet_for(neighbor_ip: str) -> bytes:
    """
    Split horizon with poisoned reverse: for neighbour N, if N is the first
    hop to a destination, still send that prefix with metric INFINITY (RIP-style
    16) instead of omitting. That gives peers explicit refresh + invalidation of
    loop/obsolete paths, while not locking stale routes (omit + global liveness).
    """
    with routing_lock:
        routes = []
        for subnet, info in routing_table.items():
            if info.get("source") == "self":
                routes.append({"subnet": subnet, "distance": 0})
                continue
            if str(info.get("next_hop")) == str(neighbor_ip):
                if info["distance"] < INFINITY:
                    routes.append({"subnet": subnet, "distance": INFINITY})
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
        ch = sync_local_subnets()
        if ch:
            print_routing_table()
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
    We only accept direct neighbours in NEIGHBORS.  Poisoned reverse in
    build_packet_for ensures every hop still gets explicit per-prefix updates.
    """
    if neighbor_ip not in NEIGHBORS:
        return
    changed = False
    now     = time.time()

    with routing_lock:
        local = set(get_local_subnets())
        for route in routes_from_neighbor:
            subnet            = route.get("subnet")
            received_distance = int(route.get("distance", INFINITY))
            if not subnet:
                continue

            new_distance = min(received_distance + 1, INFINITY)
            current      = routing_table.get(subnet)

            if subnet in local and current and current.get("source") != "self":
                nh = str(current.get("next_hop", "")) or None
                if nh and nh != "0.0.0.0":
                    _del_kernel_route(subnet, nh)
                routing_table[subnet] = {
                    "distance": 0,
                    "next_hop": "0.0.0.0",
                    "source":   "self",
                    "updated":  now,
                }
                print(f"[BF] PREFER-LOCAL {subnet:20s}  (on-link)")
                changed = True
                continue

            if current is None:
                if subnet in local:
                    routing_table[subnet] = {
                        "distance": 0,
                        "next_hop": "0.0.0.0",
                        "source":   "self",
                        "updated":  now,
                    }
                    changed = True
                    print(f"[BF] SELF   {subnet:20s}  (on-link, repair)")
                    continue
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
                pass

            elif new_distance < current["distance"]:
                if subnet in local:
                    continue
                routing_table[subnet] = {
                    "distance": new_distance,
                    "next_hop": neighbor_ip,
                    "source":   neighbor_ip,
                    "updated":  now,
                }
                _add_kernel_route(subnet, neighbor_ip)
                print(f"[BF] BETTER {subnet:20s}  via {neighbor_ip}  dist={new_distance}")
                changed = True

            elif (
                not (current.get("source") == "self")
                and new_distance == current.get("distance")
                and new_distance < INFINITY
                and str(neighbor_ip) != str(current.get("next_hop", ""))
            ):
                if subnet in local:
                    continue
                a = int(ipaddress.IPv4Address(neighbor_ip))
                b = int(ipaddress.IPv4Address(str(current.get("next_hop", "0.0.0.0"))))
                if a < b:
                    routing_table[subnet] = {
                        "distance":     new_distance,
                        "next_hop":     neighbor_ip,
                        "source":       neighbor_ip,
                        "updated":      now,
                    }
                    _add_kernel_route(subnet, neighbor_ip)
                    print(f"[BF] TIE-LOW  {subnet:20s}  via {neighbor_ip}  dist={new_distance}")
                    changed = True

            elif current["source"] == neighbor_ip:
                current["updated"] = now
                if new_distance != current["distance"]:
                    if new_distance >= INFINITY:
                        _del_kernel_route(subnet, str(current.get("next_hop", "")) or None)
                        del routing_table[subnet]
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
            if _sync_local_subnets_unlocked():
                changed = True
            for subnet, info in list(routing_table.items()):
                if info["source"] == "self":
                    continue
                if info["distance"] < INFINITY and (now - info["updated"]) > ROUTE_TIMEOUT:
                    if subnet in get_local_subnets():
                        if info.get("source") != "self":
                            routing_table[subnet] = {
                                "distance": 0, "next_hop": "0.0.0.0", "source": "self", "updated": now,
                            }
                        changed = True
                        continue
                    print(f"[EXPIRE] {subnet}  via {info['next_hop']} timed out")
                    _del_kernel_route(subnet, str(info.get("next_hop", "")) or None)
                    del routing_table[subnet]
                    changed = True

        if changed:
            print_routing_table()

def _add_kernel_route(subnet: str, via: str) -> None:
    if subnet in get_local_subnets():
        return
    ret = os.system(f"ip route replace {subnet} via {via} 2>/dev/null")
    if ret != 0:
        print(f"[KERN] Warning: could not replace route {subnet} via {via}")


def _del_kernel_route(subnet: str, via: Optional[str] = None) -> None:
    """Remove a remote (via) route; never wipe an on-link prefix the kernel owns."""
    if via and str(via) and str(via) != "0.0.0.0":
        os.system(f"ip route del {subnet} via {via} 2>/dev/null")
        return
    if subnet in get_local_subnets():
        return
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

    time.sleep(3)

    initialize_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=expire_routes,     daemon=True).start()

    listen_for_updates()
