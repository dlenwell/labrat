# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Run a disposable OPNsense firewall to develop and record fixtures against.

The test bed needs no sign-in and touches no real network:

- **Isolated.** QEMU (with KVM) boots the OPNsense nano image on a
  copy-on-write overlay. LAN and WAN use QEMU's user-mode network, a
  private NAT inside the QEMU process with no tap and no bridge; the web
  interface and API are forwarded to one loopback port. Two more NICs
  (``opt1``, ``opt2``) exchange frames as UDP datagrams on loopback with
  ``scripts/opnsense_lab_clients.py`` and nothing else, so nothing the
  firewall serves (DHCP, router advertisements) reaches a real network.
- **Configured at boot.** OPNsense's boot-time importer reads
  ``conf/config.xml`` from a small ISO: the release's
  ``config.xml.sample`` with the setup wizard removed, every LAN DHCP
  range and router advertisement removed, a dnsmasq range and reservation
  on ``opt1`` and a Kea subnet and reservation on ``opt2`` (the private
  links), a VLAN on ``opt1``'s NIC, and a ``labrat`` user in a group
  holding exactly :data:`PRIVILEGES`, with an API key generated here (its
  secret stored as a SHA-512 crypt hash, as OPNsense stores it). The
  importer only runs on a disk without a configuration, so each boot
  starts from a fresh overlay.
- **Clients.** ``clients`` leases and announces a few pretend hosts on the
  private links, so the ARP, NDP, and lease answers have rows. Hosts on a
  link whose DHCP server is not serving are skipped.

Steps (``DIR`` is a scratch directory; the image is the owner's download
of ``OPNsense-<release>-nano-amd64.img.bz2`` from ``pkg.opnsense.org``,
checked against the release's checksum file and unpacked)::

    python scripts/opnsense_testbed.py prepare --image IMG --dir DIR
    python scripts/opnsense_testbed.py prepare --image IMG --dir PEER --role peer
    python scripts/opnsense_testbed.py boot --dir DIR
    python scripts/opnsense_testbed.py boot --dir PEER --role peer --port 8463
    python scripts/opnsense_testbed.py firewall --dir DIR --peer-dir PEER
    python scripts/opnsense_testbed.py clients --dir DIR
    python scripts/opnsense_testbed.py record --dir DIR --out tests/fixtures/opnsense
    python scripts/opnsense_testbed.py timeline --dir DIR --peer-dir PEER --out tests/fixtures/opnsense/timeline
    python scripts/opnsense_testbed.py stop --dir DIR

``firewall`` sets up rules, interface groups, aliases, a DNS blocklist,
a WireGuard tunnel to the peer firewall (a second test bed, ``peer``, on a
loopback UDP link), a monitored gateway to the peer, and a CARP virtual
IP shared with it, with each firewall's administrator key
(``scripts/opnsense_lab_firewall.py``). ``timeline`` pauses and resumes
the peer and toggles CARP maintenance, recording each step (about ten
minutes: a handshake goes stale after five).

Only one DHCP server can serve at a time (dnsmasq holds the DHCP port on
every address), so Kea's leases come from a second pass: ``prepare
--dhcp kea``, ``boot``, ``clients``, then ``record --only
kea/leases4/search``.

``prepare`` reads ``config.xml.sample`` for ``--release`` from the
OPNsense core repository. The key and secret are written to
``DIR/api.env`` (mode 600) and never printed. ``record`` reads through the
adapter's own allowlisted client and writes sanitized answers:
documentation addresses (192.0.2.0/24, 198.51.100.0/24, 2001:db8::/32) and
made-up MACs (``02:00:00:00:00:NN``).
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

#: Every privilege the integration's setup guide lists (checked on 26.7).
PRIVILEGES = (
    "page-system-status", "page-system-login-logout", "page-status-interfaces", "page-diagnostics-netstat",
    "page-diagnostics-arptable", "page-diagnostics-ndptable", "page-diagnostics-routingtables", "page-status-carp",
    "page-system-gateways", "page-services-dnsforwarder", "page-dhcp-kea-v4", "page-services-unbound",
    "page-status-dnsoverview", "page-filter-api", "page-firewall-aliases", "page-interfaces-groups-edit",
    "page-wireguard-diagnostics", "page-status-openvpn", "page-status-ipsec", "page-status-services",
)

#: Paths recorded as fixtures, under ``/api/`` (each must be allowlisted).
RECORDED = (
    "core/system/status", "diagnostics/system/system_information", "diagnostics/system/system_time",
    "diagnostics/system/system_resources", "diagnostics/system/system_temperature",
    "diagnostics/system/system_disk", "diagnostics/system/system_swap",
    "interfaces/overview/interfaces_info", "diagnostics/interface/get_interface_statistics",
    "diagnostics/interface/search_arp", "diagnostics/interface/search_ndp", "diagnostics/interface/get_routes",
    "routes/gateway/status", "dnsmasq/leases/search", "dnsmasq/settings/get", "kea/leases4/search",
    "kea/dhcpv4/search_subnet", "kea/dhcpv4/search_reservation", "unbound/settings/get",
    "firewall/filter/search_rule", "firewall/alias/search_item", "firewall/group/search_item",
    "wireguard/service/show", "wireguard/service/status", "unbound/settings/search_dnsbl",
    "unbound/overview/is_block_list_enabled", "core/service/search", "diagnostics/interface/get_vip_status",
)

#: Loopback UDP ports of the link between the test bed and its peer
#: firewall (``prepare --role peer``): the test bed's side, then the peer's.
PEER_LINK = (8448, 8449)

#: The private links (``opt1``, ``opt2``) and a VLAN on ``opt1``'s NIC, on
#: documentation prefixes so recordings need no rewriting. Their NICs are
#: loopback UDP links with no other member but
#: ``scripts/opnsense_lab_clients.py``: dnsmasq serves DHCP on ``opt1``
#: and Kea on ``opt2``, and nowhere else.
PRIVATE_INTERFACES = """    <opt1>
      <enable>1</enable>
      <descr>DHCPLAB</descr>
      <if>em2</if>
      <ipaddr>203.0.113.1</ipaddr>
      <subnet>26</subnet>
      <ipaddrv6>2001:db8:1::1</ipaddrv6>
      <subnetv6>64</subnetv6>
    </opt1>
    <opt2>
      <enable>1</enable>
      <descr>KEALAB</descr>
      <if>em3</if>
      <ipaddr>203.0.113.65</ipaddr>
      <subnet>26</subnet>
    </opt2>
    <opt3>
      <enable>1</enable>
      <descr>LABVLAN</descr>
      <if>vlan01</if>
      <ipaddr>203.0.113.129</ipaddr>
      <subnet>26</subnet>
    </opt3>
    <opt4>
      <enable>1</enable>
      <descr>PEERLINK</descr>
      <if>em4</if>
      <ipaddr>203.0.113.193</ipaddr>
      <subnet>29</subnet>
    </opt4>
"""

#: The peer firewall's one extra interface: its side of the peer link,
#: where the WireGuard tunnel to the test bed runs.
PEER_INTERFACES = """    <opt1>
      <enable>1</enable>
      <descr>PEERLINK</descr>
      <if>em2</if>
      <ipaddr>203.0.113.194</ipaddr>
      <subnet>29</subnet>
    </opt1>
"""

#: The VLAN behind ``opt3``.
VLANS = """  <vlans>
    <vlan uuid="6f1c2a40-0000-4000-8000-000000000010">
      <if>em2</if>
      <tag>10</tag>
      <pcp>0</pcp>
      <proto/>
      <descr>Lab VLAN</descr>
      <vlanif>vlan01</vlanif>
    </vlan>
  </vlans>
"""

#: Let the private links reach the firewall (pings), so it resolves the
#: pretend clients' addresses itself and lists them in ARP and NDP.
FILTER = """  <filter>
    <rule>
      <type>pass</type>
      <interface>opt1,opt2,opt3,opt4</interface>
      <ipprotocol>inet46</ipprotocol>
      <statetype>keep state</statetype>
      <source>
        <any/>
      </source>
      <destination>
        <any/>
      </destination>
      <descr>Lab links</descr>
    </rule>
  </filter>
"""

#: dnsmasq: one range and one reservation, on ``opt1`` only.
DNSMASQ_DHCP = """    <dhcp_ranges uuid="6f1c2a40-0000-4000-8000-000000000001">
      <interface>opt1</interface>
      <start_addr>203.0.113.10</start_addr>
      <end_addr>203.0.113.20</end_addr>
      <domain>lab.example</domain>
    </dhcp_ranges>
    <hosts uuid="6f1c2a40-0000-4000-8000-000000000002">
      <host>nas</host>
      <domain>lab.example</domain>
      <ip>203.0.113.30</ip>
      <hwaddr>02:00:00:00:10:03</hwaddr>
      <descr>Reserved</descr>
    </hosts>
"""

#: Kea: one subnet with a pool and one reservation, on ``opt2`` only.
KEA = """    <Kea>
      <dhcp4 version="1.0.5">
        <general>
          <enabled>1</enabled>
          <interfaces>opt2</interfaces>
          <valid_lifetime>4000</valid_lifetime>
          <fwrules>1</fwrules>
          <dhcp_socket_type>raw</dhcp_socket_type>
        </general>
        <subnets>
          <subnet4 uuid="6f1c2a40-0000-4000-8000-000000000003">
            <subnet>203.0.113.64/26</subnet>
            <option_data_autocollect>1</option_data_autocollect>
            <pools>203.0.113.70-203.0.113.80</pools>
            <description>Kea lab</description>
          </subnet4>
        </subnets>
        <reservations>
          <reservation uuid="6f1c2a40-0000-4000-8000-000000000004">
            <subnet>6f1c2a40-0000-4000-8000-000000000003</subnet>
            <ip_address>203.0.113.90</ip_address>
            <hw_address>02:00:00:00:20:02</hw_address>
            <hostname>printer</hostname>
            <description>Reserved</description>
          </reservation>
        </reservations>
      </dhcp4>
    </Kea>
"""

SAMPLE_URL = "https://raw.githubusercontent.com/opnsense/core/{release}/src/etc/config.xml.sample"


def build_config(sample: str, key: str, secret_hash: str, password_hash: str,
                 admin: tuple[str, str] | None = None, dhcp: str = "dnsmasq", role: str = "main") -> str:
    """Turn the release's sample configuration into the test bed's.

    Args:
        sample: ``config.xml.sample`` for the release.
        key: The API key.
        secret_hash: The API secret as a SHA-512 crypt hash.
        password_hash: A crypt hash of a random, unused password.
        admin: The test bed's own administrator key and secret hash, for
            its setup steps (``boot``) and for diagnosing it; never used to
            record. None leaves the administrator without a key.
        dhcp: The DHCP server that serves: ``dnsmasq`` (on ``opt1``) or
            ``kea`` (on ``opt2``). Only one can: dnsmasq holds the DHCP
            port on every address, so Kea cannot bind beside it. Kea's
            subnet and reservation are configured either way.
        role: ``main`` (the test bed) or ``peer`` (a second firewall named
            ``peer``, with only LAN, WAN, and the peer link, no DHCP: the
            far end of the WireGuard tunnel).

    Returns:
        str: The configuration: no wizard; no DHCP or router advertisements
            on LAN or WAN; DHCP only on the private links; and the
            ``labrat`` user with its key.

    Raises:
        ValueError: The sample does not look as expected, or DHCP would be
            served anywhere but the private links.
    """
    config = sample.replace("  <trigger_initial_wizard/>\n", "")
    config = re.sub(r"\s*<dhcp_ranges>.*?</dhcp_ranges>", "", config, flags=re.S)
    config = config.replace("      <enable_ra>1</enable_ra>\n", "")
    if "<dhcp_ranges>" in config or "<enable_ra>1" in config or "    <timezone>" not in config:
        raise ValueError("unexpected config.xml.sample: DHCP could not be removed safely")
    # Name the NICs, so no assignment prompt stops the boot.
    config = config.replace("<if>mismatch1</if>", "<if>em1</if>").replace("<if>mismatch0</if>", "<if>em0</if>")
    if role == "peer":
        config = config.replace("<hostname>OPNsense</hostname>", "<hostname>peer</hostname>", 1)
        config = config.replace("  </interfaces>\n", PEER_INTERFACES + "  </interfaces>\n", 1)
        config = config.replace("  <filter>\n  </filter>\n", FILTER.replace("opt1,opt2,opt3,opt4", "opt1"), 1)
        if "<hostname>peer</hostname>" not in config or "<descr>Lab links</descr>" not in config:
            raise ValueError("unexpected config.xml.sample: the peer could not be configured")
        return _with_users(config, key, secret_hash, password_hash, admin)
    config = config.replace("  </interfaces>\n", PRIVATE_INTERFACES + "  </interfaces>\n" + VLANS, 1)
    if dhcp == "dnsmasq":
        config = config.replace("    <interface>lan</interface>\n    <dhcp>", "    <interface>lan,opt1</interface>\n    <dhcp>", 1)
        config = config.replace("  </dnsmasq>\n", DNSMASQ_DHCP + "  </dnsmasq>\n", 1)
    kea = KEA if dhcp == "kea" else KEA.replace("<enabled>1</enabled>", "<enabled>0</enabled>", 1)
    config = config.replace("  <OPNsense>\n", "  <OPNsense>\n" + kea, 1)
    config = config.replace("  <filter>\n  </filter>\n", FILTER, 1)
    ranges = re.findall(r"<dhcp_ranges[^>]*>\s*<interface>([^<]*)</interface>", config)
    interfaces = re.findall(r"<Kea>.*?<interfaces>([^<]*)</interfaces>", config, flags=re.S)
    if (ranges != (["opt1"] if dhcp == "dnsmasq" else []) or interfaces != ["opt2"] or "<if>em0</if>" not in config
            or "<vlanif>vlan01" not in config or "<descr>Lab links</descr>" not in config):
        raise ValueError("unexpected config.xml.sample: DHCP must be served on the private links only")
    return _with_users(config, key, secret_hash, password_hash, admin)


def _with_users(config: str, key: str, secret_hash: str, password_hash: str, admin: tuple[str, str] | None) -> str:
    """Add the ``labrat`` user and group, and the administrator's key.

    Args:
        config: The configuration so far.
        key: The API key.
        secret_hash: The API secret as a SHA-512 crypt hash.
        password_hash: A crypt hash of a random, unused password.
        admin: The administrator's key and secret hash, or None.

    Returns:
        str: The configuration with them.
    """
    if admin is not None:
        config = config.replace("      <uid>0</uid>\n", "      <uid>0</uid>\n      <apikeys>\n        <item>\n"
                                f"          <key>{admin[0]}</key>\n          <secret>{admin[1]}</secret>\n"
                                "        </item>\n      </apikeys>\n", 1)
    group = ("    <group>\n      <name>labrat-read</name>\n      <description>LabRat test bed</description>\n"
             "      <scope>user</scope>\n      <gid>2000</gid>\n      <member>2000</member>\n"
             + "".join(f"      <priv>{name}</priv>\n" for name in PRIVILEGES) + "    </group>\n")
    user = ("    <user>\n      <name>labrat</name>\n      <descr>LabRat test bed</descr>\n      <scope>user</scope>\n"
            f"      <password>{password_hash}</password>\n      <uid>2000</uid>\n"
            f"      <apikeys>\n        <item>\n          <key>{key}</key>\n"
            f"          <secret>{secret_hash}</secret>\n        </item>\n      </apikeys>\n    </user>\n"
            "    <nextuid>2001</nextuid>\n    <nextgid>2001</nextgid>\n")
    return config.replace("    <timezone>", group + user + "    <timezone>", 1)


def crypt_sha512(value: str) -> str:
    """Hash a value with SHA-512 crypt (``$6$``), as OPNsense stores secrets.

    Args:
        value: The secret.

    Returns:
        str: The crypt hash.
    """
    result = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=value, capture_output=True, text=True,
                            check=True)
    return result.stdout.strip()


def prepare(image: Path, directory: Path, release: str, dhcp: str = "dnsmasq", role: str = "main") -> None:
    """Create the overlay, the configuration ISO, and the key file.

    Args:
        image: The unpacked nano image.
        directory: The test bed's directory.
        release: The OPNsense release (a core repository tag).
        dhcp: The DHCP server that serves (see :func:`build_config`).
        role: ``main`` or ``peer`` (see :func:`build_config`).
    """
    directory.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SAMPLE_URL.format(release=release), timeout=30) as response:  # noqa: S310 (fixed https URL)
        sample = response.read().decode("utf-8")
    key = base64.b64encode(secrets.token_bytes(60)).decode()
    secret = base64.b64encode(secrets.token_bytes(60)).decode()
    admin_key = base64.b64encode(secrets.token_bytes(60)).decode()
    admin_secret = base64.b64encode(secrets.token_bytes(60)).decode()
    config = build_config(sample, key, crypt_sha512(secret), crypt_sha512(secrets.token_urlsafe(32)),
                          (admin_key, crypt_sha512(admin_secret)), dhcp, role)
    env = directory / "api.env"
    env.touch(mode=0o600)
    env.write_text(f"OPNSENSE_KEY={key}\nOPNSENSE_SECRET={secret}\n"
                   f"OPNSENSE_ADMIN_KEY={admin_key}\nOPNSENSE_ADMIN_SECRET={admin_secret}\n", encoding="utf-8")
    tree = directory / "iso" / "conf"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "config.xml").write_text(config, encoding="utf-8")
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-R", "-J", "-V", "OPNCONF", "-o",
                    str(directory / "config.iso"), str(directory / "iso")], check=True)
    overlay = directory / "overlay.qcow2"
    overlay.unlink(missing_ok=True)
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw", "-b", str(image.resolve()), str(overlay)],
                   check=True)
    print(f"Prepared {directory}: overlay, config.iso, and api.env (mode 600).")


def _console(directory: Path) -> socket.socket:
    """Connect to the firewall's serial console.

    Args:
        directory: The test bed's directory.

    Returns:
        socket.socket: The console connection.

    Raises:
        OSError: The console did not come up.
    """
    for _ in range(100):
        try:
            console = socket.socket(socket.AF_UNIX)
            console.connect(str(directory / "serial.sock"))
            console.settimeout(1)
            return console
        except OSError:
            time.sleep(0.2)
    raise OSError("the serial console did not come up")


def _wait_for(console: socket.socket, pattern: bytes, seconds: float) -> None:
    """Read the console until a prompt appears.

    Args:
        console: The console connection.
        pattern: The prompt's text.
        seconds: How long to wait.

    Raises:
        TimeoutError: The prompt did not appear.
    """
    seen, end = b"", time.time() + seconds
    while time.time() < end:
        try:
            seen += console.recv(4096)
        except socket.timeout:
            continue
        if pattern in seen:
            return
    raise TimeoutError(f"no {pattern.decode()!r} on the console")


def _private_link(name: str, port: int, mac: str, remote: int | None = None) -> list[str]:
    """QEMU arguments for one private link: a NIC on loopback UDP.

    The firewall's side listens on ``port``; the other side (the pretend
    clients of ``scripts/opnsense_lab_clients.py``, or the peer firewall)
    on ``remote``.

    Args:
        name: The netdev ID.
        port: The firewall side's loopback port.
        mac: The NIC's MAC.
        remote: The other side's port; ``port + 10`` by default.

    Returns:
        list[str]: The ``-netdev`` and ``-device`` arguments.
    """
    remote = port + 10 if remote is None else remote
    return ["-netdev", f"dgram,id={name},local.type=inet,local.host=127.0.0.1,local.port={port},"
                       f"remote.type=inet,remote.host=127.0.0.1,remote.port={remote}",
            "-device", f"e1000,netdev={name},mac={mac}"]


def boot(directory: Path, port: int, role: str = "main") -> None:
    """Boot the firewall, answer the importer, and wait for the API.

    Args:
        directory: The test bed's directory, after ``prepare``.
        port: The loopback port the web interface is forwarded to.
        role: ``main`` (the test bed: LAN, WAN, two private links, and the
            peer link) or ``peer`` (LAN, WAN, and the peer link).

    Raises:
        TimeoutError: The firewall did not finish booting.
    """
    for name in ("serial.sock", "monitor.sock", "serial.log"):
        (directory / name).unlink(missing_ok=True)
    peer = role == "peer"
    mac = "52:54:00:00:01:{:02x}" if peer else "52:54:00:00:00:{:02x}"
    links = [*_private_link("link", PEER_LINK[1], mac.format(3), PEER_LINK[0])] if peer else [
        # opt1 and opt2: private links, loopback UDP datagrams exchanged only
        # with scripts/opnsense_lab_clients.py (DHCP is served here). A
        # datagram link is up from boot, so Kea binds to it.
        *_private_link("opt1", port + 1, mac.format(3)), *_private_link("opt2", port + 2, mac.format(4)),
        # opt4: the link to the peer firewall (WireGuard runs over it).
        *_private_link("opt4", PEER_LINK[0], mac.format(5), PEER_LINK[1]),
    ]
    command = [
        "qemu-system-x86_64", "-enable-kvm", "-m", "2048", "-smp", "2",
        "-name", "opnsense-testbed-peer" if peer else "opnsense-testbed",
        "-drive", f"file={directory / 'overlay.qcow2'},if=virtio,format=qcow2", "-cdrom", str(directory / "config.iso"),
        # LAN: restricted (no outbound), its web interface on loopback only.
        "-netdev", f"user,id=lan,net=192.168.1.0/24,host=192.168.1.2,restrict=on,"
                   f"hostfwd=tcp:127.0.0.1:{port}-192.168.1.1:443",
        "-device", f"e1000,netdev=lan,mac={mac.format(1)}",
        # WAN: outbound NAT only.
        "-netdev", "user,id=wan,net=10.0.2.0/24", "-device", f"e1000,netdev=wan,mac={mac.format(2)}",
        *links,
        "-display", "none",
        "-chardev", f"socket,id=s0,path={directory / 'serial.sock'},server=on,wait=off,logfile={directory / 'serial.log'}",
        "-serial", "chardev:s0", "-monitor", f"unix:{directory / 'monitor.sock'},server=on,wait=off", "-daemonize",
    ]
    subprocess.run(command, check=True)
    console = _console(directory)
    _wait_for(console, b"start the configuration importer", 300)
    console.sendall(b" ")
    _wait_for(console, b"to import from", 60)
    time.sleep(10)  # The CD appears a few seconds after the prompt.
    console.sendall(b"cd0\r")
    _wait_for(console, b"Starting import", 60)
    console.close()
    _wait_for_api(directory, port, 900)
    # Kea writes its configuration only when reconfigured, never at boot, so
    # an imported configuration serves nothing until this (administrator key).
    if not peer:
        _call(directory, port, "kea/service/reconfigure", admin=True, post=True)
    print(f"OPNsense is up on https://127.0.0.1:{port} (loopback only).")


def _call(directory: Path, port: int, path: str, *, admin: bool = False, post: bool = False,
          body: dict | None = None) -> dict:
    """Call the test bed's API on loopback.

    Args:
        directory: The test bed's directory (for ``api.env``).
        port: The loopback port of the web interface.
        path: The path under ``/api/``.
        admin: Use the test bed's administrator key (setup and diagnosis
            only) instead of the ``labrat`` key.
        post: Send a POST (an action) instead of a GET.
        body: The POST's JSON body; empty by default.

    Returns:
        dict: The decoded answer.

    Raises:
        OSError: The API did not answer.
    """
    import ssl
    values = dict(line.split("=", 1) for line in (directory / "api.env").read_text(encoding="utf-8").split())
    prefix = "OPNSENSE_ADMIN_" if admin else "OPNSENSE_"
    token = base64.b64encode(f"{values[prefix + 'KEY']}:{values[prefix + 'SECRET']}".encode()).decode()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # The test bed's own self-signed certificate, on loopback.
    headers = {"Authorization": f"Basic {token}"}
    if post:
        headers["Content-Type"] = "application/json"  # OPNsense refuses it on a GET.
    data = json.dumps(body or {}).encode() if post else None
    request = urllib.request.Request(f"https://127.0.0.1:{port}/api/{path}", data=data,
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=60, context=context) as response:  # noqa: S310 (loopback)
        return json.loads(response.read() or b"{}")


def _wait_for_api(directory: Path, port: int, seconds: float) -> None:
    """Wait until the API answers the test bed's key.

    Args:
        directory: The test bed's directory (for ``api.env``).
        port: The loopback port of the web interface.
        seconds: How long to wait.

    Raises:
        TimeoutError: The API did not answer in time.
    """
    end = time.time() + seconds
    while time.time() < end:
        try:
            _call(directory, port, "core/system/status")
            return
        except (OSError, ValueError):
            time.sleep(5)
    raise TimeoutError("the API did not answer")


def stop(directory: Path) -> None:
    """Power the firewall off.

    Args:
        directory: The test bed's directory.
    """
    with socket.socket(socket.AF_UNIX) as monitor:
        monitor.connect(str(directory / "monitor.sock"))
        monitor.sendall(b"quit\n")
    print("Stopped.")


class Sanitizer:
    """Replace addresses with documentation ones, consistently."""

    def __init__(self) -> None:
        """Start with no replacements."""
        self.macs: dict[str, str] = {}

    def mac(self, match: re.Match) -> str:
        """A made-up MAC for each real one, in order of appearance.

        The pretend clients' MACs (``02:00:00:00:1x:xx`` and
        ``02:00:00:00:2x:xx``, already made up) stay as they are, so the
        reservations in the settings answers still match their leases.

        Args:
            match: A matched MAC address.

        Returns:
            str: ``02:00:00:00:00:NN``, or the address unchanged.
        """
        value = match.group(0).lower()
        if value == "00:00:00:00:00:00" or value.startswith("02:00:00:00:"):
            return value
        return self.macs.setdefault(value, f"02:00:00:00:00:{len(self.macs) + 1:02x}")

    @staticmethod
    def ipv4(match: re.Match) -> str:
        """A documentation address for a private one.

        Args:
            match: A matched IPv4 address.

        Returns:
            str: The address in 192.0.2.0/24 (the LAN) or 198.51.100.0/24
                (anything else private); documentation, public, and loopback
                addresses stay.
        """
        address = ipaddress.ip_address(match.group(0))
        documentation = any(address in ipaddress.ip_network(net)
                            for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
        if address.is_loopback or not address.is_private or documentation:
            return match.group(0)
        prefix = "192.0.2" if str(address).startswith("192.168.") else "198.51.100"
        return f"{prefix}.{str(address).rsplit('.', 1)[1]}"

    def clean(self, text: str) -> str:
        """Sanitize one recorded answer.

        Args:
            text: The answer as JSON text.

        Returns:
            str: The same JSON with documentation addresses.
        """
        # Not inside a longer colon string, such as a DHCP client ID.
        text = re.sub(r"(?<![0-9a-fA-F:])[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}(?![0-9a-fA-F:])", self.mac, text)
        text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.ipv4, text)
        text = text.replace("fec0::", "2001:db8::").replace("5054:ff:fe", "ff:fe")
        return text


def record(directory: Path, out: Path, port: int, only: list[str] | None = None) -> None:
    """Record sanitized answers through the adapter's allowlisted client.

    Args:
        directory: The test bed's directory.
        out: The fixtures directory.
        port: The loopback port of the web interface.
        only: Record just these paths (from :data:`RECORDED`), such as
            Kea's leases from a test bed prepared with ``--dhcp kea``.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import urllib3
    from labrat.collector.adapters.opnsense import OpnsenseClient
    urllib3.disable_warnings()  # The test bed's certificate is self-signed.
    values = dict(line.split("=", 1) for line in (directory / "api.env").read_text(encoding="utf-8").split())
    client = OpnsenseClient({"url": f"https://127.0.0.1:{port}", "api_key": values["OPNSENSE_KEY"],
                             "api_secret": values["OPNSENSE_SECRET"], "verify_tls": False})
    sanitizer = Sanitizer()
    out.mkdir(parents=True, exist_ok=True)
    paths = [path for path in RECORDED if not only or path in only]
    for path in paths:
        answer = json.dumps(client.get(path), indent=1, sort_keys=True)
        (out / f"{path.replace('/', '__')}.json").write_text(sanitizer.clean(answer) + "\n", encoding="utf-8")
    print(f"Recorded {len(paths)} answers in {out}.")


def clients(port: int) -> None:
    """Lease and announce the pretend clients on the private links.

    ``workstation`` is dual-stack and also on the VLAN; ``phone`` only holds
    a lease (never announced, so never in ARP); ``nas`` and ``printer`` get
    their reserved addresses.

    Args:
        port: The loopback port of the web interface (the links are the next two).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from opnsense_lab_clients import Host, run
    hosts = run({
        port + 1: [Host("02:00:00:00:10:01", "workstation", ipv6="2001:db8:1::a", vlan=(10, "203.0.113.140")),
                   Host("02:00:00:00:10:02", "phone", active=False), Host("02:00:00:00:10:03", "nas")],
        port + 2: [Host("02:00:00:00:20:01", "sensor"), Host("02:00:00:00:20:02", "printer")],
    })
    for host in hosts:
        print(f"{host.hostname}: {host.address} ({'announced' if host.active else 'lease only'})")


#: The answers the watchlist reads, recorded at each step of :func:`timeline`.
TIMELINE_PATHS = ("routes/gateway/status", "wireguard/service/show", "diagnostics/interface/get_vip_status")


def _monitor(directory: Path, command: str) -> None:
    """Send one command to a firewall's QEMU monitor.

    Args:
        directory: The firewall's directory.
        command: Such as ``stop`` (pause the VM) or ``cont`` (resume it).
    """
    with socket.socket(socket.AF_UNIX) as monitor:
        monitor.connect(str(directory / "monitor.sock"))
        monitor.sendall(command.encode() + b"\n")
        time.sleep(1)


def timeline(directory: Path, port: int, peer_directory: Path, peer_port: int, out: Path) -> None:
    """Record the watchlist's timeline: good, down, recovered, and a CARP change.

    The peer firewall is paused (QEMU ``stop``), so the test bed's monitored
    gateway to it goes down and the WireGuard handshake with it goes stale
    (keepalive peers rehandshake about every two minutes; stale is five
    minutes without one); it is then resumed. The test bed then enters and
    leaves CARP maintenance mode, so its virtual IP goes to backup and back.
    Each step's answers go to ``out/<step>/`` through the allowlisted client.

    Args:
        directory: The test bed's directory.
        port: Its loopback web port.
        peer_directory: The peer firewall's directory.
        peer_port: The peer's loopback web port.
        out: Where the steps go (such as ``tests/fixtures/opnsense/timeline``).
    """
    def answers() -> dict:
        return {path: _call(directory, port, path) for path in TIMELINE_PATHS}

    def gateway(found: dict) -> dict:
        return next((row for row in found["routes/gateway/status"].get("items") or [] if row.get("name") == "PEER_GW"), {})

    def peer(found: dict) -> dict:
        return next((row for row in found["wireguard/service/show"].get("rows") or [] if row.get("name") == "peer-firewall"), {})

    def vip(found: dict) -> str:
        return next((str(row.get("status")).lower() for row in found["diagnostics/interface/get_vip_status"].get("rows") or []
                     if row.get("mode") == "carp"), "")

    def until(test, what: str, seconds: float = 900) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if test(answers()):
                return
            time.sleep(10)
        raise TimeoutError(f"the test bed never showed {what}")

    def age(row: dict) -> float:
        return time.time() - (row.get("latest-handshake") or 0)

    until(lambda found: gateway(found).get("status") == "none" and gateway(found).get("delay") not in (None, "~")
          and age(peer(found)) < 150 and vip(found) == "master", "a good gateway, tunnel, and CARP master")
    record(directory, out / "good", port, list(TIMELINE_PATHS))
    _monitor(peer_directory, "stop")
    try:
        until(lambda found: gateway(found).get("status") == "down" and age(peer(found)) > 330,
              "the gateway down and the tunnel stale")
        record(directory, out / "down", port, list(TIMELINE_PATHS))
    finally:
        _monitor(peer_directory, "cont")
    until(lambda found: gateway(found).get("status") == "none" and age(peer(found)) < 150, "the recovery")
    record(directory, out / "recovered", port, list(TIMELINE_PATHS))
    _call(directory, port, "diagnostics/interface/carp_status/maintenance", admin=True, post=True)
    until(lambda found: vip(found) == "backup", "CARP backup", 120)
    record(directory, out / "carp_backup", port, list(TIMELINE_PATHS))
    _call(directory, port, "diagnostics/interface/carp_status/maintenance", admin=True, post=True)
    until(lambda found: vip(found) == "master", "CARP master again", 120)
    record(directory, out / "carp_master", port, list(TIMELINE_PATHS))
    print(f"Recorded the timeline in {out}.")


def firewall(directory: Path, port: int, peer_directory: Path, peer_port: int) -> None:
    """Set up rules, groups, aliases, the DNS blocklist, and WireGuard on both firewalls.

    Uses each firewall's administrator key (setup only); see
    ``scripts/opnsense_lab_firewall.py``.

    Args:
        directory: The test bed's directory.
        port: Its loopback web port.
        peer_directory: The peer firewall's directory.
        peer_port: The peer's loopback web port.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from opnsense_lab_firewall import configure

    def call(on_peer: bool):
        def send(path: str, body: dict | None = None) -> dict:
            target, web = (peer_directory, peer_port) if on_peer else (directory, port)
            return _call(target, web, path, admin=True, post=body is not None, body=body)
        return send

    print(configure(call(False), call(True)))


def main(argv: list[str] | None = None) -> int:
    """Run one test-bed command.

    Args:
        argv: Command-line arguments, without the program name.

    Returns:
        int: The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "boot", "clients", "firewall", "timeline", "record", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--dir", type=Path, required=True, help="the test bed's scratch directory")
        if name == "prepare":
            command.add_argument("--image", type=Path, required=True, help="the unpacked nano image")
            command.add_argument("--release", default="26.7", help="the OPNsense release")
            command.add_argument("--dhcp", choices=("dnsmasq", "kea"), default="dnsmasq",
                                 help="the DHCP server that serves (only one can)")
        if name in ("prepare", "boot"):
            command.add_argument("--role", choices=("main", "peer"), default="main",
                                 help="the test bed, or the peer firewall at the far end of its WireGuard tunnel")
        if name in ("firewall", "timeline"):
            command.add_argument("--peer-dir", type=Path, required=True, help="the peer firewall's directory")
            command.add_argument("--peer-port", type=int, default=8463, help="the peer's loopback web port")
        if name == "timeline":
            command.add_argument("--out", type=Path, required=True, help="where to write the timeline's steps")
        if name in ("boot", "clients", "firewall", "timeline", "record"):
            command.add_argument("--port", type=int, default=8443, help="the loopback port for the web interface")
        if name == "record":
            command.add_argument("--out", type=Path, required=True, help="where to write the fixtures")
            command.add_argument("--only", nargs="+", choices=RECORDED, help="record just these paths")
    args = parser.parse_args(argv)
    for tool in {"prepare": ("openssl", "xorriso", "qemu-img"), "boot": ("qemu-system-x86_64",)}.get(args.command, ()):
        if shutil.which(tool) is None:
            print(f"opnsense_testbed: {tool} is not installed", file=sys.stderr)
            return 1
    if args.command == "prepare":
        prepare(args.image, args.dir, args.release, args.dhcp, args.role)
    elif args.command == "boot":
        boot(args.dir, args.port, args.role)
    elif args.command == "clients":
        clients(args.port)
    elif args.command == "firewall":
        firewall(args.dir, args.port, args.peer_dir, args.peer_port)
    elif args.command == "timeline":
        timeline(args.dir, args.port, args.peer_dir, args.peer_port, args.out)
    elif args.command == "record":
        record(args.dir, args.out, args.port, args.only)
    else:
        stop(args.dir)
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
