# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Pretend to be a few DHCP clients on the OPNsense test bed's private links.

The test bed (``scripts/opnsense_testbed.py``) gives the firewall two extra
NICs on QEMU ``dgram`` netdevs bound to loopback: each a private Ethernet
segment with no other member, reachable only from this machine. This
module exchanges raw Ethernet frames with them, one frame per UDP
datagram (the firewall's side on port P, this side on P + 10), so the firewall
hands out real leases and fills its ARP and NDP tables without any guest
image, tap device, or bridge.

Each :class:`Host` asks for a lease (DHCP discover, offer, request, ack)
and then, if it is ``active``, announces itself: an ARP request for the
firewall (the firewall learns the sender), an IPv6 neighbour
solicitation from a static address and its link-local one, and tagged
ARP on a VLAN. A host that is not active only holds a lease, which is how
"has a lease, not seen" is recorded. Replies the firewall needs (ARP and
neighbour solicitations for a host's own addresses) are answered while the
session runs.

Used by: scripts.opnsense_testbed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import random
import socket
import struct
import time

BROADCAST = b"\xff" * 6
ETH_IPV4, ETH_ARP, ETH_IPV6, ETH_VLAN = 0x0800, 0x0806, 0x86DD, 0x8100


@dataclass
class Host:
    """One pretend client on a private link."""

    mac: str
    hostname: str
    #: Announce itself after the lease (ARP, NDP); False holds a lease only.
    active: bool = True
    #: A static IPv6 address to announce (with its link-local one), if any.
    ipv6: str | None = None
    #: VLAN tag and static address to announce on that VLAN, if any.
    vlan: tuple[int, str] | None = None
    address: str | None = None
    router: str | None = None
    extra: list[str] = field(default_factory=list)

    @property
    def raw(self) -> bytes:
        """The MAC as six bytes.

        Returns:
            bytes: The MAC.
        """
        return bytes.fromhex(self.mac.replace(":", ""))

    @property
    def link_local(self) -> str:
        """The EUI-64 link-local address for this MAC.

        Returns:
            str: Such as ``fe80::ff:fe00:1001``.
        """
        raw = bytearray(self.raw)
        raw[0] ^= 0x02
        eui = bytes(raw[:3]) + b"\xff\xfe" + bytes(raw[3:])
        return str(ipaddress.IPv6Address(b"\xfe\x80" + b"\x00" * 6 + eui))


def _checksum(data: bytes) -> int:
    """The Internet checksum of some bytes.

    Args:
        data: The bytes.

    Returns:
        int: The ones' complement sum.
    """
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ethernet(destination: bytes, source: bytes, kind: int, payload: bytes, vlan: int | None = None) -> bytes:
    """An Ethernet frame, 802.1Q-tagged when a VLAN is given.

    Args:
        destination: Destination MAC.
        source: Source MAC.
        kind: EtherType.
        payload: The payload.
        vlan: VLAN tag, or None.

    Returns:
        bytes: The frame.
    """
    tag = struct.pack("!HH", ETH_VLAN, vlan) if vlan else b""
    return destination + source + tag + struct.pack("!H", kind) + payload


def arp(operation: int, sender_mac: bytes, sender_ip: str, target_mac: bytes, target_ip: str) -> bytes:
    """An ARP packet for IPv4 over Ethernet.

    Args:
        operation: 1 request, 2 reply.
        sender_mac: Sender hardware address.
        sender_ip: Sender protocol address.
        target_mac: Target hardware address.
        target_ip: Target protocol address.

    Returns:
        bytes: The packet.
    """
    return struct.pack("!HHBBH", 1, ETH_IPV4, 6, 4, operation) + sender_mac + socket.inet_aton(sender_ip) \
        + target_mac + socket.inet_aton(target_ip)


def ipv4_udp(source: str, destination: str, sport: int, dport: int, payload: bytes) -> bytes:
    """An IPv4 UDP datagram (UDP checksum left zero, which IPv4 allows).

    Args:
        source: Source address.
        destination: Destination address.
        sport: Source port.
        dport: Destination port.
        payload: The payload.

    Returns:
        bytes: The IPv4 packet.
    """
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), random.getrandbits(16), 0, 64, 17, 0,
                         socket.inet_aton(source), socket.inet_aton(destination))
    header = header[:10] + struct.pack("!H", _checksum(header)) + header[12:]
    return header + udp


def icmpv6(source: str, destination: str, message: bytes) -> bytes:
    """An IPv6 packet carrying an ICMPv6 message, with its checksum filled in.

    Args:
        source: Source address.
        destination: Destination address.
        message: The ICMPv6 message with a zero checksum.

    Returns:
        bytes: The IPv6 packet.
    """
    src, dst = ipaddress.IPv6Address(source).packed, ipaddress.IPv6Address(destination).packed
    pseudo = src + dst + struct.pack("!I3xB", len(message), 58)
    message = message[:2] + struct.pack("!H", _checksum(pseudo + message)) + message[4:]
    return struct.pack("!IHBB", 0x60000000, len(message), 58, 255) + src + dst + message


def solicitation(host: Host, source: str, target: str) -> bytes:
    """A neighbour solicitation for ``target`` with the host's link-layer address.

    Args:
        host: The soliciting host.
        source: Its source address.
        target: The address solicited.

    Returns:
        bytes: The Ethernet frame (to the solicited-node multicast group).
    """
    packed = ipaddress.IPv6Address(target).packed
    group = ipaddress.IPv6Address(b"\xff\x02" + b"\x00" * 9 + b"\x01\xff" + packed[13:])
    message = struct.pack("!BBHI", 135, 0, 0, 0) + packed + struct.pack("!BB", 1, 1) + host.raw
    frame_mac = b"\x33\x33" + group.packed[12:]
    return ethernet(frame_mac, host.raw, ETH_IPV6, icmpv6(source, str(group), message))


def advertisement(host: Host, source: str, destination: str, destination_mac: bytes) -> bytes:
    """A solicited neighbour advertisement for one of the host's addresses.

    Args:
        host: The advertising host.
        source: The address advertised.
        destination: The solicitor's address.
        destination_mac: The solicitor's MAC.

    Returns:
        bytes: The Ethernet frame.
    """
    message = struct.pack("!BBHI", 136, 0, 0, 0x60000000) + ipaddress.IPv6Address(source).packed \
        + struct.pack("!BB", 2, 1) + host.raw
    return ethernet(destination_mac, host.raw, ETH_IPV6, icmpv6(source, destination, message))


def _advertises(frame: bytes, target: str) -> bool:
    """Whether a frame is a neighbour advertisement for an address.

    Args:
        frame: An untagged Ethernet frame.
        target: The address.

    Returns:
        bool: True for an advertisement of ``target``.
    """
    payload = frame[14:]
    return (frame[12:14] == struct.pack("!H", ETH_IPV6) and len(payload) >= 64 and payload[6] == 58
            and payload[40] == 136 and str(ipaddress.IPv6Address(payload[48:64])) == target)


def _echo_reply(frame: bytes) -> bool:
    """Whether a frame is an ICMPv6 echo reply.

    Args:
        frame: An untagged Ethernet frame.

    Returns:
        bool: True for an echo reply.
    """
    payload = frame[14:]
    return frame[12:14] == struct.pack("!H", ETH_IPV6) and len(payload) > 40 and payload[6] == 58 and payload[40] == 129


def dhcp(host: Host, kind: int, xid: int, requested: str | None = None, server: str | None = None) -> bytes:
    """A DHCP discover (1) or request (3), broadcast from 0.0.0.0.

    Args:
        host: The client.
        kind: The DHCP message type.
        xid: The transaction ID.
        requested: The offered address being requested.
        server: The offering server's identifier.

    Returns:
        bytes: The Ethernet frame.
    """
    body = struct.pack("!BBBBIHH4s4s4s4s", 1, 1, 6, 0, xid, 0, 0x8000, b"\0" * 4, b"\0" * 4, b"\0" * 4, b"\0" * 4)
    body += host.raw + b"\0" * 10 + b"\0" * 192 + b"\x63\x82\x53\x63"
    options = bytes([53, 1, kind, 61, 7, 1]) + host.raw
    name = host.hostname.encode()
    options += bytes([12, len(name)]) + name
    if requested:
        options += bytes([50, 4]) + socket.inet_aton(requested)
    if server:
        options += bytes([54, 4]) + socket.inet_aton(server)
    options += bytes([55, 3, 1, 3, 6, 255])
    return ethernet(BROADCAST, host.raw, ETH_IPV4, ipv4_udp("0.0.0.0", "255.255.255.255", 68, 67, body + options))


def _dhcp_reply(frame: bytes, xid: int) -> tuple[int, str, dict[int, bytes]] | None:
    """Decode a DHCP reply for one transaction.

    Args:
        frame: An untagged Ethernet frame.
        xid: The transaction ID expected.

    Returns:
        tuple | None: The message type, the offered address, and the
            options; None when the frame is not that reply.
    """
    if len(frame) < 14 + 20 + 8 + 240 or struct.unpack("!H", frame[12:14])[0] != ETH_IPV4:
        return None
    ip = frame[14:]
    length = (ip[0] & 0x0F) * 4
    if ip[9] != 17 or struct.unpack("!H", ip[length + 2:length + 4])[0] != 68:
        return None
    body = ip[length + 8:]
    if struct.unpack("!I", body[4:8])[0] != xid or body[236:240] != b"\x63\x82\x53\x63":
        return None
    options, index = {}, 240
    while index < len(body) and body[index] != 255:
        if body[index] == 0:
            index += 1
            continue
        options[body[index]] = body[index + 2:index + 2 + body[index + 1]]
        index += 2 + body[index + 1]
    kind = options.get(53, b"\0")[0]
    return kind, socket.inet_ntoa(body[16:20]), options


class Link:
    """One private Ethernet segment: a QEMU ``dgram`` netdev on loopback."""

    def __init__(self, port: int, hosts: list[Host]) -> None:
        """Bind this side of the segment.

        Args:
            port: The loopback port of the firewall's side.
            hosts: The pretend hosts on it.
        """
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", port + 10))
        self.socket.settimeout(0.2)
        self.peer = ("127.0.0.1", port)
        self.hosts = hosts

    def send(self, frame: bytes) -> None:
        """Send one frame.

        Args:
            frame: The Ethernet frame.
        """
        self.socket.sendto(frame, self.peer)

    def frames(self, seconds: float):
        """Yield frames the firewall sends, answering ARP and NDP meanwhile.

        Args:
            seconds: How long to listen.

        Yields:
            bytes: Each untagged frame (tagged ones are answered, not yielded).
        """
        end = time.time() + seconds
        while time.time() < end:
            try:
                frame, peer = self.socket.recvfrom(65536)
            except socket.timeout:
                continue
            if peer == self.peer and len(frame) >= 14 and not self._answer(frame):
                yield frame

    def _answer(self, frame: bytes) -> bool:
        """Answer an ARP request or neighbour solicitation for a host's address.

        Args:
            frame: A received frame.

        Returns:
            bool: Whether the frame was tagged or answered here.
        """
        kind, vlan, payload = struct.unpack("!H", frame[12:14])[0], None, frame[14:]
        if kind == ETH_VLAN:
            vlan = struct.unpack("!H", frame[14:16])[0] & 0x0FFF
            kind, payload = struct.unpack("!H", frame[16:18])[0], frame[18:]
        if kind == ETH_ARP and len(payload) >= 28 and struct.unpack("!H", payload[6:8])[0] == 1:
            target = socket.inet_ntoa(payload[24:28])
            for host in self.hosts:
                owned = {host.address, *(host.extra)} if vlan is None else {host.vlan[1] if host.vlan else None}
                if host.active and target in owned:
                    reply = arp(2, host.raw, target, payload[8:14], socket.inet_ntoa(payload[14:18]))
                    self.send(ethernet(payload[8:14], host.raw, ETH_ARP, reply, vlan))
            return True
        if kind == ETH_IPV6 and len(payload) >= 64 and payload[6] == 58 and payload[40] == 135:
            target = str(ipaddress.IPv6Address(payload[48:64]))
            source = str(ipaddress.IPv6Address(payload[8:24]))
            for host in self.hosts:
                if host.active and host.ipv6 and target in (host.ipv6, host.link_local) and source != "::":
                    self.send(advertisement(host, target, source, frame[6:12]))
            return True
        return vlan is not None

    def lease(self, host: Host) -> None:
        """Ask for a lease and wait for it.

        Args:
            host: The client.

        Raises:
            TimeoutError: No lease was offered.
        """
        for _ in range(5):
            xid = random.getrandbits(32)
            self.send(dhcp(host, 1, xid))
            offer = next((reply for frame in self.frames(4) if (reply := _dhcp_reply(frame, xid))
                          and reply[0] == 2), None)
            if offer is None:
                continue
            server = socket.inet_ntoa(offer[2][54]) if 54 in offer[2] else None
            self.send(dhcp(host, 3, xid, offer[1], server))
            ack = next((reply for frame in self.frames(4) if (reply := _dhcp_reply(frame, xid))
                        and reply[0] in (5, 6)), None)
            if ack and ack[0] == 5:
                host.address = ack[1]
                host.router = socket.inet_ntoa(ack[2][3][:4]) if 3 in ack[2] else server
                return
        raise TimeoutError(f"no lease for {host.hostname}")

    def announce(self, host: Host) -> None:
        """Make the firewall see an active host: ARP, and NDP if it has IPv6.

        Args:
            host: A host holding a lease.

        Raises:
            TimeoutError: The firewall did not answer the host's IPv6.
        """
        if not host.active:
            return
        if host.address and host.router:
            self.send(ethernet(BROADCAST, host.raw, ETH_ARP, arp(1, host.raw, host.address, b"\0" * 6, host.router)))
        if host.ipv6:
            # Ask for the router's MAC, then ping it from each address: the
            # firewall solicits the sender to answer, and keeps the entry.
            router = str(ipaddress.IPv6Network(f"{host.ipv6}/64", strict=False).network_address + 1)
            router_mac = None
            for _ in range(3):
                self.send(solicitation(host, host.ipv6, router))
                router_mac = next((frame[6:12] for frame in self.frames(3) if _advertises(frame, router)), None)
                if router_mac:
                    break
            if router_mac is None:
                raise TimeoutError(f"no neighbour advertisement for {host.hostname}")
            for source in (host.ipv6, host.link_local):
                message = struct.pack("!BBHHH", 128, 0, 0, 0x4C52, 1) + b"labrat"
                self.send(ethernet(router_mac, host.raw, ETH_IPV6, icmpv6(source, router, message)))
                if not any(_echo_reply(frame) for frame in self.frames(3)):
                    raise TimeoutError(f"no echo reply to {host.hostname} from {source}")
        if host.vlan:
            tag, address = host.vlan
            gateway = str(ipaddress.IPv4Network(f"{address}/26", strict=False).network_address + 1)
            self.send(ethernet(BROADCAST, host.raw, ETH_ARP, arp(1, host.raw, address, b"\0" * 6, gateway), tag))


def run(links: dict[int, list[Host]], linger: float = 20) -> list[Host]:
    """Lease and announce every host, then keep answering for a while.

    Args:
        links: Hosts by the loopback port of their link.
        linger: Seconds to keep answering ARP and NDP afterwards.

    Returns:
        list[Host]: The hosts, with their leased addresses.
    """
    opened = {port: Link(port, hosts) for port, hosts in links.items()}
    for link in opened.values():
        try:
            for host in link.hosts:
                link.lease(host)
        except TimeoutError:
            # This link's DHCP server is not the one serving: skip its hosts.
            link.hosts = []
        for host in link.hosts:
            link.announce(host)
    end = time.time() + linger
    while time.time() < end:
        for link in opened.values():
            for _ in link.frames(0.5):
                pass
    for link in opened.values():
        link.socket.close()
    return [host for link in opened.values() for host in link.hosts]
