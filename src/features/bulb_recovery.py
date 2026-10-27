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
    try:
        response = await asyncio.wait_for(controller._get_bulb(ip).getBulbConfig(), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        return None
    return mac_identity(response["result"].get("mac"))


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

    Ambiguous identities, already configured addresses and unverified replies
    are ignored, so discovery alone never redirects commands to another device.
    """
    saved = list(bulbs)
    identities = defaultdict(list)
    for bulb in saved:
        if identity := mac_identity(bulb.mac):
            identities[identity].append(bulb)
    if not identities:
        return []
    try:
        found = await asyncio.wait_for(controller.discover("255.255.255.255"), timeout=discovery_timeout)
    except (OSError, asyncio.TimeoutError):
        return []
    candidates = defaultdict(set)
    for device in found:
        if not isinstance(device, dict):
            continue
        identity = mac_identity(device.get("mac"))
        address = device.get("ip")
        if identity in identities and isinstance(address, str) and address:
            candidates[identity].add(address)
    occupied = {bulb.ip for bulb in saved}
    requests = []
    for identity, entries in identities.items():
        addresses = candidates.get(identity, set())
        if len(entries) != 1 or len(addresses) != 1:
            continue
        bulb = entries[0]
        if target_ips is not None and bulb.ip not in target_ips:
            continue
        address = next(iter(addresses))
        if address == bulb.ip or address in occupied:
            continue
        requests.append((bulb, identity, address))
    states = await asyncio.gather(
        *(fetch_fresh_identity(controller, address) for _, _, address in requests),
        return_exceptions=True,
    )
    recovered = []
    for (bulb, identity, address), actual_identity in zip(requests, states):
        if not isinstance(actual_identity, str) or actual_identity != identity:
            continue
        recovered.append(RecoveredBulb(bulb.ip, address, bulb.name, bulb.mac))
    return recovered
