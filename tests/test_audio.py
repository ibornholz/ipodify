import os
import time

import pytest

from ipodify import audio


class FakeFFmpeg:
    """Looks like a running ffmpeg: stdout never delivers until it's killed."""

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        r, self._w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        os.close(self._w)

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(audio.subprocess, "Popen", FakeFFmpeg)
    e = audio.Engine(lambda n, d: None)
    yield e
    e._stop_local()


def test_play_local_records_path_and_status(engine):
    engine.play_local("/mnt/iPod/iPod_Control/Music/F00/a.mp3", 12.0, info={"title": "T", "dur": 100})
    assert engine.source == "local" and engine.local_path.endswith("a.mp3")
    assert "-ss" in engine.local_proc.cmd and "12.00" in engine.local_proc.cmd
    st = engine.status()
    assert st["source"] == "local" and st["playing"] and not st["paused"]
    assert abs(st["pos"] - (12.0 - audio.Output.delay())) < 0.01
    engine.pause_local(True)
    assert engine.status()["paused"]


def test_suspend_only_for_files_below_mount(engine):
    assert engine.suspend_local("/mnt/iPod") is None  # nothing playing
    engine.play_local("/home/me/podcast.mp3", 0.0, info={"title": "P"})
    assert engine.suspend_local("/mnt/iPod") is None
    engine.play_local("https://example.org/stream.mp3", 0.0, info={"title": "S"})
    assert engine.suspend_local("/") is None  # streams never hold the mount


def test_suspend_and_resume_keep_position_and_pause(engine):
    engine.play_local("/mnt/iPod/iPod_Control/Music/F00/a.mp3", 30.0, info={"title": "T", "dur": 100, "n": 2})
    engine.pause_local(True)
    proc = engine.local_proc
    resume = engine.suspend_local("/mnt/iPod/")  # trailing slash tolerated
    assert callable(resume)
    assert proc.returncode is not None and engine.local_proc is None  # ffmpeg is gone, file closed
    resume()
    assert engine.local_proc is not None and engine.local_proc is not proc
    assert abs(engine.local_pos - (30.0 - audio.Output.delay())) < 0.01
    assert engine.status()["paused"] and engine.local_info["n"] == 2


def test_stop_clears_source(engine):
    engine.play_local("/mnt/iPod/x.mp3", 0.0, info={"title": "T"})
    engine.stop()
    assert engine.source is None and engine.status()["source"] is None
    assert engine.set_volume(150) is None and engine.volume == 100
