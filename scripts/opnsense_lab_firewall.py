# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Set up the OPNsense test bed's firewall, DNS blocklist, and WireGuard tunnel.

Run by ``scripts/opnsense_testbed.py firewall`` once the test bed and its
peer firewall are up. It uses each firewall's own administrator key
through the supported API (the ``labrat`` key never changes anything), so
the fixtures show what OPNsense itself reports for:

- **Rules** in evaluation order's awkward cases: a floating rule, a
  non-``quick`` pass followed by a ``quick`` block of the same traffic, a
  rule on an interface group, and (from the imported configuration) one
  rule still in the old format.
- **Interface groups** that overlap: ``opt1`` is in both.
- **Aliases**: a host list, and a URL table behind a user name and a
  password (dummies), which LabRat must never keep.
- **DNS blocklist**: custom domains only, so nothing is downloaded.
- **WireGuard**: a tunnel to the peer over the peer link, with persistent
  keepalive on the test bed's side, and a second peer that never connects.
- **Watchlist**: a gateway to the peer (``PEER_GW``) that dpinger monitors,
  and a CARP virtual IP on the peer link, master on the test bed (advskew
  0) and backup on the peer (100), so ``timeline`` can take the gateway
  down and move CARP state.

Keys are generated here (X25519) and live only in the two firewalls.

Used by: scripts.opnsense_testbed.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

#: Calls one firewall's API: a GET without a body, a POST with one.
Call = Callable[..., dict]


def keypair() -> tuple[str, str]:
    """A WireGuard key pair.

    Returns:
        tuple[str, str]: The private and public keys, in base64.
    """
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode(), base64.b64encode(public).decode()


def _added(answer: dict, what: str) -> str:
    """The UUID of something an ``add_*`` call created.

    Args:
        answer: The API's answer.
        what: What was added, for the error.

    Returns:
        str: Its UUID.

    Raises:
        RuntimeError: OPNsense refused it.
    """
    if answer.get("result") != "saved":
        raise RuntimeError(f"OPNsense refused the {what}: {answer.get('validations') or answer}")
    return answer.get("uuid", "")


def _rules(call: Call) -> None:
    """Groups, aliases, and new-format rules on the test bed.

    Args:
        call: The test bed's API.
    """
    for name, members, description in (("labnets", "opt1,opt3", "Lab networks"),
                                       ("dhcpnets", "opt1,opt2", "Networks with DHCP")):
        _added(call("firewall/group/add_item", {"group": {"ifname": name, "members": members, "descr": description}}),
               f"group {name}")
    call("firewall/group/reconfigure", {})
    _added(call("firewall/alias/add_item", {"alias": {
        "enabled": "1", "name": "lab_servers", "type": "host", "content": "203.0.113.30\n203.0.113.90",
        "description": "Reserved lab hosts"}}), "host alias")
    _added(call("firewall/alias/add_item", {"alias": {
        "enabled": "1", "name": "remote_blocklist", "type": "urltable", "content": "https://127.0.0.1/blocked.txt",
        "updatefreq": "1", "username": "testbed-user", "password": "testbed-alias-password",
        "description": "A list behind a password (never kept by LabRat)"}}), "URL table alias")
    call("firewall/alias/reconfigure", {})
    base = {"enabled": "1", "direction": "in", "ipprotocol": "inet", "protocol": "any", "source_net": "any",
            "destination_net": "any", "log": "0"}
    for rule in (
        {"sequence": "10", "interface": "opt1,opt2", "action": "block", "quick": "1", "direction": "any",
         "destination_net": "203.0.113.250", "description": "Floating: block one bad host"},
        {"sequence": "100", "interface": "opt1", "action": "pass", "quick": "0", "source_net": "opt1",
         "description": "Lab: allow out (not quick)"},
        {"sequence": "200", "interface": "opt1", "action": "block", "quick": "1", "protocol": "TCP",
         "source_net": "opt1", "destination_net": "203.0.113.64/26", "destination_port": "22",
         "description": "Lab: no SSH to the Kea lab (quick)"},
        {"sequence": "300", "interface": "labnets", "action": "pass", "quick": "1", "protocol": "TCP",
         "destination_net": "lab_servers", "destination_port": "443", "description": "Group: HTTPS to lab servers"},
    ):
        _added(call("firewall/filter/add_rule", {"rule": {**base, **rule}}), f"rule {rule['description']}")
    call("firewall/filter/apply", {})


def _blocklist(call: Call) -> None:
    """A DNS blocklist of custom domains (no download).

    Args:
        call: The test bed's API.
    """
    _added(call("unbound/settings/add_dnsbl", {"blocklist": {
        "enabled": "1", "type": "", "blocklists": "ads.example,tracker.example", "wildcards": "telemetry.example",
        "description": "Lab blocklist"}}), "DNS blocklist")
    call("unbound/service/dnsbl", {})
    call("unbound/service/reconfigure", {})


def _wireguard(call: Call, private: str, public: str, peer_public: str, peer: dict) -> None:
    """One firewall's WireGuard instance and its peers.

    Args:
        call: That firewall's API.
        private: Its private key.
        public: Its public key.
        peer_public: The other firewall's public key.
        peer: The other firewall's peer settings (tunnel address, endpoint,
            keepalive), and ``tunnel``: this side's tunnel address.
    """
    peers = [_added(call("wireguard/client/add_client", {"client": {
        "enabled": "1", "name": peer["name"], "pubkey": peer_public, "tunneladdress": peer["allowed"],
        "serveraddress": peer["endpoint"], "serverport": "51820", "keepalive": peer["keepalive"]}}), "WireGuard peer")]
    if peer.get("idle"):
        # A road warrior that never connects: no endpoint, no keepalive.
        peers.append(_added(call("wireguard/client/add_client", {"client": {
            "enabled": "1", "name": "road-warrior", "pubkey": keypair()[1], "tunneladdress": "203.0.113.249/32",
            "serveraddress": "", "serverport": "", "keepalive": ""}}), "idle WireGuard peer"))
    _added(call("wireguard/server/add_server", {"server": {
        "enabled": "1", "name": "wg_lab", "privkey": private, "pubkey": public, "port": "51820",
        "tunneladdress": peer["tunnel"], "peers": ",".join(peers)}}), "WireGuard instance")
    call("wireguard/general/set", {"general": {"enabled": "1"}})
    call("wireguard/service/reconfigure", {})


def _watch(main: Call, peer: Call) -> None:
    """A monitored gateway to the peer, and a CARP virtual IP on both firewalls.

    Args:
        main: The test bed's API.
        peer: The peer firewall's API.
    """
    _added(main("routing/settings/add_gateway", {"gateway_item": {
        "disabled": "0", "name": "PEER_GW", "descr": "The peer firewall", "interface": "opt4", "ipprotocol": "inet",
        "gateway": "203.0.113.194", "monitor_disable": "0", "monitor": "203.0.113.194"}}), "gateway")
    main("routing/settings/reconfigure", {})
    for call, skew, link in ((main, "0", "opt4"), (peer, "100", "opt1")):
        _added(call("interfaces/vip_settings/add_item", {"vip": {
            "mode": "carp", "interface": link, "subnet": "203.0.113.198", "subnet_bits": "29", "vhid": "1",
            "advbase": "1", "advskew": skew, "password": "testbed-carp", "descr": "Lab CARP"}}), "CARP virtual IP")
        call("interfaces/vip_settings/reconfigure", {})


def configure(main: Call, peer: Call, seconds: float = 120) -> str:
    """Set everything up on both firewalls and wait for the tunnel's handshake.

    Args:
        main: The test bed's API.
        peer: The peer firewall's API.
        seconds: How long to wait for the handshake.

    Returns:
        str: What was set up.

    Raises:
        TimeoutError: The tunnel did not handshake in time.
    """
    _rules(main)
    _blocklist(main)
    (main_private, main_public), (peer_private, peer_public) = keypair(), keypair()
    _wireguard(peer, peer_private, peer_public, main_public, {
        "name": "testbed", "allowed": "203.0.113.245/32", "endpoint": "203.0.113.193", "keepalive": "",
        "tunnel": "203.0.113.246/30"})
    _wireguard(main, main_private, main_public, peer_public, {
        "name": "peer-firewall", "allowed": "203.0.113.246/32", "endpoint": "203.0.113.194", "keepalive": "25",
        "tunnel": "203.0.113.245/30", "idle": True})
    _watch(main, peer)
    end = time.time() + seconds
    while time.time() < end:
        rows = main("wireguard/service/show").get("rows") or []
        if any(row.get("name") == "peer-firewall" and str(row.get("latest-handshake") or "0") not in ("0", "")
               for row in rows):
            return ("Rules, groups, aliases, the DNS blocklist, a WireGuard tunnel (handshake seen), "
                    "a monitored gateway, and CARP.")
        time.sleep(5)
    raise TimeoutError("the WireGuard tunnel did not handshake")
