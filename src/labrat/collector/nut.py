# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read UPS state from a Network UPS Tools (NUT) server, read-only.

The client speaks just enough of the NUT network protocol to enumerate UPS
units and their variables: ``LIST UPS``, ``LIST VAR <ups>``, and ``LOGOUT``.
It never logs in, sets variables, or sends instant commands (no shutdown,
test, or beeper control), so it cannot change anything on the server.
Responses are size- and time-limited.

Protocol reference: https://networkupstools.org/docs/developer-guide.chunked/net-protocol.html
"""

from collections.abc import Callable
import socket

#: The NUT network server's registered TCP port.
NUT_PORT = 3493

#: Seconds allowed for connecting and for each read.
NUT_TIMEOUT_SECONDS = 3.0

#: Largest response accepted for one command; NUT lists are small.
MAX_RESPONSE_BYTES = 256 * 1024


class NutError(Exception):
    """The server answered with an error, or not in the NUT protocol."""


def split_line(line: str) -> list[str]:
    """Split one protocol line into words, honouring NUT's quoting.

    Words are separated by spaces; a double-quoted word may contain spaces and
    backslash escapes (``\\"`` and ``\\\\``).

    Args:
        line: One response line, without its newline.

    Returns:
        list[str]: The words, unquoted and unescaped.

    Raises:
        NutError: A quoted word is not closed.
    """
    words: list[str] = []
    index = 0
    while index < len(line):
        if line[index] == " ":
            index += 1
            continue
        if line[index] == '"':
            index += 1
            word = []
            while True:
                if index >= len(line):
                    raise NutError("Unterminated quoted value")
                character = line[index]
                if character == "\\" and index + 1 < len(line):
                    word.append(line[index + 1])
                    index += 2
                    continue
                if character == '"':
                    index += 1
                    break
                word.append(character)
                index += 1
            words.append("".join(word))
            continue
        end = line.find(" ", index)
        end = len(line) if end == -1 else end
        words.append(line[index:end])
        index = end
    return words


class NutSession:
    """One read-only conversation with a NUT server."""

    def __init__(self, host: str, port: int = NUT_PORT, timeout: float = NUT_TIMEOUT_SECONDS,
                 connect: Callable[..., socket.socket] = socket.create_connection) -> None:
        """Connect to the server.

        Args:
            host: Server address.
            port: Server port.
            timeout: Seconds for connecting and each read.
            connect: Socket factory; tests may substitute one.

        Raises:
            OSError: The server cannot be reached.
        """
        self.sock = connect((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buffer = b""

    def __enter__(self) -> "NutSession":
        """Use the session as a context manager.

        Returns:
            NutSession: This session.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Say goodbye and close the connection.

        Args:
            *exc: The exception details from the ``with`` block, if any.
        """
        try:
            self.sock.sendall(b"LOGOUT\n")
        except OSError:
            pass
        self.sock.close()

    def _line(self, received: list[int]) -> str:
        """Read one response line, enforcing the size limit.

        Args:
            received: A one-item counter of bytes read for this response,
                updated in place.

        Returns:
            str: The line, without its line ending.

        Raises:
            NutError: When the server closes the connection mid-line or the
                response passes ``MAX_RESPONSE_BYTES``.
        """
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise NutError("Connection closed mid-response")
            received[0] += len(chunk)
            if received[0] > MAX_RESPONSE_BYTES:
                raise NutError("Response too large")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def list(self, query: str) -> list[list[str]]:
        """Run a ``LIST`` command and return the words of each item line.

        Args:
            query: The list to read, such as ``"UPS"`` or ``"VAR myups"``.

        Returns:
            list[list[str]]: Words of each line between ``BEGIN LIST`` and
                ``END LIST``.

        Raises:
            NutError: The server reported an error or broke the framing.
        """
        self.sock.sendall(f"LIST {query}\n".encode("utf-8"))
        received = [0]
        first = self._line(received)
        if first.startswith("ERR "):
            raise NutError(first[4:].strip() or "Server error")
        if first != f"BEGIN LIST {query}":
            raise NutError("Not a NUT response")
        items = []
        while True:
            line = self._line(received)
            if line == f"END LIST {query}":
                return items
            if line.startswith("ERR "):
                raise NutError(line[4:].strip() or "Server error")
            items.append(split_line(line))


def read_server(host: str, port: int = NUT_PORT, *,
                session: Callable[..., NutSession] = NutSession) -> dict[str, dict]:
    """Read every UPS a NUT server offers, with its variables.

    Args:
        host: Server address.
        port: Server port.
        session: Session factory; tests may substitute one.

    Returns:
        dict[str, dict]: Per UPS name, ``description`` and ``vars`` (variable
            name to string value). A UPS whose variables cannot be read is
            returned with ``error`` instead of ``vars``.

    Raises:
        OSError: The server cannot be reached.
        NutError: The server does not speak NUT or rejects ``LIST UPS``.
    """
    with session(host, port) as nut:
        units = {}
        for words in nut.list("UPS"):
            if len(words) >= 2 and words[0] == "UPS":
                units[words[1]] = {"description": words[2] if len(words) > 2 else ""}
        for name, unit in units.items():
            try:
                unit["vars"] = {
                    words[2]: words[3] for words in nut.list(f"VAR {name}")
                    if len(words) >= 4 and words[0] == "VAR" and words[1] == name
                }
            except NutError as exc:
                unit["error"] = str(exc)[:120]
        return units
