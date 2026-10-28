#!/usr/bin/env python3
"""Stand-in for the spotdl CLI. Track ids decide the outcome: 'ok…' is found, 'bad…' is not,
'skip…' is silently dropped (spotDL gave up half-way)."""

import json
import sys
from pathlib import Path

op, args = sys.argv[1], sys.argv[2:]
queries = [a for a in args if not a.startswith("-") and (a.startswith("http") or a.endswith(".spotdl"))]
ids = []
for q in queries:
    if q.endswith(".spotdl"):
        ids += [(s["song_id"], True) for s in json.loads(Path(q).read_text())]
    else:
        ids.append((q.rsplit("/", 1)[-1], False))
print("Processing", len(ids), "queries")
songs = []
for tid, preloaded in ids:
    if tid.startswith("skip"):
        continue
    title = f"Titel {tid}"
    if tid.startswith("bad"):
        print(f"Artist - {title} generated an exception: No results found for song: Artist - {title}")
        songs.append(None)
        continue
    print(f"Found url for Artist - {title}:\nhttps://music.youtube.com/watch?v={tid}")
    songs.append({"song_id": tid, "name": title, "url": f"https://open.spotify.com/track/{tid}",
                  "download_url": f"https://music.youtube.com/watch?v={tid}", "preloaded": preloaded})
    if op == "download":
        Path(f"{tid}.mp3").write_bytes(b"ID3" + b"\0" * 64)
        print(f'Downloaded "Artist - {title}": https://music.youtube.com/watch?v={tid}')
if op == "save":
    print(json.dumps(songs, indent=4))
