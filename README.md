# HTTP Stream Player for Music Assistant

A custom [Music Assistant](https://music-assistant.io) player provider that exposes virtual players as
**stable, persistent HTTP MP3 streams**. Any device that can play an internet radio URL (web radios,
VLC, browsers, a Toniebox via [TeddyCloud](https://github.com/toniebox-reverse-engineering/teddycloud), …)
can tune into a player, while you control what it plays from Music Assistant.

Unlike a regular broadcast, **the listener connection is never closed**: on stop, at the end of
the queue and between tracks the stream keeps running and sends MP3 silence. Playback resumes on the
same connection as soon as Music Assistant plays something again.

It is based on the Web Broadcast provider proposed in
[music-assistant/server#4050](https://github.com/music-assistant/server/pull/4050), redesigned around
this always-on stream.

## Why

Some devices treat a closed stream as an error. A Toniebox with a live stream assigned to a tonie
through TeddyCloud, for example, plays an audible error announcement and stops when the stream ends,
and only tries again once the tonie is placed back on the box. With this provider the stream never
ends, so the tonie can stay on the box and Music Assistant alone decides what plays.

## Features

- One virtual player per configured name, each on its own fixed URL:
  `http://<ma-host>:8097/stream/<name>.mp3`
- Persistent stream: silence instead of a closed connection on stop and queue end
- MP3 at 128 kbit/s, 44.1 kHz stereo; Music Assistant delivers lossless FLAC to the provider, so
  audio is encoded lossy only once
- ICY (Shoutcast) metadata: track title (or the live title of a radio station), and in `full` mode
  the cover art via Music Assistant's image proxy
- Answers `HEAD` requests, for devices that probe the URL first
- Playback state, elapsed time and queue position stay in step with what is broadcast; a listener
  that reconnects after everyone left resumes the queue where it is now
- Players can be added, renamed and removed at runtime (provider settings, or *Delete* on the
  player) without disconnecting the listeners of other players
- HTTP profile setting (chunked / no content length / forced content length) for picky clients
- English and German translations

## Requirements

- Music Assistant **2.10.x (stable)**. The `dev` branch has moved players to a new
  `PlayerProtocol` model, so the provider will need changes for a future release.
- The stream is served on Music Assistant's **stream server** (plain HTTP, port 8097 by default).
  It has no authentication and is meant for the local network.

## Installation

This is a custom provider, so it is not part of the Music Assistant image.

1. Copy this repository into the providers directory of your Music Assistant installation as
   `http_stream_player`, e.g.
   `/app/venv/lib/python3.14/site-packages/music_assistant/providers/http_stream_player`
   in the official container.
2. Music Assistant only reads its compiled `translations/<lang>.json` files at runtime. To get the
   labels and descriptions in the UI, merge `strings.json` into `translations/en.json` and
   `translations/de.json` (this repo) into `translations/de.json`, with every key prefixed by
   `provider.http_stream_player.`.
3. Restart Music Assistant.

Copies inside the container are lost when it is recreated, so an automated way to reinstall the
provider (and merge the translations) after every update is recommended.

## Configuration

1. *Settings → Providers → Add provider → HTTP Stream Player*
2. Enter one name per player. Each name becomes a player and the URL `/stream/<name>.mp3`, using a
   lowercase, URL-safe form of the name (`Kids Room` → `/stream/kids-room.mp3`).
3. Later, edit the list in the provider settings. A name whose URL is already taken is renamed
   automatically (`Rock (2)`).

The player settings show the exact stream URL and offer:

| Setting | Default | Notes |
|---|---|---|
| ICY metadata | `basic` | `basic`: title every 16 KiB (~1 s). `full`: title and cover art URL every 256 KB (~16 s). `disabled`: none. Only sent to clients that ask for it (`Icy-MetaData: 1`). |
| HTTP profile | no content length | Use *chunked* or *forced content length* only if a client needs it. |

## Quick check

```bash
# headers
curl -sI http://<ma-host>:8097/stream/<name>.mp3

# ICY titles (and cover URLs in full mode)
curl -s --max-time 40 -H 'Icy-MetaData: 1' http://<ma-host>:8097/stream/<name>.mp3 \
  | grep -a -o "StreamTitle='[^;]*';\(StreamURL='[^']*';\)\?"
```

## How it works

For each player with at least one listener, the provider keeps **one MP3 encoder** running
(Music Assistant's `get_ffmpeg_stream` helper). Its input is a PCM generator that yields the audio of
the current source while Music Assistant plays, and real-time silence otherwise. Each source comes
from Music Assistant's regular flow stream URL (`resolve_stream_url`) and is decoded to a fixed PCM
format, so track changes, stop and the end of the queue never interrupt the MP3 stream itself. The
encoded stream is fanned out to all listeners; each listener gets its own ICY metadata framing.

The encoder stops when the last listener disconnects and starts again with the next one.

## Limitations

- MP3 at 128 kbit/s only
- Local network only, no authentication
- A slow listener whose buffer is full skips audio instead of being disconnected
- Built and tested against Music Assistant 2.10.12
