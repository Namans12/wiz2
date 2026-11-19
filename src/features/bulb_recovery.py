"""Conservative rediscovery of saved lights whose addresses stopped responding."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from ..core.device_identity import mac_identity


@dataclass(frozen=True)
class RecoveredBulb:
    old_ip: str
    new_ip: str
    name: str
    mac: str


async def fetch_fresh_identity(controller, ip, *, timeout: float = 1.8):
    """Read the MAC from a fresh response, not pywizlight's cached attribute."""
    for _ in range(2):
        try:
            response = await asyncio.wait_for(controller._get_bulb(ip).getBulbConfig(), timeout)
        except (OSError, asyncio.TimeoutError):
            continue
        if isinstance(response, dict) and isinstance(response.get("result"), dict):
            identity = mac_identity(response["result"].get("mac"))
            if identity:
                return identity
    return None


async def refresh_verified_targets(controller, bulbs):
    """Reject an address that now belongs to a different saved device."""
    saved = list(bulbs)
    reachable = await controller.refresh_screen_sync_targets([bulb.ip for bulb in saved])
    by_ip = {bulb.ip: bulb for bulb in saved}
    to_verify = [ip for ip in reachable if ip in by_ip and mac_identity(by_ip[ip].mac)]
    results = await asyncio.gather(
        *(fetch_fresh_identity(controller, ip) for ip in to_verify),
        return_exceptions=True,
    )
    accepted = set(reachable) - set(to_verify)
    for ip, identity in zip(to_verify, results):
        if isinstance(identity, str) and identity == mac_identity(by_ip[ip].mac):
            accepted.add(ip)
    return [ip for ip in reachable if ip in accepted]


async def recover_saved_bulbs(controller, bulbs, *, discovery_timeout: float = 6.0, target_ips=None):
    """Find and verify changed addresses without modifying the saved config.

    Ambiguous identities and unverified replies are ignored. An occupied address
    is accepted only when its saved occupant is also verified to move in the
    same batch, so swaps do not redirect commands to another device.
    """
    saved = list(bulbs)
    identities = defaultdict(list)
    for bulb in saved:
        if identity := mac_identity(bulb.mac):
            identities[identity].append(bulb)
    if not identities:
        return []
    candidates = defaultdict(set)
    expected = {identity for identity, entries in identities.items()
                if target_ips is None or any(bulb.ip in target_ips for bulb in entries)}
    for _ in range(2):
        try:
            found = await asyncio.wait_for(controller.discover("255.255.255.255"), timeout=discovery_timeout)
        except (OSError, asyncio.TimeoutError):
            continue
        for device in found:
            if not isinstance(device, dict):
                continue
            identity = mac_identity(device.get("mac"))
            address = device.get("ip")
            if identity in identities and isinstance(address, str) and address:
                candidates[identity].add(address)
        if expected.issubset(candidates):
            break
    requests = []
    for identity, entries in identities.items():
        addresses = candidates.get(identity, set())
        if len(entries) != 1 or len(addresses) != 1:
            continue
        bulb = entries[0]
        address = next(iter(addresses))
        if address == bulb.ip:
            continue
        requests.append((bulb, identity, address))
    if target_ips is not None:
        # A target can move into an occupied address only if its occupant
        # moves too. Include those dependencies without probing other lights.
        by_ip = {bulb.ip: address for bulb, _, address in requests}
        needed = set(target_ips)
        pending = list(target_ips)
        while pending:
            address = by_ip.get(pending.pop())
            if address in by_ip and address not in needed:
                needed.add(address)
                pending.append(address)
        requests = [request for request in requests if request[0].ip in needed]
    states = await asyncio.gather(
        *(fetch_fresh_identity(controller, address) for _, _, address in requests),
        return_exceptions=True,
    )
    recovered = []
    for (bulb, identity, address), actual_identity in zip(requests, states):
        if not isinstance(actual_identity, str) or actual_identity != identity:
            continue
        recovered.append(RecoveredBulb(bulb.ip, address, bulb.name, bulb.mac))
    # Occupied addresses are allowed only when their current saved occupant is
    # also verified to move in this same batch. Recheck until stable so a
    # failed verification cannot leave part of a chain or swap accepted.
    occupied = {bulb.ip for bulb in saved}
    counts = defaultdict(int)
    for item in recovered:
        counts[item.new_ip] += 1
    recovered = [item for item in recovered if counts[item.new_ip] == 1]
    while True:
        moving = {item.old_ip for item in recovered}
        safe = [item for item in recovered if item.new_ip not in occupied or item.new_ip in moving]
        if len(safe) == len(recovered):
            return safe
        recovered = safe
