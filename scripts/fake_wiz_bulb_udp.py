"""Local fake WiZ UDP receiver for packet cadence and payload validation."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PacketRecord:
    received_at: float
    address: tuple[str, int]
    raw: bytes
    payload: dict[str, Any]


def decode_wiz_packet(packet: bytes) -> dict[str, Any]:
    """Decode a WiZ UDP JSON packet."""

    data = json.loads(packet.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("WiZ packet must decode to a JSON object")
    return data


def validate_screen_sync_packet(payload: dict[str, Any], *, rgb_only: bool = False) -> list[str]:
    """Validate the sender's RGBW/RGBCW contract; white channels are legal."""

    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["packet is not an object"]
    if payload.get("method") != "setPilot":
        errors.append("method is not setPilot")
    params = payload.get("params")
    if not isinstance(params, dict):
        return [*errors, "params is not an object"]
    for channel in ("r", "g", "b", "c", "w"):
        if channel not in params:
            errors.append(f"missing channel {channel}")
        elif type(params[channel]) is not int or not 0 <= params[channel] <= 255:
            errors.append(f"channel {channel} must be an integer from 0 to 255")
    if type(params.get("state")) is not bool:
        errors.append("state must be a boolean")
    elif all(type(params.get(ch)) is int for ch in ("r", "g", "b", "c", "w")):
        if params["state"] != any(params[ch] for ch in ("r", "g", "b", "c", "w")):
            errors.append("state must match whether the output channels contain light")
    if "dimming" in params and (type(params["dimming"]) is not int or not 10 <= params["dimming"] <= 100):
        errors.append("dimming must be an integer from 10 to 100")
    if rgb_only and (params.get("c") != 0 or params.get("w") != 0):
        errors.append("white channels must be zero when explicitly checking pure RGB")
    return errors


class FakeWizBulbUDP:
    """Threaded UDP receiver that records WiZ packets for local tests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 38899, *, rgb_only: bool = False):
        self.host = host
        self.port = port
        self.rgb_only = rgb_only
        self.records: list[PacketRecord] = []
        self.errors: list[str] = []
        self._bound_address = (host, port)
        self._running = False
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._socket is None:
            return self._bound_address
        host, port = self._socket.getsockname()
        return (str(host), int(port))

    def start(self) -> None:
        if self._running:
            return
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((self.host, self.port))
        host, port = self._socket.getsockname()
        self._bound_address = (str(host), int(port))
        self._socket.settimeout(0.1)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="FakeWizBulbUDP")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _run(self) -> None:
        assert self._socket is not None
        while self._running:
            try:
                packet, address = self._socket.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                payload = decode_wiz_packet(packet)
                self.errors.extend(validate_screen_sync_packet(payload, rgb_only=self.rgb_only))
            except Exception as exc:
                payload = {"decode_error": str(exc)}
                self.errors.append(str(exc))
            self.records.append(
                PacketRecord(
                    received_at=time.perf_counter(),
                    address=(address[0], address[1]),
                    raw=packet,
                    payload=payload,
                )
            )

    def summary(self) -> dict[str, Any]:
        intervals = [
            self.records[index].received_at - self.records[index - 1].received_at
            for index in range(1, len(self.records))
        ]
        average_interval = sum(intervals) / len(intervals) if intervals else 0.0
        return {
            "address": f"{self.address[0]}:{self.address[1]}",
            "packetCount": len(self.records),
            "averageIntervalMs": average_interval * 1000,
            "averageRateHz": 1.0 / average_interval if average_interval > 0 else 0.0,
            "validationErrors": self.errors,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local fake WiZ UDP bulb receiver.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=38899)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--rgb-only", action="store_true", help="Require zero white channels for an intentional pure-RGB test.")
    parser.add_argument(
        "--require-packets",
        action="store_true",
        help="Exit with code 1 if no packets were received.",
    )
    args = parser.parse_args()

    receiver = FakeWizBulbUDP(args.host, args.port, rgb_only=args.rgb_only)
    receiver.start()
    print(f"Listening on {receiver.address[0]}:{receiver.address[1]} for {args.seconds:.1f}s")
    try:
        time.sleep(max(0.1, args.seconds))
    finally:
        receiver.stop()
    summary = receiver.summary()
    if summary["packetCount"] == 0:
        summary["note"] = (
            "No packets received. This is expected unless another process sends WiZ UDP "
            "setPilot packets to this host and port."
        )
    print(json.dumps(summary, indent=2))
    return 1 if receiver.errors or (args.require_packets and summary["packetCount"] == 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
