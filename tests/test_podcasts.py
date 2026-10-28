import json

import pytest

from ipodify import podcasts
from ipodify.core import IPodState

RSS = b"""<?xml version="1.0"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
 <title>Mein Podcast</title><itunes:author>Der Sender</itunes:author>
 <itunes:image href="https://x/art.jpg"/><category>Wissen</category>
 <item><title>Folge 2</title><guid>g2</guid><pubDate>Tue, 02 Jan 2024 10:00:00 +0000</pubDate>
  <enclosure url="https://x/ep2.m4a" type="audio/mp4"/><itunes:duration>1:02:03</itunes:duration>
  <itunes:summary>&lt;p&gt;Zweite   Folge&lt;/p&gt;</itunes:summary></item>
 <item><title>Folge 1</title><guid>g1</guid><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
  <enclosure url="https://x/ep1.mp3?x=1" type="audio/mpeg"/><itunes:duration>1800</itunes:duration>
  <description>Erste Folge</description><itunes:image href="https://x/ep1.jpg"/></item>
 <item><title>Video</title><enclosure url="https://x/v.mp4" type="video/mp4"/></item>
 <item><title>Ohne Datei</title></item>
</channel></rss>"""


def test_fetch_parses_feed(monkeypatch):
    monkeypatch.setattr(podcasts, "_get", lambda url, timeout=20: RSS)
    pod, eps = podcasts.fetch("https://x/feed")
    assert (pod.name, pod.author, pod.artwork, pod.count) == ("Mein Podcast", "Der Sender", "https://x/art.jpg", 2)
    assert [e.title for e in eps] == ["Folge 2", "Folge 1"]  # newest first, video and empty items skipped
    e2, e1 = eps
    assert e2.duration == 3723 and e1.duration == 1800
    assert e2.description == "Zweite Folge" and e1.description == "Erste Folge"
    assert e2.artwork == "https://x/art.jpg" and e1.artwork == "https://x/ep1.jpg"
    assert e2.ext == "m4a" and e1.ext == "mp3"
    assert e1.display == "Mein Podcast - Folge 1" and e1.artist == e1.album == "Mein Podcast"
    assert len(e1.id) == 16 and e1.id != e2.id


def test_fetch_rejects_non_rss(monkeypatch):
    monkeypatch.setattr(podcasts, "_get", lambda url, timeout=20: b"<html/>")
    with pytest.raises(RuntimeError):
        podcasts.fetch("https://x/feed")


def test_duration_formats():
    assert podcasts._duration(None) == 0
    assert podcasts._duration("90") == 90
    assert podcasts._duration("12:34") == 754
    assert podcasts._duration("1:02:03") == 3723


def test_on_ipod_matches_guid_url_or_title():
    ep = podcasts.Episode(guid="g1", title="Folge 1", podcast="Mein Podcast", feed="f", url="https://x/ep1.mp3")
    st = IPodState()
    assert not podcasts.on_ipod(ep, st)
    st.pod_urls.add("https://x/ep1.mp3")
    assert podcasts.on_ipod(ep, st)
    st.pod_urls.clear()
    st.pod_keys.add(("meinpodcast", "folge1"))
    assert podcasts.on_ipod(ep, st)


def test_subscriptions_roundtrip():
    subs = podcasts.Subscriptions()
    pod = podcasts.Podcast("https://x/feed", "Mein Podcast", "Der Sender", "https://x/art.jpg")
    sub = subs.subscribe(pod)
    sub.keep, sub.added["g1"] = 5, "dbid1"
    subs.save()
    again = podcasts.Subscriptions()
    assert again.subs["https://x/feed"].keep == 5 and again.subs["https://x/feed"].added == {"g1": "dbid1"}
    assert [p.name for p in again.podcasts()] == ["Mein Podcast"]
    again.unsubscribe("https://x/feed")
    assert podcasts.Subscriptions().subs == {}
    podcasts.SUBS_FILE.write_text("garbage")
    assert podcasts.Subscriptions().subs == {}
