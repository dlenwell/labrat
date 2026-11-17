# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read physical NIC MAC addresses from a Proxmox node's system report.

Proxmox's ``/nodes/{node}/network`` API describes interface configuration and
can omit hardware addresses. ``GET /nodes/{node}/report`` (the API form of
``pvereport``, readable with ``Sys.Audit``) includes ``ip -details`` output for
every host interface, which carries each NIC's MAC and, for bond members, its
permanent address.

The report also contains host configuration that LabRat has no use for, such
as storage, cluster, and hosts files. Callers must parse it in memory with
:func:`interface_macs` and discard the text: never store, log, or display it.
"""

import re

#: Start of one interface block in ``ip link``/``ip address`` output, such as
#: ``2: eno1: <BROADCAST,...>`` or ``7: vlan10@bond0: <...>``.
_BLOCK_START = re.compile(r"^\d+:\s+([^:\s@]+)(?:@\S+)?:\s+<", re.MULTILINE)

#: Ethernet address line inside a block, with an optional permanent address.
_LINK_ETHER = re.compile(
    r"^\s+link/ether\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b"
    r"(?:.*?\bpermaddr\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b)?",
    re.IGNORECASE | re.MULTILINE,
)

#: Alternative interface names, which the kernel reports on separate lines.
_ALTNAME = re.compile(r"^\s+altname\s+(\S+)\s*$", re.MULTILINE)


def interface_macs(report: str, *, permanent: bool = True) -> dict[str, str]:
    """Map interface names to hardware MAC addresses from report text.

    The permanent address is preferred over the current one, because bond
    members share the bond's address while switches see each NIC's own
    address in LLDP. Alternative names map to the same address. A name that
    appears with conflicting addresses is left out rather than guessed.

    Args:
        report: Text returned by ``/nodes/{node}/report``.
        permanent: Prefer hardware identity for physical ports. False returns
            current addresses for exact host/bond client association instead.

    Returns:
        dict[str, str]: Lowercase ``aa:bb:cc:dd:ee:ff`` addresses by name.
    """
    found: dict[str, str] = {}
    conflicts: set[str] = set()
    starts = list(_BLOCK_START.finditer(report))
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(report)
        # A block ends at the next interface or at a blank line, so later
        # report sections cannot lend an address to the last interface.
        block = report[start.end():end].split("\n\n", 1)[0]
        ether = _LINK_ETHER.search(block)
        if ether is None:
            continue
        mac = ((ether.group(2) if permanent else None) or ether.group(1)).lower()
        for name in (start.group(1), *_ALTNAME.findall(block)):
            if found.get(name, mac) != mac:
                conflicts.add(name)
            found.setdefault(name, mac)
    return {name: mac for name, mac in found.items() if name not in conflicts}
