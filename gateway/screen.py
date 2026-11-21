"""Screening of response bytes for a loaded key, before any of them is relayed.

A unit is either one whole server-sent event or one whole non-streaming body.
The scan does not depend on a JSON parser. A unit is flagged when a key
appears in:

1. the unit's raw bytes, or
2. the same bytes with JSON string escapes undone textually: `\\uXXXX`
   (surrogate pairs combined) and the two-character escapes `\\"`, `\\\\`,
   `\\/`, `\\b`, `\\f`, `\\n`, `\\r`, `\\t`.

For an SSE event, both scans also run on the event's `data:` lines joined
with newlines (as an SSE client joins them), after stripping one leading
UTF-8 byte order mark. The bytes on the wire are never changed.

The response head is screened for literal keys in the status reason, header
names and header values.
"""

from __future__ import annotations

import re

LINE_BREAK = re.compile(rb"\r\n|\r|\n")
BOM = b"\xef\xbb\xbf"
# A surrogate pair is matched explicitly, before a single \uXXXX.
ESCAPE = re.compile(
    rb"\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    rb"|\\u([0-9a-fA-F]{4})"
    rb'|\\(["\\/bfnrt])'
)
SIMPLE = {b'"': b'"', b"\\": b"\\", b"/": b"/", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t"}
REPLACEMENT = "�".encode()


def unescape(data: bytes) -> bytes:
    """Undo JSON string escapes textually, anywhere in the bytes."""

    def sub(m: re.Match) -> bytes:
        if m.group(1) is not None:
            hi, lo = int(m.group(1), 16), int(m.group(2), 16)
            return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)).encode()
        if m.group(3) is not None:
            cp = int(m.group(3), 16)
            # A lone surrogate cannot be encoded; it never forms part of a key.
            return REPLACEMENT if 0xD800 <= cp <= 0xDFFF else chr(cp).encode()
        return SIMPLE[m.group(4)]

    return ESCAPE.sub(sub, data)


def sse_data(event: bytes, strip_bom: bool = True) -> bytes | None:
    """The joined `data:` payload of one event, or None if it has none.

    With strip_bom, a byte order mark at the very start is ignored, as an SSE
    client ignores one at the start of a stream. Screening strips it from
    every event, which can only make it look harder. Usage parsing passes
    strip_bom only for the first event of a stream, because a client does not
    ignore a mark anywhere else.
    """
    if strip_bom and event.startswith(BOM):
        event = event[len(BOM):]
    lines = []
    for line in LINE_BREAK.split(event):
        if line.startswith(b"data:"):
            value = line[5:]
            lines.append(value[1:] if value.startswith(b" ") else value)
        elif line == b"data":
            lines.append(b"")
    if not lines:
        return None
    payload = b"\n".join(lines)
    return payload[len(BOM):] if strip_bom and payload.startswith(BOM) else payload


class Screen:
    """Checks units against the loaded keys."""

    def __init__(self, secrets: list[str]):
        self.secrets = [s for s in secrets if s]
        self._bytes = [s.encode() for s in self.secrets]

    def _has(self, data: bytes) -> bool:
        return any(s in data for s in self._bytes)

    def _scan(self, data: bytes) -> bool:
        if self._has(data):
            return True
        return b"\\" in data and self._has(unescape(data))

    def text(self, value: str) -> bool:
        return any(s in value for s in self.secrets)

    def body(self, data: bytes) -> bool:
        """A whole non-streaming body."""
        return bool(self.secrets) and self._scan(data)

    def event(self, data: bytes) -> bool:
        """One whole server-sent event."""
        if not self.secrets:
            return False
        if self._scan(data):
            return True
        payload = sse_data(data)
        return payload is not None and self._scan(payload)

    def head(self, reason: str, headers: list[tuple[str, str]]) -> bool:
        if not self.secrets:
            return False
        if self.text(reason or ""):
            return True
        return any(self.text(n) or self.text(v) for n, v in headers)


class EventSplitter:
    """Splits a byte stream into whole server-sent events.

    An event ends at an empty line; lines end with CRLF, LF or CR. A trailing
    CR is held until the next byte shows whether an LF follows, or until
    `finish()` says the stream ended.
    """

    def __init__(self, max_event: int):
        self.max_event = max_event
        self.buf = bytearray()
        self._reset()

    def _reset(self) -> None:
        self._scan = 0  # where to resume looking for line breaks
        self._line_start = 0  # start of the line being read

    def feed(self, data: bytes) -> list[bytes]:
        self.buf.extend(data)
        return self._take(eof=False)

    def append(self, data: bytes) -> None:
        """Add bytes without extracting events; take them with next_event()."""
        self.buf.extend(data)

    def next_event(self) -> bytes | None:
        """Remove and return the next whole event, or None if there is none yet."""
        end = self._find_end(eof=False)
        if end is None:
            return None
        event = bytes(self.buf[:end])
        del self.buf[:end]
        self._reset()
        return event

    def finish(self) -> tuple[list[bytes], bytes]:
        """At end of stream: the remaining whole events, and an undispatched rest."""
        events = self._take(eof=True)
        rest = bytes(self.buf)
        self.buf.clear()
        self._reset()
        return events, rest

    def pending(self) -> int:
        return len(self.buf)

    def _take(self, eof: bool) -> list[bytes]:
        events = []
        while True:
            end = self._find_end(eof)
            if end is None:
                break
            events.append(bytes(self.buf[:end]))
            del self.buf[:end]
            self._reset()
        return events

    def _find_end(self, eof: bool) -> int | None:
        buf = self.buf
        for m in LINE_BREAK.finditer(buf, self._scan):
            if m.group() == b"\r" and m.end() == len(buf) and not eof:
                # Might be the first half of CRLF: wait for more bytes.
                self._scan = m.start()
                return None
            if m.start() == self._line_start:
                # An empty line: the event ends here.
                return m.end()
            self._line_start = m.end()
        self._scan = len(buf)
        return None
