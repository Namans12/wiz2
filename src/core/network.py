"""Local-network helpers: find this PC's interfaces and the WiZ bulbs on them."""

from __future__ import annotations

import json
import socket
import time
from typing import Iterable, Optional

from .device_identity import mac_identity

WIZ_PORT = 38899
# pywizlight's discovery message. ``register: False`` only asks for a reply;
# it does not subscribe this host to the bulb's state pushes.
_DISCOVERY_MESSAGE = json.dumps(
    {
        "method": "registration",
        "params": {"phoneMac": "AAAAAAAAAAAA", "register": False, "phoneIp": "1.2.3.4", "id": "1"},
    }
).encode()
LIMITED_BROADCAST = "255.255.255.255"


def local_ipv4_addresses() -> list[str]:
    """IPv4 addresses of this machine's usable interfaces, default route first."""

    candidates: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket sends nothing; it only asks the OS which
        # local address the default route would use.
        probe.connect(("192.0.2.1", 9))
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.append(info[4][0])
    except OSError:
        pass

    addresses: list[str] = []
    for address in candidates:
        if address.startswith(("127.", "169.254.", "0.")) or address in addresses:
            continue
        addresses.append(address)
    return addresses


def _directed_broadcast(address: str) -> Optional[str]:
    """Broadcast address assuming the usual home /24 network."""

    parts = address.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3] + ["255"])


def discover_wiz_bulbs(
    broadcast_addresses: Optional[Iterable[str]] = None,
    wait_time: float = 2.5,
) -> list[dict]:
    """Find WiZ bulbs by broadcasting from every local interface.

    A single broadcast to one hard-coded subnet misses bulbs on any other
    network, and ``255.255.255.255`` sent from an unbound socket leaves through
    whichever adapter Windows prefers, which can be a virtual one. Binding one
    socket per interface makes each broadcast leave through that interface.

    ``broadcast_addresses`` restricts the search to explicit destinations.
    Returns ``[{"ip": ..., "mac": ...}]`` sorted by address. Blocking; run it
    in a worker thread from async code.
    """

    explicit = [address for address in (broadcast_addresses or ()) if address]
    sockets: list[tuple[socket.socket, list[str]]] = []
    for local in local_ipv4_addresses() or [""]:
        if explicit:
            destinations = list(explicit)
        else:
            destinations = [LIMITED_BROADCAST]
            directed = _directed_broadcast(local) if local else None
            if directed:
                destinations.append(directed)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((local, 0))
            sock.setblocking(False)
        except OSError:
            sock.close()
            continue
        sockets.append((sock, destinations))

    found: dict[str, Optional[str]] = {}
    try:
        deadline = time.monotonic() + max(0.2, float(wait_time))
        next_send = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                next_send = now + 0.7
                for sock, destinations in sockets:
                    for destination in destinations:
                        try:
                            sock.sendto(_DISCOVERY_MESSAGE, (destination, WIZ_PORT))
                        except OSError:
                            pass
            received = False
            for sock, _ in sockets:
                try:
                    data, sender = sock.recvfrom(4096)
                except OSError:
                    continue
                received = True
                try:
                    result = json.loads(data).get("result")
                except (ValueError, AttributeError):
                    continue
                if isinstance(result, dict) and mac_identity(result.get("mac")):
                    found[sender[0]] = result.get("mac")
            if not received:
                time.sleep(0.02)
    finally:
        for sock, _ in sockets:
            sock.close()

    def sort_key(item: tuple[str, Optional[str]]) -> tuple:
        try:
            return tuple(int(part) for part in item[0].split("."))
        except ValueError:
            return (999,)

    return [{"ip": ip, "mac": mac} for ip, mac in sorted(found.items(), key=sort_key)]
