# Architecture

iPodify is a [Textual](https://textual.textualize.io) app (`ipodify/`) plus a tiny C helper on top of
libgpod (`helper/`).

```
                ┌──────────────── ipodify (Textual TUI) ────────────────┐
 Spotify  ◀──── │ spotify.py   Web API: search, library, playback ctl    │
 Web API        │ webplayer.py headless Chromium + Web Playback SDK ─┐   │──▶ PipeWire ──▶ speakers
                │ audio.py     PCM engine, FFT analyzer, monitor  ◀──┘   │
 Apple Podcasts │ podcasts.py  directory search, RSS, subscriptions      │
 directory/RSS ◀│ core.py      models, iPod access, transfer queue ──────┼──▶ spotDL ──▶ YouTube
                │ app.py       views, keys, LCD/spectrum deck            │
                └──────────────────────────┬─────────────────────────────┘
                                           │ TSV over stdin/stdout
                                   bin/ipod-helper (libgpod) ──▶ iPod iTunesDB
```

## Modules

| File | Purpose |
|---|---|
| `app.py` | The UI: deck (LCD, marquee, spectrum, seek bar), sidebar library, views and their actions, key bindings, background polling. |
| `widgets.py` | Title bar, marquee, spectrum analyzer, seek and volume bars. |
| `screens.py` | Modal dialogs: setup, login, device picker, tag editor, playlist picker, confirm. |
| `spotify.py` | Spotify Web API via spotipy: OAuth with the user's own app, search, library, device selection, playback control. |
| `webplayer.py` | Serves a one-file Web Playback SDK page on `127.0.0.1` and runs it in headless Chromium with a private profile. |
| `audio.py` | Audio engine: ffmpeg/librespot → PCM → `pacat`, a `parec` monitor for audio played elsewhere, and the FFT analyzer. |
| `podcasts.py` | Apple Podcasts directory search, RSS parsing, subscriptions and episode downloads. |
| `core.py` | Config, theme, data models, the `IPod` class (read/write strategy), the `Transfers` queue and the `Availability` cache. |
| `recorder.py` | Real-time capture of Spotify tracks from the SDK player through a private PipeWire null sink. |
| `onevent.py` | librespot `--onevent` hook (only used with `player = "librespot"`). |
| `helper/ipod-helper.c` | libgpod front end: dumps the database and applies batches of changes. |

## Playback

Three interchangeable players, all controlled through the Spotify Web API (play, pause, seek, next,
transfer):

- **`sdk` (default):** `webplayer.py` starts Chromium with `--headless=new`, a private profile under
  `~/.cache/ipodify/chromium-player` (only the Widevine CDM is copied in from the user's Chromium), and
  a set of flags that keep it small. The page loads `https://sdk.scdn.co/spotify-player.js`, takes
  tokens from `/token` and reports `ready`/errors to `/event`. Chromium plays straight to PipeWire.
- **`app`:** remote-controls the official Spotify desktop app (launched if needed).
- **`librespot`:** librespot with `--backend pipe` writes raw PCM into iPodify's own audio path.

With the Web Playback SDK and the desktop app, Spotify sends no events to iPodify, so `app.py` polls
`/me/player` (every 1.5 s while playing, 4 s otherwise) and interpolates the position in between.

iPod files and podcast episodes play locally: `ffmpeg` decodes them (podcasts can stream over HTTP)
to s16le 44.1 kHz stereo, and the engine writes that to `pacat` and paces it in real time itself,
because `pacat` would otherwise buffer seconds at once.

## Spectrum analyzer

`audio.Analyzer` keeps a one-second ring buffer of mono samples and, on every UI tick (24 fps), runs
an FFT over a 2048-sample Hann window. The bins are mapped onto log-spaced bands from 40 Hz to 16 kHz
with a +3 dB/octave tilt, then normalized to 0..1. The window is taken *delay* seconds back, so the
bars match what you hear:

- own audio path (iPod files, podcasts, librespot): output latency + lead (~0.18 s),
- everything else: samples captured from `@DEFAULT_MONITOR@` via `parec` (~0.03 s).

## The iPod

### Reading

`ipod-helper dump <mount>` prints one TSV line per device (`I`), track (`T`) and playlist (`P`).
`core.IPod.refresh()` parses it into `IPodState`, including lookup sets to mark Spotify tracks, albums
and podcast episodes that are already on the iPod (podcast matching uses the episode GUID, as iTunes
does).

### Writing

The iPod's HFS+ volume is journaled, and Linux mounts journaled HFS+ read-only. For each write
`IPod.apply()`:

1. remounts the volume read-write through udisks with the `force` option (no root needed),
2. on first use, adds the FireWire GUID (= USB serial) to `iPod_Control/Device/SysInfo`, which libgpod
   needs to compute the iPod Classic's database hash,
3. backs up the iTunesDB (the last 30 copies are kept),
4. pipes all operations to `ipod-helper apply`, which applies them in memory and writes the database
   once, or exits without writing if any operation fails,
5. `sync`s; 20 s after the last write the volume is remounted read-only, which flags it clean again.

`apply` operations (tab separated, one per line):

```
add      file  playlist  title  artist  album  genre  track_nr  year  ms  kbps  samplerate  cover
addpod   file  title  artist  album  ms  kbps  samplerate  cover  released  description  guid  rss
rm       dbid
set      dbid  field  value        # title artist album albumartist genre composer year track_nr rating
plnew    name
pldel    plid
plren    plid  name
pladd    plid  dbid
plrm     plid  dbid
```

Podcast episodes (`addpod`) are written the way iTunes writes them: media type 5, marked unplayed,
position remembered, skipped when shuffling, added to the special Podcasts playlist.

### Transfers

`core.Transfers` runs four worker threads:

- **spotDL:** downloads Spotify tracks in batches of up to 12 per spotDL process (4 threads), using
  the user's Spotify app credentials and the configured `--audio` provider chain. spotDL runs inside the
  target directory with a relative output template, because it strips leading dots from path components
  (`~/.cache` → `~/cache`). Tracks whose source the availability check already found are passed in a
  `.spotdl` file, so spotDL skips its search.
- **Podcasts:** plain HTTP downloads; formats the iPod can't play are converted to MP3.
- **Recorder:** real-time capture, see below.
- **Writer:** waits for the iPod, then writes every ready file in a single `apply` batch.

### Availability

`core.Availability` runs `spotdl save --preload --save-file -` for a list of tracks: spotDL searches
its providers and prints one song dict per track, with `download_url` set when a source exists (`null`
entries for misses). Results are cached in `~/.local/share/ipodify/availability.json`, hits for 30 days,
misses for 7. The album/playlist transfer runs this check first and lets the user choose between the
available titles only, or all of them with the rest recorded.

### Recording

`recorder.Recorder` is the fallback for tracks no provider has. A session:

1. loads `module-null-sink` (`ipodify-rec`, 44.1 kHz) via `pactl`,
2. restarts the headless Chromium with `PULSE_SINK=ipodify-rec`, so the SDK player's audio goes only
   there, and sets the Spotify volume to 100 % (the SDK scales samples before output),
3. per track: starts `parec` on `ipodify-rec.monitor`, plays the single URI through the Web API and
   waits for the SDK's `player_state_changed` events (the page posts them as `state`): playing →
   started, then `paused` with position 0 or a different track → ended,
4. trims leading/trailing silence with ffmpeg's `silenceremove`, encodes to the configured format at
   256 kbit/s, tags it with mutagen (cover from the album art URL), and drops it into
   `~/.cache/ipodify/tracks/<id>.<ext>`, where the writer picks it up,
5. afterwards restarts Chromium normally, unloads the sink and restores the volume.

iPod and podcast playback use iPodify's own audio path and keep working during a session.

## Process hygiene

Helpers that might outlive iPodify (headless Chromium, `parec`, librespot) are passed to
`core.reap_with_us()`. That starts a tiny shell watchdog which kills the helper, or its whole process
group, once iPodify is gone. On start, `webplayer.py` also removes Chromium instances left over from a
crash, since Chromium allows only one instance per profile.
