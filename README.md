# wyoming-tts: streaming Wyoming TTS for Hermes Agent

A Hermes plugin that streams speech from any [Wyoming](https://github.com/rhasspy/wyoming) TTS server
(Piper, Pocket TTS, anything Home Assistant can use as a Wyoming TTS). Each clause starts playing
as soon as the server's **first audio chunk** arrives. Without it, Hermes waits for a complete
audio file per clause.

Measured with Pocket TTS on a small CPU box, comparing the first audio of a clause:

| Clause | Via a whole-file provider (e.g. Home Assistant) | Via this plugin |
|---|---|---|
| short (~2 s of speech) | 1.1–1.5 s | **0.14–0.17 s** |
| long (~11 s of speech) | 5.6–8.2 s | **0.25–0.32 s** |

Synthesis speed is the same either way. The gain is that playback no longer waits for each whole
clause, which also removes the silent gaps between sentences in long replies.

## Install

Copy the `wyoming-tts/` folder into your Hermes home's `plugins/` directory (for a profile:
`~/.hermes/profiles/<name>/plugins/`), then add this to that home's `config.yaml`:

```yaml
plugins:
  enabled: [wyoming-tts]        # add to your existing list

tts:
  streaming:
    provider: wyoming           # use this plugin for streaming speech
  wyoming:
    host: 192.168.1.14          # your Wyoming TTS server
```

Restart the gateway. Its log should show `wyoming-tts: registered streaming TTS provider 'wyoming'`,
followed by one `wyoming-tts: voice=… first=…s` line per spoken clause.

Keep your normal `tts.provider` as it is. It still handles whole-file speech (voice notes, the
`text_to_speech` tool), and the plugin uses it as its fallback.

## Options (`tts.wyoming`)

| Key | Default | Meaning |
|---|---|---|
| `host` | env `WYOMING_TTS_HOST` | Wyoming server. Required; the plugin stays inactive without it. |
| `port` | `10200` (env `WYOMING_TTS_PORT`) | Wyoming TCP port. |
| `voice` | env `WYOMING_TTS_VOICE`, else the `voice` of your active `tts.provider` | Voice name sent to the server. |
| `speaker` | none | Speaker id for multi-speaker voices. |
| `language` | none | Language hint. |
| `sample_rate` | `24000` | Rate Hermes is told to expect. Server audio in any other rate, width or channel count is resampled with PyAV (already a Hermes dependency). |
| `connect_timeout` | `2.0` | Seconds before a dead server counts as down. |
| `read_timeout` | `15.0` | Longest allowed silence between audio events. |
| `fallback` | `provider` | `provider`: speak the clause through your normal `tts.provider`. `none`: fail and let Hermes handle it. |
| `retry_after` | `30` | After a failure, skip the server for this many seconds so each clause doesn't wait out the timeout again. `0` disables this. |

## Failure behaviour

- **Server unreachable, or an error before any audio**: that clause goes through your normal
  provider, and the server is skipped for `retry_after` seconds. The bot keeps talking.
- **Stream breaks after audio has played**: the clause is abandoned rather than restarted, so the
  listener never hears a sentence twice. Hermes treats it as a partial reply.
- **Barge-in**: the socket is closed right away.
- A background thread reads the server's audio as fast as it arrives, so a listener consuming
  audio at playback speed never slows down the TTS server's other clients.

## How it hooks in, and the caveat

`ctx.register_tts_provider()` is the documented TTS hook, but Hermes only uses providers
registered that way for whole-file synthesis. The streaming voice path looks providers up in
`tools.tts_streaming`'s own registry, so this plugin registers there with that module's
`register()` decorator. That registry isn't a documented plugin API. If a Hermes update changes it,
the plugin logs a warning, registers nothing, and Hermes falls back to whole-file TTS: slower, never
silent. Tested against Hermes Agent 0.21.3.

## Tests

```
python tests/test_plugin.py
```

These run against an in-process fake Wyoming server and cover normal streaming, old-style inline
headers, stalls, mid-stream drops, server errors, fallback, the retry cooldown, barge-in and
resampling. Hermes isn't needed; PyAV is needed only for the resampling test.

## Deploy script

`deploy/rollout.sh` is the script used in the author's own setup (Hermes voice profiles in a
Proxmox container). Treat it as an example: it backs up a profile's `config.yaml`, enables the
plugin, restarts the gateway's systemd unit, and has a `--rollback`.

## License

MIT. See [LICENSE](LICENSE).
