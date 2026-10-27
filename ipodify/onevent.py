"""librespot --onevent hook: forwards the event's environment to iPodify as one JSON datagram."""

import json
import os
import socket

KEYS = ("PLAYER_EVENT", "TRACK_ID", "URI", "NAME", "ARTISTS", "ALBUM", "DURATION_MS", "POSITION_MS",
        "COVERS", "ITEM_TYPE", "USER_NAME", "CLIENT_NAME", "VOLUME", "SHUFFLE", "REPEAT")

path = os.environ.get("IPODIFY_EVENT_SOCK")
if path:
    msg = json.dumps({k: os.environ[k] for k in KEYS if k in os.environ}).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(msg[:60000], path)
    except OSError:
        pass
