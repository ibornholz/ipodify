import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from ipodify import core
from ipodify.core import Availability, DirtyFilesystem, IPod, IPodState, Job, Track, Transfers, fmt_time

from conftest import wait_for

FAKE_SPOTDL = Path(__file__).parent / "fake_spotdl.py"


# ── helpers ───────────────────────────────────────────────────────────────────

def test_norm_and_artist():
    assert core.norm("Dreams - 2004 Remaster") == "dreams2004remaster"
    assert core.main_artist("Fleetwood Mac feat. Someone") == "fleetwoodmac"
    assert core.main_artist("A & B") == "a"
    assert core.main_artist("A, B") == "a"


def test_fmt_time():
    assert fmt_time(0) == "00:00"
    assert fmt_time(277.7) == "04:37"
    assert fmt_time(3600) == "60:00"
    assert fmt_time(None) == "--:--"
    assert fmt_time(-1) == "--:--"


def test_track_key_display_and_cache(track):
    assert track.display == "Wieso? Weshalb? Warum? - Kapitel 1"
    assert track.key == ("kapitel1", "wiesoweshalbwarum")
    assert track.cached_file is None
    d = core.CACHE / "tracks"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ok1.m4a").write_bytes(b"x")
    assert track.cached_file == d / "ok1.m4a"
    (d / "ok1.m4a").write_bytes(b"")  # empty = not cached
    assert track.cached_file is None


def test_config_roundtrip():
    core.save_config(client_id="abc", playlist="Test")
    assert core.load_config() == {"client_id": "abc", "playlist": "Test"}
    core.save_config(client_id="xyz")  # replaces the key, keeps the rest
    cfg = core.load_config()
    assert cfg["client_id"] == "xyz" and cfg["playlist"] == "Test"
    assert oct(os.stat(core.CONFIG_DIR / "config.toml").st_mode)[-3:] == "600"
    (core.CONFIG_DIR / "config.toml").write_text("not = [toml")
    assert core.load_config() == {}


# ── iPod state ────────────────────────────────────────────────────────────────

DUMP = "\n".join([
    "I\tiPod Classic",
    "T\t1\tDreams\tFleetwood Mac\tRumours\tFleetwood Mac\tRock\t1\t1977\t257000\t320\t10\t2\t4\t/iPod_Control/Music/F00/a.mp3\t1\t",
    "T\t2\tGo Your Own Way\tFleetwood Mac\tRumours\t\tRock\t2\t1977\t220000\t320\t9\t0\t0\t/iPod_Control/Music/F01/b.mp3\t1\t",
    "T\t3\tFolge 1\tDer Sender\tMein Podcast\t\tPodcast\t0\t2024\t1800000\t128\t20\t0\t0\t/iPod_Control/Music/F02/c.mp3\t4\thttps://x/ep1.mp3",
    "P\tm\tiPod\tmaster\t1,2,3",
    "P\t9\tRock\tnormal\t1,2",
])


def test_parse_dump():
    st = IPodState()
    IPod._parse(st, DUMP)
    assert st.model == "Classic"
    assert set(st.tracks) == {"1", "2", "3"}
    assert st.tracks["1"].rating == 4 and st.tracks["1"].plays == 2
    assert st.album_count("Rumours", "Fleetwood Mac") == 2
    assert ("dreams", "fleetwoodmac") in st.keys
    assert st.tracks["3"].is_podcast
    assert "https://x/ep1.mp3" in st.pod_urls and ("meinpodcast", "folge1") in st.pod_keys
    assert st.playlist("9").dbids == ["1", "2"] and st.playlist("m").kind == "master"
    assert st.playlist("nope") is None


def test_state_ready_needs_mount_and_helper(tmp_path):
    assert IPodState().status == "kein iPod"
    st = IPodState(mount=tmp_path)
    assert not st.ready and st.status == "./setup.sh ausführen"


def test_find_ipod(tmp_path, monkeypatch):
    (tmp_path / "iPod_Control").mkdir()
    esc = str(tmp_path).replace(" ", "\\040")
    mounts = f"/dev/sda1 / ext4 rw 0 0\n/dev/sdb2 {esc} hfsplus ro,nosuid 0 0\n"
    orig = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: mounts if str(self) == "/proc/mounts"
                        else orig(self, *a, **k))
    assert core.find_ipod() == ("/dev/sdb2", tmp_path, False)
    mounts = mounts.replace("ro,nosuid", "rw,nosuid")
    assert core.find_ipod()[2] is True
    mounts = ""
    assert core.find_ipod() == ("", None, False)


# ── iPod write strategy ───────────────────────────────────────────────────────

class FakeUdisks:
    """Tracks the mount state the way udisksctl would change it."""

    def __init__(self, mount: Path, busy: bool = False, rw_allowed: bool = True):
        self.mount, self.busy, self.rw_allowed = mount, busy, rw_allowed
        self.mounted, self.rw = True, False
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "unmount":
            if self.busy:
                raise RuntimeError("Error unmounting /dev/sdb2: GDBus.Error:org.freedesktop.UDisks2.Error.DeviceBusy: "
                                   "target is busy")
            self.mounted, self.rw = False, False
        elif args[0] == "mount":
            self.mounted = True
            self.rw = "force" in args and self.rw_allowed
        return ""

    def find(self):
        return ("/dev/sdb2", self.mount if self.mounted else None, self.rw)


@pytest.fixture
def ipod(tmp_path, monkeypatch):
    u = FakeUdisks(tmp_path)
    monkeypatch.setattr(core, "find_ipod", u.find)
    monkeypatch.setattr(IPod, "_udisks", lambda self, *a: u(*a))
    monkeypatch.setattr(IPod, "_prepare_sysinfo", staticmethod(lambda part, mount: None))
    p = IPod()
    p.udisks = u
    return p


def test_ensure_rw_remounts_with_force(ipod):
    assert ipod._ensure_rw() == ipod.udisks.mount
    assert [c[0] for c in ipod.udisks.calls] == ["unmount", "mount"]
    assert "force" in ipod.udisks.calls[1]
    ipod.udisks.calls.clear()
    ipod._ensure_rw()  # already writable → nothing to do
    assert ipod.udisks.calls == []


def test_ensure_rw_pauses_local_playback_and_resumes(ipod):
    log = []
    ipod.before_remount = lambda mount: log.append(("suspend", mount)) or (lambda: log.append("resume"))
    ipod._ensure_rw()
    assert log == [("suspend", str(ipod.udisks.mount)), "resume"]
    assert ipod.udisks.rw


def test_ensure_rw_busy_gives_readable_error(ipod):
    ipod.udisks.busy = True
    resumed = []
    ipod.before_remount = lambda mount: lambda: resumed.append(True)
    with pytest.raises(RuntimeError, match="noch benutzt"):
        ipod._ensure_rw()
    assert resumed == [True]  # playback continues even when the remount failed
    assert ipod.udisks.mounted


def test_ensure_rw_dirty_filesystem(ipod):
    ipod.udisks.rw_allowed = False
    with pytest.raises(DirtyFilesystem):
        ipod._ensure_rw()


def test_ensure_rw_without_ipod(ipod):
    ipod.udisks.mounted = False
    with pytest.raises(RuntimeError, match="kein iPod"):
        ipod._ensure_rw()


def test_release_if_idle(ipod, monkeypatch):
    monkeypatch.setattr(os, "sync", lambda: None)
    ipod._ensure_rw()
    ipod.last_write = time.time()
    assert ipod.release_if_idle() is False  # too soon
    ipod.last_write = 0
    ipod.busy = 1
    assert ipod.release_if_idle() is False
    ipod.busy = 0
    assert ipod.release_if_idle() is True
    assert not ipod.udisks.rw and ipod.udisks.mounted
    ipod.udisks.rw = True
    ipod.udisks.busy = True
    assert ipod.release_if_idle() is False  # a song from the iPod is playing: try later


def test_backup_keeps_last_30(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "data")
    db = tmp_path / "iPod_Control" / "iTunes"
    db.mkdir(parents=True)
    db.joinpath("iTunesDB").write_bytes(b"db")
    dest = tmp_path / "data" / "backup"
    dest.mkdir(parents=True)
    for i in range(35):
        (dest / f"iTunesDB-2020010{i:02d}-000000").write_bytes(b"old")
    IPod._backup(tmp_path)
    left = sorted(dest.glob("iTunesDB-*"))
    assert len(left) == 30 and left[-1].read_bytes() == b"db"


# ── availability ──────────────────────────────────────────────────────────────

def mk(tid: str) -> Track:
    return Track(id=tid, uri=f"spotify:track:{tid}", title=f"Titel {tid}", artists=["Artist"], album="Album",
                 duration_ms=1000, url=f"https://open.spotify.com/track/{tid}")


@pytest.fixture
def fake_spotdl(monkeypatch):
    monkeypatch.setattr(core, "SPOTDL", FAKE_SPOTDL)
    monkeypatch.setattr(core, "AUDIO_PROVIDERS", ["youtube-music"])
    yield
    Availability.FILE.unlink(missing_ok=True)


def test_availability_check_and_cache(fake_spotdl):
    av = Availability()
    progress = []
    res = av.check([mk("ok1"), mk("bad1"), mk("ok2")], lambda d, n: progress.append((d, n)))
    assert res == {"ok1": True, "bad1": False, "ok2": True}
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert av.get("ok1") is True and av.get("bad1") is False and av.get("unknown") is None
    assert av.song("ok1")["download_url"].endswith("ok1") and av.song("bad1") is None
    assert [t.id for t in av.unknown([mk("ok1"), mk("new")])] == ["new"]
    av2 = Availability()  # persisted
    assert av2.get("ok1") is True and av2.get("bad1") is False
    av2.forget("ok1")
    assert av2.get("ok1") is None


def test_availability_expiry(fake_spotdl):
    av = Availability()
    av.check([mk("ok1"), mk("bad1")])
    av.data["ok1"]["t"] -= Availability.TTL_FOUND + 1
    av.data["bad1"]["t"] -= Availability.TTL_MISSING + 1
    assert av.get("ok1") is None and av.get("bad1") is None and av.song("ok1") is None


def test_availability_incomplete_run_keeps_misses_unknown(fake_spotdl):
    av = Availability()
    with pytest.raises(RuntimeError, match="nur 2 von 3"):
        av.check([mk("ok1"), mk("bad1"), mk("skip1")])
    assert av.get("ok1") is True
    assert av.get("bad1") is None and av.get("skip1") is None  # not remembered as missing


def test_availability_dedupes_and_handles_empty(fake_spotdl):
    av = Availability()
    assert av.check([]) == {}
    assert av.check([mk("ok1"), mk("ok1")]) == {"ok1": True}


# ── transfers ─────────────────────────────────────────────────────────────────

@pytest.fixture
def transfers(fake_spotdl, monkeypatch):
    # no iPod here: keep the writer out, so "bereit" stays observable instead of "wartet auf iPod"
    monkeypatch.setattr(Transfers, "_write_loop", lambda self: None)
    changes = []  # (state, progress) snapshots – the Job objects themselves keep changing
    t = Transfers(IPod(), lambda job: changes.append((job.state, job.progress)), availability=Availability())
    t.changes = changes
    yield t
    for f in (core.CACHE / "tracks").glob("*"):
        f.unlink()


def test_download_batch(transfers):
    jobs = [Job(mk("ok1")), Job(mk("bad1"))]
    transfers.jobs = {j.track.id: j for j in jobs}
    transfers._download(jobs)
    assert jobs[0].state == "bereit" and jobs[0].track.cached_file is not None
    assert jobs[1].state == "fehler" and "No results" in jobs[1].error
    assert transfers.ready.get_nowait() is jobs[0]
    assert not list((core.CACHE / "tracks").glob("*.spotdl"))


def test_download_uses_preloaded_source(transfers, monkeypatch):
    transfers.availability.check([mk("ok1")])
    seen = {}
    real = core.subprocess.Popen

    def spy(cmd, **kw):
        files = [a for a in cmd if a.endswith(".spotdl")]
        seen["file"] = json.loads(Path(files[0]).read_text()) if files else None
        return real(cmd, **kw)

    monkeypatch.setattr(core.subprocess, "Popen", spy)
    job = Job(mk("ok1"))
    transfers._download([job])
    assert job.state == "bereit"
    assert seen["file"][0]["song_id"] == "ok1"  # the .spotdl file carried the known source
    assert not list((core.CACHE / "tracks").glob("*.spotdl"))  # and was cleaned up


def test_download_failure_forgets_stale_source(transfers):
    transfers.availability.data["bad1"] = {"url": "https://stale", "t": time.time(), "song": {"song_id": "bad1",
                                           "url": "https://open.spotify.com/track/bad1", "download_url": "x"}}
    job = Job(mk("bad1"))
    transfers._download([job])
    assert job.state == "fehler" and transfers.availability.get("bad1") is None


def test_add_routes_and_states(transfers, monkeypatch):
    monkeypatch.setattr(transfers, "_download", lambda batch: None)  # keep the worker quiet
    t = mk("ok1")
    assert transfers.add(t) is True
    assert transfers.add(t) is False  # already queued
    assert transfers.active == 1
    job = transfers.jobs["ok1"]
    transfers._set(job, "fehler", "x")
    assert transfers.failed_downloads() == [t]
    assert transfers.retry_failed() == 1 and transfers.jobs["ok1"].state == "wartet"
    rec = mk("ok2")
    assert transfers.add(rec, record=True)
    assert transfers.jobs["ok2"].record
    assert transfers.failed_downloads() == []  # record jobs are never offered for recording again
    transfers._set(transfers.jobs["ok1"], "fertig")
    assert transfers.clear_done() == ["ok1"] and "ok1" not in transfers.jobs
    assert transfers.changes[-1] == ("fertig", "")


class FakeRecorder:
    def __init__(self, fail_on: set[str] = frozenset()):
        self.fail_on, self.sessions, self.recorded = fail_on, 0, []

    @contextmanager
    def session(self):
        self.sessions += 1
        yield

    def record(self, track, on_progress):
        on_progress("⏺ 00:01 / 00:03")
        if track.id in self.fail_on:
            raise RuntimeError("Wiedergabe startet nicht")
        self.recorded.append(track.id)
        d = core.CACHE / "tracks"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{track.id}.mp3").write_bytes(b"rec")


def test_record_loop(transfers):
    transfers.recorder = FakeRecorder(fail_on={"ok3"})
    for tid in ("ok1", "ok2", "ok3"):
        transfers.add(mk(tid), record=True)
    assert wait_for(lambda: all(j.state in ("bereit", "fehler") for j in transfers.jobs.values()))
    assert transfers.recorder.recorded == ["ok1", "ok2"]
    assert transfers.jobs["ok3"].state == "fehler" and "Mitschnitt" in transfers.jobs["ok3"].error
    assert transfers.recorder.sessions == 1  # one player restart for the whole batch
    assert ("nimmt auf", "⏺ 00:01 / 00:03") in transfers.changes  # progress reached the UI callback
    assert transfers.ready.qsize() == 2


def test_record_without_recorder_fails_cleanly(transfers):
    transfers.recorder = None
    transfers.add(mk("ok1"), record=True)
    assert wait_for(lambda: transfers.jobs["ok1"].state == "fehler")
    assert "nicht verfügbar" in transfers.jobs["ok1"].error
