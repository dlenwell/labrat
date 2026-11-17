# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Learn about MikroTik devices from their MNDP announcements, passively.

RouterOS broadcasts MikroTik Neighbor Discovery Protocol (MNDP) packets on
UDP port 5678 on its discovery interfaces (the "LAN" interface list by
default) about once a minute. Each packet carries the device's identity,
board model, RouterOS version, uptime, a per-device software ID, and the
announcing interface's name and MAC.

This adapter only listens: it binds UDP 5678 for a configured number of
seconds, decodes what arrives, and never sends a packet. It needs no
credentials, but broadcasts do not cross VLANs or routed firewalls, so the
collector must share a network segment with the announcing interfaces.

Enable it in ``local_config.toml``::

    [mndp]
    listen_seconds = 65   # 5 to 300; a full announcement cycle is ~60 s

Observations are source-scoped like any other: a MikroTik also seen as a
UniFi LLDP neighbor appears once per source until linked to one inventory
device.
"""

from collections.abc import Callable, Mapping
import socket
import struct
import time
from typing import Any

from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, PortObservation

#: MNDP TLV type codes LabRat reads.
_TLV_NAMES = {
    1: "mac", 5: "identity", 7: "version", 8: "platform", 10: "uptime",
    11: "software_id", 12: "board", 16: "interface", 17: "ipv4",
}

#: Default and allowed listening windows, in seconds.
DEFAULT_LISTEN_SECONDS = 65
MIN_LISTEN_SECONDS = 5
MAX_LISTEN_SECONDS = 300


def decode_packet(packet: bytes) -> dict[str, Any]:
    """Decode one MNDP packet into named fields.

    The packet is a 4-byte header followed by type/length/value records with
    big-endian 16-bit type and length. Unknown types are ignored, and a
    truncated record ends decoding.

    Args:
        packet: Raw UDP payload.

    Returns:
        dict[str, Any]: Any of ``mac`` (lowercase ``aa:bb:..``), ``identity``,
            ``version``, ``platform``, ``uptime_seconds``, ``software_id``,
            ``board``, ``interface``, and ``ipv4`` (dotted string).
    """
    fields: dict[str, Any] = {}
    offset = 4
    while offset + 4 <= len(packet):
        kind, length = struct.unpack("!HH", packet[offset:offset + 4])
        value = packet[offset + 4:offset + 4 + length]
        if len(value) < length:
            break
        offset += 4 + length
        name = _TLV_NAMES.get(kind)
        if name is None:
            continue
        if name == "mac" and length == 6:
            fields["mac"] = ":".join(f"{byte:02x}" for byte in value)
        elif name == "uptime" and length == 4:
            fields["uptime_seconds"] = struct.unpack("<I", value)[0]
        elif name == "ipv4" and length == 4:
            fields["ipv4"] = socket.inet_ntoa(value)
        elif name not in ("mac", "uptime", "ipv4"):
            text = value.decode("utf-8", "replace").strip()
            if text:
                fields[name] = text
    return fields


def category_for_board(board: str | None) -> str:
    """Guess a physical category from a RouterBOARD model name.

    Args:
        board: Board name such as ``CRS326-24G-2S+`` or ``hEX S``.

    Returns:
        str: ``switch`` for CRS/CSS models and the older SwOS-only
            RB250GS/RB260GS boards, ``access_point`` for cAP/wAP models,
            otherwise ``router``.
    """
    name = (board or "").upper()
    if name.startswith(("CRS", "CSS", "RB250GS", "RB260GS")):
        return "switch"
    if name.startswith(("CAP", "WAP")):
        return "access_point"
    return "router"


def _listen(seconds: float, port: int = 5678) -> list[bytes]:
    """Collect MNDP packets for a while without sending anything.

    Args:
        seconds: How long to listen.
        port: UDP port; MNDP uses 5678.

    Returns:
        list[bytes]: Raw packets received.
    """
    packets: list[bytes] = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(("", port))
        listener.settimeout(1.0)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                packet, _ = listener.recvfrom(2048)
            except socket.timeout:
                continue
            packets.append(packet)
    finally:
        listener.close()
    return packets


class MndpAdapter:
    """Turn passively heard MNDP announcements into device observations."""

    def __init__(self, listen: Callable[[float], list[bytes]] | None = None) -> None:
        """Use the real UDP listener unless tests supply recorded packets.

        Args:
            listen: Callable taking a duration and returning raw packets.
        """
        self.listen = listen if listen is not None else _listen

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Listen for the configured window and report each MikroTik heard.

        Devices are keyed by RouterOS's software ID (one per device), falling
        back to identity plus board. Each announcing interface becomes a
        port. Port lists are never complete, because MNDP only covers the
        interfaces that announce.

        Args:
            settings: The ``[mndp]`` section; ``listen_seconds`` is optional.

        Returns:
            DiscoveryBatch: MikroTik devices and their announcing interfaces.

        Raises:
            ValueError: ``listen_seconds`` is not an integer from 5 to 300.
        """
        seconds = settings.get("listen_seconds", DEFAULT_LISTEN_SECONDS)
        if type(seconds) is not int or not MIN_LISTEN_SECONDS <= seconds <= MAX_LISTEN_SECONDS:
            raise ValueError("[mndp] listen_seconds must be an integer from 5 to 300")
        heard: dict[str, dict[str, Any]] = {}
        interfaces: dict[str, dict[str, str | None]] = {}
        for packet in self.listen(seconds):
            fields = decode_packet(packet)
            identity = fields.get("identity")
            if not identity:
                continue
            key = fields.get("software_id") or f"{identity}|{fields.get('board', '')}"
            remote_id = f"mndp:{key}"
            heard[remote_id] = {**heard.get(remote_id, {}), **fields}
            if fields.get("interface"):
                interfaces.setdefault(remote_id, {})[fields["interface"]] = fields.get("mac")
        devices = []
        ports = []
        for remote_id, fields in sorted(heard.items()):
            devices.append(DeviceObservation(
                remote_id=remote_id,
                name=fields["identity"],
                category=category_for_board(fields.get("board")),
                identifiers={key: fields[key] for key in ("mac", "software_id") if fields.get(key)},
                details={
                    "platform": "mndp", "vendor": "MikroTik",
                    "model": fields.get("board"),
                    "version": fields.get("version"),
                    "uptime_hours": (fields["uptime_seconds"] // 3600
                                     if "uptime_seconds" in fields else None),
                    "ports_complete": False,
                },
            ))
            for name, mac in sorted(interfaces.get(remote_id, {}).items()):
                ports.append(PortObservation(device_remote_id=remote_id, remote_id=name, name=name, mac=mac))
        return DiscoveryBatch(devices=tuple(devices), ports=tuple(ports))
