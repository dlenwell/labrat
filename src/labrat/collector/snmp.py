# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read physical interface identity and state from SNMPv3 agents."""

import asyncio
import ipaddress
from collections.abc import Mapping
from typing import Any

from pysnmp.hlapi.v3arch.asyncio import (
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    USM_AUTH_HMAC128_SHA224,
    USM_AUTH_HMAC192_SHA256,
    USM_AUTH_HMAC256_SHA384,
    USM_AUTH_HMAC384_SHA512,
    USM_AUTH_HMAC96_SHA,
    USM_PRIV_CFB128_AES,
    USM_PRIV_CFB192_AES,
    USM_PRIV_CFB192_AES_BLUMENTHAL,
    USM_PRIV_CFB256_AES,
    USM_PRIV_CFB256_AES_BLUMENTHAL,
    UdpTransportTarget,
    UsmUserData,
    walk_cmd,
)

_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
_IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"
_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"

#: SNMPv3 authentication protocols by setting value, named as Net-SNMP's
#: ``createUser`` names them. The default, ``sha``, is SHA-1.
AUTH_PROTOCOLS = {
    "sha": USM_AUTH_HMAC96_SHA,
    "sha224": USM_AUTH_HMAC128_SHA224,
    "sha256": USM_AUTH_HMAC192_SHA256,
    "sha384": USM_AUTH_HMAC256_SHA384,
    "sha512": USM_AUTH_HMAC384_SHA512,
}

#: SNMPv3 privacy protocols by setting value, named as Net-SNMP names them.
#: ``aes`` is AES-128. ``aes192``/``aes256`` use Net-SNMP's key extension;
#: ``aes192c``/``aes256c`` use the one many network vendors use.
PRIV_PROTOCOLS = {
    "aes": USM_PRIV_CFB128_AES,
    "aes192": USM_PRIV_CFB192_AES_BLUMENTHAL,
    "aes256": USM_PRIV_CFB256_AES_BLUMENTHAL,
    "aes192c": USM_PRIV_CFB192_AES,
    "aes256c": USM_PRIV_CFB256_AES,
}

#: Protocol words an agent's refusal may carry, and the setting each points
#: at. Only these are ever named in an error.
_INDICATION_HINTS = {
    "wrongDigest": "check auth_protocol and the authentication passphrase",
    "decryptionError": "check priv_protocol and the privacy passphrase",
    "unknownUserName": "check the SNMPv3 username",
    "unsupportedSecLevel": "the agent's user must allow authPriv",
}


def snmp_options(settings: Mapping[str, Any]) -> tuple[Any, Any, int]:
    """Validate the optional protocol and port settings.

    Args:
        settings: SNMP options: ``auth_protocol`` (default ``sha``),
            ``priv_protocol`` (default ``aes``), and ``port`` (default 161).

    Returns:
        tuple: The pysnmp authentication and privacy protocols and the UDP
            port.

    Raises:
        ValueError: A protocol name is unknown or the port is out of range.
    """
    auth = settings.get("auth_protocol", "sha")
    priv = settings.get("priv_protocol", "aes")
    port = settings.get("port", 161)
    if auth not in AUTH_PROTOCOLS:
        raise ValueError(f"SNMP auth_protocol must be one of {', '.join(AUTH_PROTOCOLS)}")
    if priv not in PRIV_PROTOCOLS:
        raise ValueError(f"SNMP priv_protocol must be one of {', '.join(PRIV_PROTOCOLS)}")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SNMP port must be a whole number from 1 to 65535")
    return AUTH_PROTOCOLS[auth], PRIV_PROTOCOLS[priv], port


def _required_setting(settings: Mapping[str, Any], key: str) -> str:
    """Return one required, nonempty credential setting.

    The error includes only the setting's key. Credential values are never
    interpolated into exceptions or logs.

    Args:
        settings: SNMP options loaded from private local configuration.
        key: Name of the required string option.

    Returns:
        str: The trimmed setting value.

    Raises:
        ValueError: The setting is absent, not a string, or empty.
    """
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SNMP setting {key} must be a nonempty string")
    return value.strip()


async def _walk_column(
    engine: SnmpEngine,
    auth: UsmUserData,
    target: UdpTransportTarget,
    oid: str,
) -> dict[int, Any]:
    """Read one IF-MIB table column and index its rows by interface index.

    Args:
        engine: SNMP engine shared by this agent's table walks.
        auth: SNMPv3 USM authentication and privacy credentials.
        target: UDP endpoint of the SNMP agent.
        oid: Numeric object identifier for the selected table column.

    Returns:
        dict[int, Any]: Interface index mapped to its reported value.

    Raises:
        RuntimeError: The agent reports an error or does not respond.
    """
    results: dict[int, Any] = {}
    root_oid = tuple(int(part) for part in oid.split("."))
    async for indication, status, index, var_binds in walk_cmd(
        engine, auth, target, ContextData(),
        ObjectType(ObjectIdentity(oid)), lexicographicMode=False,
    ):
        if indication:
            # Raw indications can carry endpoint details, so only a known
            # protocol word is named, with the setting it points at.
            word = next((key for key in _INDICATION_HINTS if key in str(indication)), None)
            raise RuntimeError(f"SNMP request failed ({word}: {_INDICATION_HINTS[word]})" if word
                               else "SNMP request failed")
        if status:
            raise RuntimeError("SNMP agent returned an error")
        for object_type in var_binds:
            name, value = object_type
            object_oid = tuple(int(part) for part in name)
            if len(object_oid) <= len(root_oid):
                continue
            try:
                results[object_oid[-1]] = value
            except (TypeError, ValueError):
                continue
    return results


async def _read_interfaces(address: str, settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read standard interface identity and state from one SNMP agent.

    Args:
        address: IP address of the SNMP agent.
        settings: Username, SNMPv3 authPriv passphrases, and optional
            protocols and port (``snmp_options``).

    Returns:
        dict[str, dict[str, Any]]: Interface names mapped to MAC and link state.
    """
    auth_protocol, priv_protocol, port = snmp_options(settings)
    engine = SnmpEngine()
    auth = UsmUserData(
        _required_setting(settings, "username"),
        _required_setting(settings, "auth_passphrase"),
        _required_setting(settings, "priv_passphrase"),
        authProtocol=auth_protocol,
        privProtocol=priv_protocol,
    )
    target = await UdpTransportTarget.create((address, port), timeout=2, retries=1)
    try:
        names = await _walk_column(engine, auth, target, _IF_NAME)
        descriptions = await _walk_column(engine, auth, target, _IF_DESCR)
        macs = await _walk_column(engine, auth, target, _IF_PHYS_ADDRESS)
        states = await _walk_column(engine, auth, target, _IF_OPER_STATUS)
        interfaces: dict[str, dict[str, Any]] = {}
        for index in set(names) | set(descriptions):
            raw_name = names.get(index) or descriptions.get(index)
            name = raw_name.prettyPrint().strip() if hasattr(raw_name, "prettyPrint") else str(raw_name or "").strip()
            if not name:
                continue
            raw_mac = macs.get(index)
            mac = None
            if raw_mac is not None:
                mac_bytes = bytes(raw_mac)
                if len(mac_bytes) == 6 and any(mac_bytes):
                    mac = ":".join(f"{part:02x}" for part in mac_bytes)
            try:
                # IF-MIB defines 1 as up and 2 as down. Testing, unknown,
                # dormant, and not-present states are not explicit link state.
                status = int(states[index])
                link_up = True if status == 1 else False if status == 2 else None
            except (KeyError, TypeError, ValueError):
                link_up = None
            interfaces[name] = {"mac": mac, "link_up": link_up}
        return interfaces
    finally:
        engine.close_dispatcher()


def read_physical_interfaces(address: str, settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read IF-MIB interface identity over SNMPv3 authPriv.

    The protocols default to SHA authentication and AES-128 privacy, the
    Net-SNMP ``createUser USER SHA ... AES ...`` configuration; set
    ``auth_protocol``, ``priv_protocol``, and ``port`` to match other agents.

    Args:
        address: Hostname or IP address of the SNMP agent.
        settings: SNMP username and authentication/privacy passphrases.

    Returns:
        dict: Interface name mapped to its MAC address and link state.
    """
    normalized_address = str(ipaddress.ip_address(address))
    return asyncio.run(_read_interfaces(normalized_address, settings))
