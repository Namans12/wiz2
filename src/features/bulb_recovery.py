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


async def recover_saved_bulbs(controller, bulbs, *, discovery_timeout: float = 6.0):
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
        address = next(iter(addresses))
        if address == bulb.ip or address in occupied:
            continue
        requests.append((bulb, identity, address))
    states = await asyncio.gather(
        *(controller._resolve_screen_sync_target(address) for _, _, address in requests),
        return_exceptions=True,
    )
    recovered = []
    for (bulb, identity, address), state in zip(requests, states):
        if isinstance(state, BaseException) or state is None or mac_identity(state.mac) != identity:
            continue
        recovered.append(RecoveredBulb(bulb.ip, address, bulb.name, bulb.mac))
    return recovered
