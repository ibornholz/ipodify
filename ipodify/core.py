"""Shared bits: paths, config, omarchy theme, data models, iPod access, transfer queue."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = Path(os.environ.get("IPODIFY_HELPER", ROOT / "bin" / "ipod-helper"))
SPOTDL = ROOT / ".venv" / "bin" / "spotdl"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ipodify"
DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ipodify"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ipodify"
THEME_COLORS = Path.home() / ".local/state/omarchy/current/theme/colors.toml"


def reap_with_us(proc: subprocess.Popen, group: bool = False) -> None:
    """Make sure a helper dies with iPodify, however iPodify ends (closed window, crash, kill -9).

    Pipes don't cover every case: a recorder that gets no audio never writes, so it never notices
    that we're gone. A tiny watchdog shell polls both PIDs and kills the helper (or its group)."""
    target = f"-{proc.pid}" if group else str(proc.pid)
    subprocess.Popen(
        ["sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null && kill -0 {proc.pid} 2>/dev/null; do sleep 2; done; "
                     f"kill -TERM -- {target} 2>/dev/null; sleep 3; kill -KILL -- {target} 2>/dev/null"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def load_config() -> dict:
    """Optional ~/.config/ipodify/config.toml — see README."""
    try:
        return tomllib.loads((CONFIG_DIR / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_config(**values: str) -> None:
    """Set top-level string keys in config.toml, keeping everything else as it is."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "config.toml"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f'{k} = "{v}"' for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


CONFIG = load_config()
PLAYLIST = CONFIG.get("playlist", "iPodify")
FORMAT = CONFIG.get("format", "mp3")
COOKIES_BROWSER = CONFIG.get("cookies_from_browser", "")
# spotDL tries these in order until one has the song (youtube-music alone is spotDL's default).
AUDIO_PROVIDERS = ["youtube-music", "youtube", "soundcloud", "bandcamp"]
_cfg_audio = CONFIG.get("audio", AUDIO_PROVIDERS)
AUDIO_PROVIDERS = [p for p in ([_cfg_audio] if isinstance(_cfg_audio, str) else list(_cfg_audio)) if p] or AUDIO_PROVIDERS

# ── Theme ───────────────────────────────────────────────────────────────────

FALLBACK_COLORS = {
    "accent": "#7aa2f7", "background": "#1a1b26", "dark_background": "#13141c",
    "darker_background": "#0e0e14", "lighter_background": "#24283b", "selection": "#292e42",
    "foreground": "#a9b1d6", "dark_foreground": "#565f89", "bright_foreground": "#c0caf5",
    "red": "#f7768e", "yellow": "#e0af68", "green": "#9ece6a", "magenta": "#ad8ee6",
    "cyan": "#449dab", "muted": "#414868", "mode": "dark",
}


def load_colors() -> dict[str, str]:
    try:
        return FALLBACK_COLORS | tomllib.loads(THEME_COLORS.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return dict(FALLBACK_COLORS)


def theme_mtime() -> float:
    try:
        return THEME_COLORS.stat().st_mtime
    except OSError:
        return 0.0


# ── Models ──────────────────────────────────────────────────────────────────


def norm(s: str) -> str:
    return re.sub(r"\W+", "", (s or "").lower())


def main_artist(s: str) -> str:
    return norm(re.split(r",|&| feat\.?| ft\.?| x ", s or "", maxsplit=1, flags=re.I)[0])


def fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}" if m < 100 else f"{m // 60}h{m % 60:02d}"


@dataclass
class Track:
    id: str
    uri: str
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    url: str
    album_uri: str = ""
    year: int = 0
    track_nr: int = 0
    cover: str = ""  # album art URL

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}"

    @property
    def key(self) -> tuple[str, str]:
        return norm(self.title), main_artist(self.artists[0] if self.artists else "")

    @property
    def cached_file(self) -> Path | None:
        for ext in ("mp3", "m4a"):
            p = CACHE / "tracks" / f"{self.id}.{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None


@dataclass
class Album:
    id: str
    uri: str
    name: str
    artists: list[str]
    year: int
    total: int
    url: str

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


@dataclass
class Playlist:
    id: str
    uri: str
    name: str
    owner: str
    total: int
    url: str


@dataclass
class Artist:
    id: str
    uri: str
    name: str
    genres: list[str]
    url: str


@dataclass
class IPodTrack:
    dbid: str
    title: str
    artist: str
    album: str
    albumartist: str
    genre: str
    track_nr: int
    year: int
    ms: int
    kbps: int
    size: int
    plays: int
    rating: int
    path: str
    mediatype: int = 1
    podcasturl: str = ""

    @property
    def is_podcast(self) -> bool:
        return bool(self.mediatype & 4)

    @property
    def key(self) -> tuple[str, str]:
        return norm(self.title), main_artist(self.artist)

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass
class IPodPlaylist:
    id: str
    name: str
    kind: str  # master · podcast · smart · normal
    dbids: list[str]


# ── iPod ────────────────────────────────────────────────────────────────────


class DirtyFilesystem(RuntimeError):
    pass


@dataclass
class IPodState:
    part: str = ""
    mount: Path | None = None
    writable: bool = False
    model: str = ""
    free_gb: float = 0.0
    total_gb: float = 0.0
    tracks: dict[str, IPodTrack] = field(default_factory=dict)
    playlists: list[IPodPlaylist] = field(default_factory=list)
    keys: set = field(default_factory=set)
    albums: dict[tuple[str, str], int] = field(default_factory=dict)
    pod_urls: set = field(default_factory=set)
    pod_keys: set = field(default_factory=set)
    error: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.mount) and HELPER.exists() and not self.error

    @property
    def status(self) -> str:
        if not self.mount:
            return "kein iPod"
        if not HELPER.exists():
            return "./setup.sh ausführen"
        return self.error or "bereit"

    def playlist(self, plid: str) -> IPodPlaylist | None:
        return next((p for p in self.playlists if p.id == plid), None)

    def album_count(self, album: str, artist: str) -> int:
        return self.albums.get((norm(album), main_artist(artist)), 0)


def _run(*cmd: str, timeout: int = 60, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)


def find_ipod() -> tuple[str, Path | None, bool]:
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return "", None, False
    for line in lines:
        dev, mnt, _fs, opts, *_ = line.split()
        p = Path(mnt.encode().decode("unicode_escape"))
        if (p / "iPod_Control").is_dir():
            return dev, p, "rw" in opts.split(",")
    return "", None, False


class IPod:
    """Reads the iTunesDB and applies changes, mounting read-write only while writing.

    The iPod's HFS+ is journaled, so Linux mounts it read-only by default. For a write we
    remount with `force` via udisks (no root needed), back up the DB, write, and remount
    read-only again once idle so the volume is marked clean as soon as possible.
    """

    RELEASE_AFTER = 20  # seconds without writes before going back to read-only

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state = IPodState()
        self.last_write = 0.0
        self.busy = 0
        # Called with the mount point before an unmount; may return a callback that is run once the
        # iPod is mounted again (the app uses this to pause a track playing from the iPod).
        self.before_remount: Callable[[str], Callable[[], None] | None] | None = None

    # reading ------------------------------------------------------------------
    def refresh(self) -> IPodState:
        with self.lock:
            part, mount, writable = find_ipod()
            st = IPodState(part=part, mount=mount, writable=writable)
            if not mount:
                self.state = st
                return st
            vfs = os.statvfs(mount)
            st.free_gb = vfs.f_bavail * vfs.f_frsize / 1e9
            st.total_gb = vfs.f_blocks * vfs.f_frsize / 1e9
            if HELPER.exists():
                try:
                    self._parse(st, self._helper("dump", str(mount)).stdout)
                except Exception as e:  # noqa: BLE001
                    st.error = f"Datenbank: {e}"
            self.state = st
            return st

    @staticmethod
    def _parse(st: IPodState, out: str) -> None:
        for line in out.splitlines():
            f = line.split("\t")
            if f[0] == "I":
                st.model = f[1].replace("iPod ", "")
            elif f[0] == "T" and len(f) >= 15:
                t = IPodTrack(f[1], f[2], f[3], f[4], f[5], f[6], *(int(x or 0) for x in f[7:14]), f[14],
                              int(f[15] or 1) if len(f) > 15 else 1, f[16] if len(f) > 16 else "")
                st.tracks[t.dbid] = t
                if t.is_podcast:
                    st.pod_urls.add(t.podcasturl)
                    st.pod_keys.add((norm(t.album), norm(t.title)))
                st.keys.add(t.key)
                k = (norm(t.album), main_artist(t.albumartist or t.artist))
                st.albums[k] = st.albums.get(k, 0) + 1
            elif f[0] == "P":
                st.playlists.append(IPodPlaylist(f[1], f[2], f[3], [x for x in f[4].split(",") if x]))

    @staticmethod
    def _helper(*args: str, input: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        r = _run(str(HELPER), *args, input=input, timeout=timeout)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                               else f"ipod-helper {args[0]} fehlgeschlagen")
        return r

    # writing ------------------------------------------------------------------
    def apply(self, ops: list[list[str]]) -> list[list[str]]:
        """Run helper ops in one DB write. Returns the helper's output lines split by tab."""
        clean = lambda v: str(v).replace("\t", " ").replace("\n", " ").replace("\r", " ")  # noqa: E731
        payload = "".join("\t".join(clean(v) for v in op) + "\n" for op in ops)
        with self.lock:
            self.busy += 1
            try:
                mount = self._ensure_rw()
                self._backup(mount)
                r = self._helper("apply", str(mount), input=payload, timeout=600)
                os.sync()
                self.last_write = time.time()
                return [l.split("\t") for l in r.stdout.splitlines()]
            finally:
                self.busy -= 1

    def _udisks(self, *args: str) -> str:
        r = _run("udisksctl", *args, "--no-user-interaction", timeout=60)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
        return r.stdout

    def _ensure_rw(self) -> Path:
        part, mount, writable = find_ipod()
        if not mount:
            raise RuntimeError("kein iPod angeschlossen")
        if writable:
            return mount
        resume = self.before_remount(str(mount)) if self.before_remount else None
        try:
            try:
                self._udisks("unmount", "-b", part)
            except RuntimeError as e:
                if "busy" in str(e).lower():
                    raise RuntimeError("iPod wird noch benutzt (Datei geöffnet) – Wiedergabe stoppen "
                                       "und erneut versuchen") from e
                raise
            self._udisks("mount", "-b", part, "-o", "force")
        finally:
            if resume:
                if not find_ipod()[1]:  # remount failed: at least get it back read-only
                    try:
                        self._udisks("mount", "-b", part)
                    except RuntimeError:
                        pass
                resume()
        part, mount, writable = find_ipod()
        if not writable:
            raise DirtyFilesystem("iPod-Dateisystem muss geprüft werden – ./setup.sh ausführen")
        self._prepare_sysinfo(part, mount)
        return mount

    def release_if_idle(self) -> bool:
        """Back to read-only (= marked clean) once nothing was written for a while."""
        if self.busy or time.time() - self.last_write < self.RELEASE_AFTER:
            return False
        with self.lock:
            part, mount, writable = find_ipod()
            if not (mount and writable):
                return False
            os.sync()
            try:
                self._udisks("unmount", "-b", part)
            except RuntimeError:  # busy (e.g. a song from the iPod is playing) — try later
                return False
            self._udisks("mount", "-b", part)
            return True

    @staticmethod
    def _prepare_sysinfo(part: str, mount: Path) -> None:
        """libgpod needs the FireWire GUID (= USB serial) to sign the iPod Classic's DB."""
        sysinfo = mount / "iPod_Control" / "Device" / "SysInfo"
        try:
            text = sysinfo.read_text(errors="ignore")
        except OSError:
            text = ""
        if "FirewireGuid" in text:
            return
        props = _run("udevadm", "info", "-q", "property", "-n", part).stdout
        if m := re.search(r"^ID_SERIAL_SHORT=([0-9A-Fa-f]{16})$", props, re.M):
            sysinfo.write_text(text + f"FirewireGuid: 0x{m.group(1)}\n")

    @staticmethod
    def _backup(mount: Path) -> None:
        dest = DATA / "backup"
        dest.mkdir(parents=True, exist_ok=True)
        src = mount / "iPod_Control" / "iTunes" / "iTunesDB"
        if src.exists():
            shutil.copy2(src, dest / time.strftime("iTunesDB-%Y%m%d-%H%M%S"))
        for old in sorted(dest.glob("iTunesDB-*"))[:-30]:
            old.unlink()

    def eject(self) -> None:
        with self.lock:
            part, mount, _ = find_ipod()
            if not part:
                return
            os.sync()
            self._udisks("unmount", "-b", part)
            disk = _run("lsblk", "-no", "PKNAME", part).stdout.strip()
            if disk:
                _run("udisksctl", "power-off", "-b", f"/dev/{disk}", "--no-user-interaction")


# ── Transfer queue: spotDL (YouTube) download → iPod ────────────────────────


@dataclass
class Job:
    track: Track  # or a podcasts.Episode
    state: str = "wartet"  # wartet · lädt · nimmt auf · bereit · überträgt · fertig · fehler
    error: str = ""
    progress: str = ""  # free text while running (recording position …)
    record: bool = False  # capture from the Spotify player instead of downloading


# ── Availability: which Spotify tracks spotDL can find on YouTube & co. ─────


class Availability:
    """Asks spotDL (`save --preload`) for a source per track, without downloading, and remembers the
    answer: found URLs for a month, misses for a week (labels do upload late)."""

    FILE = DATA / "availability.json"
    TTL_FOUND, TTL_MISSING = 30 * 86400, 7 * 86400

    def __init__(self) -> None:
        self.lock = threading.Lock()
        try:
            self.data: dict[str, dict] = json.loads(self.FILE.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, track_id: str) -> bool | None:
        """True = source known, False = nothing found, None = not checked (or expired)."""
        e = self.data.get(track_id)
        if not e:
            return None
        if time.time() - e.get("t", 0) > (self.TTL_FOUND if e.get("url") else self.TTL_MISSING):
            return None
        return bool(e.get("url"))

    def song(self, track_id: str) -> dict | None:
        """spotDL's song dict incl. download_url, for a download without a second search."""
        e = self.data.get(track_id)
        return e.get("song") if e and e.get("url") and self.get(track_id) else None

    def unknown(self, tracks: list[Track]) -> list[Track]:
        return [t for t in tracks if self.get(t.id) is None]

    def check(self, tracks: list[Track], on_progress=None) -> dict[str, bool]:
        """Blocking; one spotDL process for all tracks. on_progress(done, total) from this thread."""
        tracks = list({t.id: t for t in tracks}.values())
        if not tracks:
            return {}
        cmd = [str(SPOTDL), "save", *(t.url for t in tracks), "--save-file", "-", "--preload",
               "--simple-tui", "--max-retries", "1", "--audio", *AUDIO_PROVIDERS]  # a miss costs seconds, not minutes
        cfg = load_config()
        if cfg.get("client_id") and cfg.get("client_secret"):
            cmd += ["--client-id", cfg["client_id"], "--client-secret", cfg["client_secret"]]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                env=os.environ | {"PYTHONUNBUFFERED": "1", "COLUMNS": "400"})
        done, out = 0, []
        for line in proc.stdout:
            if line.startswith("Found url for ") or "generated an exception" in line:
                done += 1
                if on_progress:
                    on_progress(min(done, len(tracks)), len(tracks))
            out.append(line)
        proc.wait()
        text = "".join(out)
        try:
            songs = json.loads(text[text.rindex("\n["):])
        except ValueError as e:
            raise RuntimeError("spotDL lieferte kein Ergebnis") from e
        found = {sg.get("song_id"): sg for sg in songs if sg and sg.get("download_url")}
        complete = len(songs) == len(tracks)  # else spotDL gave up half-way: don't remember misses
        result: dict[str, bool] = {}
        with self.lock:
            for t in tracks:
                sg = found.get(t.id)
                result[t.id] = bool(sg)
                if sg:
                    self.data[t.id] = {"url": sg["download_url"], "t": time.time(), "song": sg}
                elif complete:
                    self.data[t.id] = {"url": "", "t": time.time()}
            self._save()
        if not complete:
            raise RuntimeError(f"spotDL hat nur {len(songs)} von {len(tracks)} Titeln geprüft")
        return result

    def forget(self, track_id: str) -> None:
        with self.lock:
            self.data.pop(track_id, None)
            self._save()

    def _save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            tmp = self.FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.FILE)
        except OSError:
            pass


class Transfers:
    """Downloads in batches with one spotDL process, writes to the iPod in batches.

    `on_change(job)` is called from worker threads.
    """

    BATCH = 12

    def __init__(self, ipod: IPod, on_change, subs=None, availability: Availability | None = None) -> None:
        self.ipod = ipod
        self.on_change = on_change
        self.subs = subs  # podcasts.Subscriptions – remembers which episodes we put on the iPod
        self.availability = availability or Availability()
        self.recorder = None  # recorder.Recorder, set by the app (needs the Spotify player)
        self.jobs: dict[str, Job] = {}
        self.pending: queue.Queue[Job] = queue.Queue()
        self.pod_pending: queue.Queue[Job] = queue.Queue()
        self.rec_pending: queue.Queue[Job] = queue.Queue()
        self.ready: queue.Queue[Job] = queue.Queue()
        threading.Thread(target=self._download_loop, daemon=True).start()
        threading.Thread(target=self._pod_loop, daemon=True).start()
        threading.Thread(target=self._record_loop, daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def add(self, item, record: bool = False) -> bool:
        """item: a Spotify Track (→ spotDL, or record=True → captured from the Spotify player)
        or a podcasts.Episode (→ plain download)."""
        job = self.jobs.get(item.id)
        if job and job.state != "fehler":
            return False
        job = Job(item, record=record and isinstance(item, Track))
        self.jobs[item.id] = job
        self.on_change(job)
        is_episode = not isinstance(item, Track)
        (self.ready if item.cached_file else self.pod_pending if is_episode else
         self.rec_pending if job.record else self.pending).put(job)
        return True

    def retry_failed(self) -> int:
        failed = [j for j in self.jobs.values() if j.state == "fehler"]
        return sum(self.add(j.track, record=j.record) for j in failed)

    def failed_downloads(self) -> list[Track]:
        """Spotify tracks whose download failed – candidates for a recording."""
        return [j.track for j in self.jobs.values()
                if j.state == "fehler" and not j.record and isinstance(j.track, Track)]

    @property
    def recording(self) -> bool:
        return any(j.state == "nimmt auf" for j in self.jobs.values())

    def clear_done(self) -> list[str]:
        done = [k for k, j in self.jobs.items() if j.state == "fertig"]
        for k in done:
            del self.jobs[k]
        return done

    @property
    def active(self) -> int:
        return sum(j.state not in ("fertig", "fehler") for j in self.jobs.values())

    def _set(self, job: Job, state: str, error: str = "") -> None:
        job.state, job.error, job.progress = state, error, ""
        self.on_change(job)

    def _progress(self, job: Job, text: str) -> None:
        job.progress = text
        self.on_change(job)

    @staticmethod
    def _drain(q: queue.Queue, first: Job, limit: int) -> list[Job]:
        batch = [first]
        while len(batch) < limit:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        return batch

    # download ------------------------------------------------------------------
    def _download_loop(self) -> None:
        while True:
            batch = self._drain(self.pending, self.pending.get(), self.BATCH)
            try:
                self._download(batch)
            except Exception as e:  # noqa: BLE001
                for job in batch:
                    if job.state == "lädt":
                        self._set(job, "fehler", str(e)[:200])

    def _download(self, batch: list[Job]) -> None:
        out_dir = CACHE / "tracks"
        out_dir.mkdir(parents=True, exist_ok=True)
        for job in batch:
            self._set(job, "lädt")
        queries, preloaded = [], []
        for j in batch:
            (preloaded.append if (sg := self.availability.song(j.track.id)) else queries.append)(sg or j.track.url)
        if preloaded:  # a .spotdl file carries the source URL → spotDL skips its search
            spotdl_file = out_dir / f"batch-{int(time.time() * 1000)}.spotdl"
            spotdl_file.write_text(json.dumps(preloaded))
            queries.append(str(spotdl_file))
        cmd = [str(SPOTDL), "download", *queries,
               # relative on purpose: spotDL strips leading dots from path parts (~/.cache → ~/cache)
               "--output", "{track-id}.{output-ext}", "--format", FORMAT,
               "--bitrate", "auto", "--threads", "4", "--simple-tui", "--print-errors",
               "--audio", *AUDIO_PROVIDERS]
        cfg = load_config()
        if cfg.get("client_id") and cfg.get("client_secret"):  # own app instead of spotDL's shared one
            cmd += ["--client-id", cfg["client_id"], "--client-secret", cfg["client_secret"]]
        if COOKIES_BROWSER:
            cmd += ["--yt-dlp-args", f"--cookies-from-browser {COOKIES_BROWSER}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=out_dir,
                                env=os.environ | {"PYTHONUNBUFFERED": "1", "COLUMNS": "400"})
        waiting = {j.track.id: j for j in batch}
        log: list[str] = []
        for line in proc.stdout:
            log.append(line.strip())
            low = line.lower()
            if "not a bot" in low:
                log.append("BOT")
            for tid, job in list(waiting.items()):
                if norm(job.track.title)[:24] in norm(line) and job.track.cached_file:
                    del waiting[tid]
                    self._set(job, "bereit")
                    self.ready.put(job)
        proc.wait()
        if preloaded:
            spotdl_file.unlink(missing_ok=True)
        for job in waiting.values():
            if job.track.cached_file:
                self._set(job, "bereit")
                self.ready.put(job)
            elif "BOT" in log:
                self._set(job, "fehler", "YouTube-Bot-Sperre – cookies_from_browser in config.toml setzen")
            else:
                hint = next((l for l in reversed(log) if norm(job.track.title)[:24] in norm(l)), "")
                self.availability.forget(job.track.id)  # a remembered source may be stale
                self._set(job, "fehler", hint[:200] or "kein passender YouTube-Treffer")

    # recording -----------------------------------------------------------------
    def _record_loop(self) -> None:
        """Real-time capture from the Spotify player, one session for all queued recordings."""
        while True:
            batch = self._drain(self.rec_pending, self.rec_pending.get(), 500)
            if not self.recorder:
                for job in batch:
                    self._set(job, "fehler", "Mitschnitt nicht verfügbar")
                continue
            try:
                with self.recorder.session():
                    while batch:
                        job = batch.pop(0)
                        self._set(job, "nimmt auf")
                        try:
                            self.recorder.record(job.track, lambda text, j=job: self._progress(j, text))
                            self._set(job, "bereit")
                            self.ready.put(job)
                        except Exception as e:  # noqa: BLE001
                            self._set(job, "fehler", f"Mitschnitt: {e}"[:200])
                        batch += self._drain(self.rec_pending, None, 500)[1:]
            except Exception as e:  # noqa: BLE001
                for job in batch:
                    self._set(job, "fehler", f"Mitschnitt: {e}"[:200])

    def _pod_loop(self) -> None:
        from . import podcasts

        while True:
            job = self.pod_pending.get()
            self._set(job, "lädt")
            try:
                podcasts.download(job.track)
                self._set(job, "bereit")
                self.ready.put(job)
            except Exception as e:  # noqa: BLE001
                self._set(job, "fehler", f"Download: {e}"[:200])

    # iPod ----------------------------------------------------------------------
    def _write_loop(self) -> None:
        while True:
            first = self.ready.get()
            while not self.ipod.state.ready:
                if first.state != "wartet auf iPod":
                    self._set(first, "wartet auf iPod")
                time.sleep(3)
            time.sleep(1)  # let a few more finish so they share one DB write
            batch = self._drain(self.ready, first, 50)
            try:
                self._write(batch)
            except Exception as e:  # noqa: BLE001
                for job in batch:
                    self._set(job, "fehler", str(e)[:200])

    def _write(self, batch: list[Job]) -> None:
        import mutagen

        from . import podcasts

        ops, covers = [], []
        for job in batch:
            self._set(job, "überträgt")
            t, f = job.track, job.track.cached_file
            m = mutagen.File(f)
            info = getattr(m, "info", None)
            if isinstance(t, podcasts.Episode):
                ops.append(["addpod", f, t.title, t.author or t.podcast, t.podcast,
                            int(info.length * 1000) if info else t.duration * 1000,
                            int(getattr(info, "bitrate", 0) / 1000), int(getattr(info, "sample_rate", 44100)),
                            podcasts.artwork_file(t.feed, t.artwork), t.pub, t.description, t.guid, t.feed])
                continue
            cover = ""
            if m is not None and m.tags:
                for k in list(m.tags.keys()):
                    if k.startswith("APIC") or k == "covr":
                        data = m.tags[k].data if k.startswith("APIC") else bytes(m.tags[k][0])
                        cover = str(CACHE / f"{t.id}.jpg")
                        Path(cover).write_bytes(data)
                        covers.append(cover)
                        break
            genre = ""
            if m is not None and m.tags and "TCON" in m.tags:
                genre = str(m.tags["TCON"].text[0])
            ops.append(["add", f, PLAYLIST, t.title, t.artist, t.album, genre, t.track_nr, t.year,
                        int(info.length * 1000) if info else t.duration_ms,
                        int(getattr(info, "bitrate", 0) / 1000), int(getattr(info, "sample_rate", 44100)),
                        cover])
        try:
            out = self.ipod.apply(ops)
        finally:
            for c in covers:
                Path(c).unlink(missing_ok=True)
        dbids = [o[1] for o in out if o[0] == "added"]  # same order as the add/addpod ops
        for job, dbid in zip(batch, dbids):
            if self.subs and (sub := self.subs.subs.get(getattr(job.track, "feed", ""))):
                sub.added[job.track.guid] = dbid
        if self.subs:
            self.subs.save()
        for job in batch:
            self._set(job, "fertig")
