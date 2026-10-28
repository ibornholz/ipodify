import math
import queue
import shutil
import struct
import threading
import time
from pathlib import Path

import pytest

from ipodify import recorder, spotify
from ipodify.recorder import Recorder

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg fehlt")


class FakeApi:
    def __init__(self):
        self.logged_in, self.device_id, self.calls = True, None, []

    def play(self, **kw):
        self.calls.append(("play", kw))

    def pause(self):
        self.calls.append(("pause",))

    def set_volume(self, v):
        self.calls.append(("volume", v))

    def find_device(self, local_only=False):
        self.device_id = "dev-new"
        return self.device_id


class FakeWebPlayer:
    def __init__(self):
        self.device_id, self.envs, self.state_hook = None, [], None

    def stop(self):
        self.device_id = None

    def start(self, env=None):
        self.envs.append(env)
        self.device_id = "dev-new"


@pytest.fixture
def rec(monkeypatch):
    calls = []

    def pactl(*args):
        calls.append(args)
        return "42\n" if args[0] == "load-module" else f"7\tmodule-null-sink\tsink_name={recorder.SINK}\n" \
            if args[0] == "list" else ""

    monkeypatch.setattr(recorder, "_pactl", pactl)
    monkeypatch.setattr(spotify, "PLAYER", "sdk")
    notes = []
    r = Recorder(FakeApi(), FakeWebPlayer(), lambda m, **kw: notes.append(m), lambda: 55)
    r.pactl_calls, r.notes = calls, notes
    return r


def test_session_routes_player_and_restores(rec):
    with rec.session():
        assert rec.active
        assert rec.webplayer.envs == [{"PULSE_SINK": recorder.SINK}]
        assert ("volume", 100) in rec.api.calls
        assert rec.api.device_id == "dev-new"
    assert not rec.active
    assert rec.webplayer.envs[-1] is None  # back to normal audio
    assert ("pause",) in rec.api.calls
    assert ("unload-module", "7") in rec.pactl_calls  # stale sink from a crash
    assert ("unload-module", "42") in rec.pactl_calls
    time.sleep(0.1)
    assert rec.api.calls[-1] == ("volume", 55)
    assert len(rec.notes) == 2


def test_session_requires_sdk_player(rec, monkeypatch):
    monkeypatch.setattr(spotify, "PLAYER", "app")
    with pytest.raises(RuntimeError, match="sdk"):
        with rec.session():
            pass
    assert rec.webplayer.envs == []


def test_session_requires_login(rec):
    rec.api.logged_in = False
    with pytest.raises(RuntimeError, match="anmelden"):
        with rec.session():
            pass


def feed(events, *evs):
    for e in evs:
        events.put(e)


def test_play_and_wait_ends_on_pause_at_zero(rec, track):
    ev = queue.Queue()
    feed(ev, {"track": track.id, "paused": True, "position": 0},  # loaded, not yet playing
         {"track": track.id, "paused": False, "position": 0},
         {"track": track.id, "paused": False, "position": 5000},
         {"track": track.id, "paused": True, "position": 0})
    rec._play_and_wait(track, ev, 180.0, None)
    assert rec.api.calls == [("play", {"uris": [track.uri]})]


def test_play_and_wait_ends_on_track_change(rec, track):
    ev = queue.Queue()
    feed(ev, {"track": track.id, "paused": False, "position": 0}, {"track": "other", "paused": False, "position": 0})
    rec._play_and_wait(track, ev, 180.0, None)


def test_play_and_wait_timeouts(rec, track):
    rec.START_TIMEOUT = 0.2
    with pytest.raises(RuntimeError, match="startet nicht"):
        rec._play_and_wait(track, queue.Queue(), 180.0, None)
    rec.START_TIMEOUT, rec.END_SLACK = 40, 0.2
    ev = queue.Queue()
    ev.put({"track": track.id, "paused": False, "position": 0})
    with pytest.raises(RuntimeError, match="kein Ende"):
        rec._play_and_wait(track, ev, 0.1, None)


def test_record_captures_state_events(rec, track, monkeypatch, tmp_path):
    """record() wires the SDK state hook to the wait loop and encodes what parec wrote."""
    started = threading.Event()

    class FakeParec:
        def __init__(self, cmd, stdout, stderr):
            self.cmd = cmd
            stdout.write(b"\0\0\0\0" * 100)
            started.set()

        def terminate(self):
            pass

        def wait(self, t=None):
            return 0

    monkeypatch.setattr(recorder.subprocess, "Popen", FakeParec)
    monkeypatch.setattr(recorder, "reap_with_us", lambda p, group=False: None)
    encoded = {}
    monkeypatch.setattr(Recorder, "_encode", lambda self, t, raw, dur: encoded.setdefault("raw", raw.read_bytes()) and tmp_path / "x")

    def push_events():
        started.wait(2)
        while not rec.webplayer.state_hook:
            time.sleep(0.01)
        rec.webplayer.state_hook({"track": track.id, "paused": False, "position": 0})
        rec.webplayer.state_hook({"track": track.id, "paused": True, "position": 0})

    threading.Thread(target=push_events, daemon=True).start()
    assert rec.record(track) == tmp_path / "x"
    assert len(encoded["raw"]) == 400 and rec.webplayer.state_hook is None
    assert not (recorder.REC_DIR / f"{track.id}.raw").exists()


def pcm(seconds: float, tone: bool) -> bytes:
    n = int(recorder.RATE * seconds)
    if not tone:
        return b"\0" * (n * 4)
    return b"".join(struct.pack("<hh", v, v) for v in (int(12000 * math.sin(2 * math.pi * 440 * i / recorder.RATE))
                                                       for i in range(n)))


@needs_ffmpeg
def test_encode_trims_silence_and_tags(rec, track, tmp_path):
    import mutagen

    raw = tmp_path / "t.raw"
    raw.write_bytes(pcm(0.7, False) + pcm(3.0, True) + pcm(1.5, False))
    track.duration_ms = 3000
    track.cover = ""
    out = rec._encode(track, raw, 3.0)
    assert out == recorder.CACHE / "tracks" / "ok1.mp3"
    m = mutagen.File(out)
    assert 2.9 <= m.info.length <= 3.6  # the 2.2 s of silence are gone, the music is not
    assert str(m.tags["TIT2"]) == "Kapitel 1" and str(m.tags["TALB"]) == "Alles über Roboter"
    assert str(m.tags["TRCK"]) == "1" and str(m.tags["TDRC"]) == "2021"
    out.unlink()


@needs_ffmpeg
def test_encode_rejects_short_capture(rec, track, tmp_path):
    raw = tmp_path / "t.raw"
    raw.write_bytes(pcm(1.0, True))
    with pytest.raises(RuntimeError, match="zu kurz"):
        rec._encode(track, raw, 30.0)
    assert not list((recorder.CACHE / "tracks").glob("ok1*"))
