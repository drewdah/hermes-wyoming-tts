"""Minimal, dependency-free Wyoming TTS client.

Wyoming is a JSONL-framed TCP protocol: each event is one JSON header line
(``type``, ``data_length``, ``payload_length``), then ``data_length`` bytes of
JSON data, then ``payload_length`` bytes of binary payload. Older servers put
``data`` inline in the header; both forms are accepted here.

TTS exchange: client sends ``synthesize`` → server replies ``audio-start``
(rate/width/channels), zero or more ``audio-chunk`` (raw PCM payload), then
``audio-stop``. A server may send ``error`` instead.

This module knows nothing about Hermes so it can be tested and reused alone.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
from dataclasses import dataclass
from typing import Iterator, Optional


class WyomingError(RuntimeError):
    """Protocol, server, or transport failure."""


@dataclass(frozen=True)
class PcmFormat:
    rate: int
    width: int  # bytes per sample
    channels: int


def write_event(sock: socket.socket, etype: str, data: Optional[dict] = None, payload: bytes = b"") -> None:
    body = json.dumps(data or {}, separators=(",", ":")).encode("utf-8")
    header = {"type": etype, "data_length": len(body), "payload_length": len(payload)}
    sock.sendall(json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + body + payload)


def _read_exact(rfile, n: int) -> bytes:
    buf = rfile.read(n)
    if buf is None or len(buf) != n:
        raise WyomingError("connection closed mid-event")
    return buf


def read_event(rfile) -> tuple[str, dict, bytes]:
    line = rfile.readline()
    if not line:
        raise WyomingError("connection closed by server")
    try:
        header = json.loads(line)
    except ValueError as exc:
        raise WyomingError(f"bad event header: {line[:80]!r}") from exc
    data = dict(header.get("data") or {})
    if header.get("data_length"):
        data.update(json.loads(_read_exact(rfile, int(header["data_length"]))))
    payload = _read_exact(rfile, int(header["payload_length"])) if header.get("payload_length") else b""
    return str(header.get("type")), data, payload


def _format_from(data: dict) -> Optional[PcmFormat]:
    try:
        return PcmFormat(int(data["rate"]), int(data["width"]), int(data["channels"]))
    except (KeyError, TypeError, ValueError):
        return None


_END = object()


class Synthesis:
    """One ``synthesize`` request. Use as a context manager, then iterate for PCM payloads.

    A background thread drains the socket as fast as the server sends, so a slow consumer
    (e.g. one pacing audio at playback speed) never back-pressures the TTS server itself.
    ``format`` is known once the first ``audio-start``/``audio-chunk`` has arrived.
    """

    def __init__(self, host: str, port: int, text: str, *, voice: Optional[str] = None,
                 speaker: Optional[str] = None, language: Optional[str] = None,
                 connect_timeout: float = 2.0, read_timeout: float = 15.0) -> None:
        self.host, self.port, self.text = host, port, text
        self.voice, self.speaker, self.language = voice, speaker, language
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.format: Optional[PcmFormat] = None
        self._sock: Optional[socket.socket] = None
        self._events: "queue.Queue[object]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Synthesis":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        """Connect and send the request. Raises OSError/WyomingError; no audio has been produced yet."""
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(self.read_timeout)
        self._sock = sock
        voice = {k: v for k, v in (("name", self.voice), ("speaker", self.speaker),
                                   ("language", self.language)) if v}
        request: dict = {"text": self.text}
        if voice:
            request["voice"] = voice
        write_event(sock, "synthesize", request)
        self._thread = threading.Thread(target=self._reader, name="wyoming-tts-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        try:
            rfile = self._sock.makefile("rb")
            while True:
                etype, data, payload = read_event(rfile)
                if etype == "audio-start":
                    self._events.put(("format", _format_from(data)))
                elif etype == "audio-chunk":
                    fmt = _format_from(data)
                    if fmt:
                        self._events.put(("format", fmt))
                    if payload:
                        self._events.put(("pcm", payload))
                elif etype == "audio-stop":
                    self._events.put(_END)
                    return
                elif etype == "error":
                    raise WyomingError(f"server error: {data.get('text') or data}")
                # anything else (info, ping...) is ignored
        except Exception as exc:  # socket timeout, EOF, protocol error, or close() from the consumer
            self._events.put(exc)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            try:
                item = self._events.get(timeout=self.read_timeout + 1.0)
            except queue.Empty:
                raise WyomingError(f"no audio from server for {self.read_timeout:.0f}s") from None
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item if isinstance(item, WyomingError) else WyomingError(str(item) or type(item).__name__)
            kind, value = item
            if kind == "format":
                if value is not None and self.format is None:
                    self.format = value
                continue
            if self.format is None:
                raise WyomingError("audio-chunk before any audio format was announced")
            yield value

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def describe(host: str, port: int, timeout: float = 5.0) -> dict:
    """Return the server's ``info`` event data (voices, capabilities)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        write_event(sock, "describe")
        rfile = sock.makefile("rb")
        while True:
            etype, data, _ = read_event(rfile)
            if etype == "info":
                return data
