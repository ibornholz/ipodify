"""All tests run against a throw-away XDG tree: no real config, cache or iPod is touched."""

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="ipodify-tests-"))
for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
    os.environ[var] = str(_ROOT / sub)
os.environ["IPODIFY_HELPER"] = str(_ROOT / "no-helper")  # iPod writes are never attempted

import pytest  # noqa: E402

from ipodify import core  # noqa: E402  (imports after the environment is set, on purpose)


@pytest.fixture
def track():
    return core.Track(id="ok1", uri="spotify:track:ok1", title="Kapitel 1", artists=["Wieso? Weshalb? Warum?"],
                      album="Alles über Roboter", duration_ms=180000, url="https://open.spotify.com/track/ok1",
                      year=2021, track_nr=1)


def wait_for(cond, timeout=5.0):
    """Poll cond() until true; background threads make a few things asynchronous."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()
