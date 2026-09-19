"""wyoming-tts — stream Hermes speech from any Wyoming TTS server (Piper, Pocket TTS, ...).

Registers a ``wyoming`` streaming TTS provider. Hermes pulls PCM from it clause by clause and
plays each clause as soon as its first audio chunk arrives, instead of waiting for a whole
audio file per clause. Enable with::

    plugins:
      enabled: [wyoming-tts]
    tts:
      streaming:
        provider: wyoming
      wyoming:
        host: 192.168.1.14

See README.md for every option.

Why the private registry: ``ctx.register_tts_provider()`` providers are only used for
whole-file synthesis; the gateway's streaming path resolves providers from
``tools.tts_streaming``'s registry. If that module ever changes shape, this plugin logs a
warning and does nothing, and Hermes keeps using its normal whole-file TTS.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from .protocol import PcmFormat, Synthesis, WyomingError

logger = logging.getLogger("wyoming_tts")

PROVIDER_NAME = "wyoming"
_SENTENCE_BYTE_CAP = 16 * 1024 * 1024  # same bound Hermes applies to its built-in streamers
_FALLBACK_MODES = ("provider", "none")


def _str(value: Any) -> Optional[str]:
    value = "" if value is None else str(value).strip()
    return value or None


def _section(tts_config: Dict) -> Dict:
    """Plugin options live under ``tts.wyoming`` (or ``tts.providers.wyoming``)."""
    return (tts_config.get(PROVIDER_NAME)
            or (tts_config.get("providers") or {}).get(PROVIDER_NAME) or {})


def _inherited_voice(tts_config: Dict) -> Optional[str]:
    """The ``voice`` of the active whole-file provider, so switching streaming on keeps the same voice."""
    active = _str(tts_config.get("provider"))
    if not active:
        return None
    sec = tts_config.get(active) or (tts_config.get("providers") or {}).get(active) or {}
    return _str(sec.get("voice")) if isinstance(sec, dict) else None


@dataclass
class Options:
    host: Optional[str]
    port: int = 10200
    voice: Optional[str] = None
    speaker: Optional[str] = None
    language: Optional[str] = None
    sample_rate: int = 24000
    connect_timeout: float = 2.0
    read_timeout: float = 15.0
    fallback: str = "provider"
    retry_after: float = 30.0

    @classmethod
    def from_config(cls, tts_config: Dict, section: Optional[Dict] = None) -> "Options":
        sec = section if section is not None else _section(tts_config)
        env = os.environ.get
        fallback = (_str(sec.get("fallback")) or "provider").lower()
        if fallback not in _FALLBACK_MODES:
            logger.warning("wyoming-tts: unknown fallback %r, using 'provider'", fallback)
            fallback = "provider"
        return cls(
            host=_str(sec.get("host")) or _str(env("WYOMING_TTS_HOST")),
            port=int(sec.get("port") or env("WYOMING_TTS_PORT") or 10200),
            voice=(_str(sec.get("voice")) or _str(env("WYOMING_TTS_VOICE"))
                   or _inherited_voice(tts_config)),
            speaker=_str(sec.get("speaker")),
            language=_str(sec.get("language")),
            sample_rate=int(sec.get("sample_rate") or 24000),
            connect_timeout=float(sec.get("connect_timeout") or 2.0),
            read_timeout=float(sec.get("read_timeout") or 15.0),
            fallback=fallback,
            retry_after=float(sec.get("retry_after", 30.0)),
        )


class PcmConverter:
    """Convert server PCM to the int16 mono stream Hermes was promised.

    Hermes fixes the stream format *before* synthesis starts, but a Wyoming server only announces
    its format in ``audio-start``. Matching formats pass straight through; anything else
    (Piper's 22050 Hz, stereo, 32-bit) is resampled with PyAV, which Hermes already ships.
    """

    _AV_FORMATS = {1: "u8", 2: "s16", 4: "s32"}

    def __init__(self, source: PcmFormat, target_rate: int) -> None:
        self.passthrough = source == PcmFormat(target_rate, 2, 1)
        self._frame_bytes = source.width * source.channels
        self._tail = b""
        self._source = source
        if self.passthrough:
            return
        if source.width not in self._AV_FORMATS or source.channels not in (1, 2):
            raise WyomingError(f"unsupported server audio format {source}")
        from av import AudioResampler  # imported lazily: only needed on mismatch
        self._resampler = AudioResampler(format="s16", layout="mono", rate=target_rate)

    def feed(self, pcm: bytes) -> Iterator[bytes]:
        data = self._tail + pcm
        end = len(data) - len(data) % self._frame_bytes
        self._tail = data[end:]
        if not end:
            return
        if self.passthrough:
            yield data[:end]
            return
        from av import AudioFrame
        src = self._source
        frame = AudioFrame(format=self._AV_FORMATS[src.width], layout="mono" if src.channels == 1 else "stereo",
                           samples=end // self._frame_bytes)
        frame.sample_rate = src.rate
        frame.planes[0].update(data[:end])
        for out in self._resampler.resample(frame):
            yield bytes(out.planes[0])[: out.samples * 2]

    def flush(self) -> Iterator[bytes]:
        if self.passthrough:
            return
        for out in self._resampler.resample(None):
            yield bytes(out.planes[0])[: out.samples * 2]


def build_streamer(ts) -> type:
    """Create the provider class on top of the live ``tools.tts_streaming`` module (injected for tests)."""

    class WyomingStreamer(ts.StreamingTTSProvider):
        """Wyoming TTS as a Hermes streaming provider, with per-clause fallback."""

        sample_rate = 24000
        _down_until = 0.0  # shared circuit breaker: skip a dead server instead of re-timing-out each clause

        def __init__(self, tts_config: Dict, section: Dict) -> None:
            super().__init__(tts_config, section)
            self.options = Options.from_config(tts_config, section or None)
            self.sample_rate = self.options.sample_rate  # read by Hermes to size the audio stream

        @staticmethod
        def available() -> bool:
            try:
                return bool(Options.from_config(ts._load_tts_config()).host)
            except Exception:  # pragma: no cover - config unreadable
                return False

        def stream(self, text: str) -> Iterator[bytes]:
            text = (text or "").strip()
            if not text:
                return
            opts = self.options
            cls = type(self)
            if opts.host and time.monotonic() >= cls._down_until:
                yielded = False
                try:
                    for chunk in self._wyoming(text):
                        yielded = True
                        yield chunk
                    return
                except (OSError, WyomingError) as exc:
                    if yielded:
                        raise WyomingError(f"stream broke mid-clause: {exc}") from exc  # never replay audio
                    if opts.retry_after > 0:
                        cls._down_until = time.monotonic() + opts.retry_after
                    if opts.fallback == "none":
                        raise
                    logger.warning("wyoming-tts: %s:%s unavailable (%s); using fallback for %.0fs",
                                   opts.host, opts.port, exc, opts.retry_after)
            elif opts.fallback == "none":
                raise WyomingError("wyoming server marked down; fallback disabled")
            yield from self._fallback(text)

        def _wyoming(self, text: str) -> Iterator[bytes]:
            opts = self.options
            t0 = time.monotonic()
            first = None
            total = 0
            converter: Optional[PcmConverter] = None
            with Synthesis(opts.host, opts.port, text, voice=opts.voice, speaker=opts.speaker,
                           language=opts.language, connect_timeout=opts.connect_timeout,
                           read_timeout=opts.read_timeout) as synth:
                for pcm in synth:
                    if converter is None:
                        converter = PcmConverter(synth.format, self.sample_rate)
                    for out in converter.feed(pcm):
                        if first is None:
                            first = time.monotonic() - t0
                        total += len(out)
                        if total > _SENTENCE_BYTE_CAP:
                            raise WyomingError("clause exceeded the 16 MiB audio cap")
                        yield out
                if converter is not None:
                    for out in converter.flush():
                        total += len(out)
                        yield out
                src = synth.format
            if not total:
                raise WyomingError("server returned no audio")
            logger.info("wyoming-tts: voice=%s first=%.3fs total=%.3fs audio=%.2fs src=%s",
                        opts.voice or "(default)", first or 0.0, time.monotonic() - t0,
                        total / (self.sample_rate * 2), f"{src.rate}/{src.width}/{src.channels}" if src else "?")

        def _fallback(self, text: str) -> Iterator[bytes]:
            """Synthesize this one clause with Hermes's configured whole-file provider, decode to PCM."""
            from tools.tts_tool import text_to_speech_tool
            with tempfile.TemporaryDirectory(prefix="wyoming_tts_fb_") as td:
                result = json.loads(text_to_speech_tool(text, output_path=os.path.join(td, "clause.mp3")))
                path = result.get("file_path")
                if not result.get("success") or not path:
                    raise WyomingError(f"fallback TTS failed: {result.get('error') or result}")
                ff = subprocess.run(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le",
                     "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(self.sample_rate), "pipe:1"],
                    capture_output=True, timeout=60)
                if ff.returncode != 0 or not ff.stdout:
                    raise WyomingError(f"fallback decode failed: {ff.stderr.decode(errors='replace')[:200]}")
            step = self.sample_rate * 2 // 25  # 40 ms
            for i in range(0, len(ff.stdout), step):
                yield ff.stdout[i:i + step]

    return WyomingStreamer


def register(ctx) -> None:
    try:
        import tools.tts_streaming as ts
        if not (hasattr(ts, "register") and hasattr(ts, "StreamingTTSProvider")):
            raise AttributeError("tools.tts_streaming no longer exposes register/StreamingTTSProvider")
        ts.register(PROVIDER_NAME)(build_streamer(ts))
    except Exception as exc:
        logger.warning("wyoming-tts: could not register streaming provider (%s); "
                       "Hermes will keep using whole-file TTS", exc)
        return
    logger.info("wyoming-tts: registered streaming TTS provider %r", PROVIDER_NAME)
