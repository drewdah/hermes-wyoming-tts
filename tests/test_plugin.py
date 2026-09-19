"""Tests against an in-process fake Wyoming server. No Hermes needed; PyAV only for the resample test.

Run:  python tests/test_plugin.py      (or pytest)
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "wyoming-tts"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "wyoming_tts_under_test", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = _load_plugin()
protocol = sys.modules["wyoming_tts_under_test.protocol"]


# ---- fake Hermes tools.tts_streaming -------------------------------------------------------------

def _fake_ts(tts_config):
    ts = types.SimpleNamespace(registry={})

    class StreamingTTSProvider:
        sample_rate, channels, sample_width = 24000, 1, 2

        def __init__(self, tts_config, section):
            self.tts_config, self.section = tts_config, section

    def register(name):
        def wrap(cls):
            ts.registry[name] = cls
            return cls
        return wrap

    ts.StreamingTTSProvider, ts.register = StreamingTTSProvider, register
    ts._load_tts_config = lambda: tts_config
    return ts


# ---- fake Wyoming server -------------------------------------------------------------------------

class FakeServer:
    """mode: ok | stall | drop | error | inline (old-style inline data) | slow"""

    def __init__(self, mode="ok", rate=24000, width=2, channels=1, chunks=5, chunk_samples=2400):
        self.mode, self.rate, self.width, self.channels = mode, rate, width, channels
        self.chunks, self.chunk_samples = chunks, chunk_samples
        self.requests = []
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _send(self, conn, etype, data, payload=b""):
        if self.mode == "inline":
            hdr = {"type": etype, "data": data, "payload_length": len(payload)}
            conn.sendall(json.dumps(hdr).encode() + b"\n" + payload)
        else:
            protocol.write_event(conn, etype, data, payload)

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            etype, data, _ = protocol.read_event(conn.makefile("rb"))
            self.requests.append((etype, data))
            if self.mode == "error":
                self._send(conn, "error", {"text": "voice not found"})
                return
            fmt = {"rate": self.rate, "width": self.width, "channels": self.channels}
            self._send(conn, "audio-start", fmt)
            chunk = b"\x01\x00" * (self.chunk_samples * self.channels * self.width // 2)
            for i in range(self.chunks):
                if self.mode == "stall" and i == 1:
                    time.sleep(5)
                    return
                if self.mode == "drop" and i == 2:
                    return  # close mid-stream
                if self.mode == "slow":
                    time.sleep(0.05)
                self._send(conn, "audio-chunk", fmt, chunk)
            self._send(conn, "audio-stop", {})

    def close(self):
        self.sock.close()


def _streamer(port, **opts):
    cfg = {"provider": "pocket_tts", "providers": {"pocket_tts": {"voice": "kitt"}},
           "wyoming": {"host": "127.0.0.1", "port": port, "read_timeout": 1.0, **opts}}
    ts = _fake_ts(cfg)
    cls = plugin.build_streamer(ts)
    cls._down_until = 0.0
    inst = cls(cfg, cfg["wyoming"])
    inst._fallback = lambda text: iter([b"FB" * 10])  # stand-in for Hermes whole-file TTS
    return inst


# ---- tests ---------------------------------------------------------------------------------------

def test_passthrough_streams_all_audio_and_inherits_voice():
    srv = FakeServer()
    s = _streamer(srv.port)
    out = b"".join(s.stream("Hello there."))
    assert len(out) == 5 * 2400 * 2
    etype, req = srv.requests[0]
    assert etype == "synthesize" and req["text"] == "Hello there." and req["voice"] == {"name": "kitt"}
    srv.close()


def test_explicit_voice_overrides_inherited():
    srv = FakeServer()
    b"".join(_streamer(srv.port, voice="wheatley-v2", speaker="0").stream("Hi."))
    assert srv.requests[0][1]["voice"] == {"name": "wheatley-v2", "speaker": "0"}
    srv.close()


def test_old_style_inline_data_header():
    srv = FakeServer(mode="inline")
    assert len(b"".join(_streamer(srv.port).stream("Hi."))) == 5 * 2400 * 2
    srv.close()


def test_first_chunk_arrives_before_synthesis_finishes():
    srv = FakeServer(mode="slow", chunks=10)
    it = iter(_streamer(srv.port).stream("Hi."))
    t0 = time.monotonic()
    next(it)
    first = time.monotonic() - t0
    list(it)
    total = time.monotonic() - t0
    assert first < 0.2 and total > 0.4, (first, total)
    srv.close()


def test_connection_refused_falls_back_and_opens_breaker():
    dead = socket.create_server(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    s = _streamer(port)
    assert b"".join(s.stream("Hi.")) == b"FB" * 10
    assert type(s)._down_until > time.monotonic()
    # breaker open: next clause goes straight to fallback without trying the socket
    t0 = time.monotonic()
    assert b"".join(s.stream("Again.")) == b"FB" * 10
    assert time.monotonic() - t0 < 0.1


def test_server_error_before_audio_falls_back():
    srv = FakeServer(mode="error")
    assert b"".join(_streamer(srv.port).stream("Hi.")) == b"FB" * 10
    srv.close()


def test_stall_after_audio_raises_not_replays():
    srv = FakeServer(mode="stall")
    got = []
    try:
        for chunk in _streamer(srv.port).stream("Hi."):
            got.append(chunk)
        raise AssertionError("expected WyomingError")
    except protocol.WyomingError as exc:
        assert "mid-clause" in str(exc)
    assert got and b"FB" not in b"".join(got)
    srv.close()


def test_drop_mid_stream_raises_not_replays():
    srv = FakeServer(mode="drop")
    try:
        list(_streamer(srv.port).stream("Hi."))
        raise AssertionError("expected WyomingError")
    except protocol.WyomingError as exc:
        assert "mid-clause" in str(exc)
    srv.close()


def test_fallback_none_raises():
    srv = FakeServer(mode="error")
    try:
        list(_streamer(srv.port, fallback="none").stream("Hi."))
        raise AssertionError("expected WyomingError")
    except protocol.WyomingError:
        pass
    srv.close()


def test_early_close_releases_socket():
    srv = FakeServer(mode="slow", chunks=50)
    it = iter(_streamer(srv.port).stream("Hi."))
    next(it)
    it.close()  # what Hermes does on barge-in
    srv.close()


def test_resamples_mismatched_format():
    try:
        import av  # noqa: F401
    except ImportError:
        print("  (skipped: PyAV not installed)")
        return
    srv = FakeServer(rate=22050, channels=2, chunks=10, chunk_samples=2205)  # 1 s of 22.05 kHz stereo
    out = b"".join(_streamer(srv.port).stream("Hi."))
    seconds = len(out) / (24000 * 2)
    assert 0.95 < seconds < 1.05, seconds
    srv.close()


def test_available_requires_host():
    ts = _fake_ts({"wyoming": {}})
    assert plugin.build_streamer(ts).available() is False
    ts = _fake_ts({"wyoming": {"host": "x"}})
    assert plugin.build_streamer(ts).available() is True


def test_register_survives_missing_hermes_module():
    sys.modules.pop("tools.tts_streaming", None)
    plugin.register(object())  # must log, not raise


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failed else 0)
