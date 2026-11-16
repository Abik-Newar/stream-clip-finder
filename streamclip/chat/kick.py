"""Kick.com chat via the site's Pusher websocket (best effort).

Kick's REST API is sometimes protected by Cloudflare, so this can fail on
datacenter IPs — in that case the thread simply stays off and scoring relies
on audio + transcript signals.
"""

import json
import threading
import time

import requests
import websocket

PUSHER_URL = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _chatroom_id(channel):
    try:
        r = requests.get(f"https://kick.com/api/v2/channels/{channel}",
                         headers=_UA, timeout=15)
        r.raise_for_status()
        return r.json()["chatroom"]["id"]
    except Exception:
        return None


class KickChat(threading.Thread):
    def __init__(self, channel, on_event, reconnect_delay=5):
        super().__init__(daemon=True)
        self.channel = channel
        self.on_event = on_event
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        cid = _chatroom_id(self.channel)
        if not cid:
            return  # chat unavailable — audio/transcript still cover detection
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    PUSHER_URL,
                    on_open=self._on_open(cid),
                    on_message=self._on_message)
                ws.run_forever(ping_interval=25)
            except Exception:
                pass
            if not self._stop.is_set():
                time.sleep(self.reconnect_delay)

    def _on_open(self, cid):
        def handler(ws):
            try:
                ws.send(json.dumps({
                    "event": "pusher:subscribe",
                    "data": {"auth": "", "channel": f"chatrooms_{cid}"},
                }))
            except Exception:
                pass
        return handler

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        event = msg.get("event")
        if event == "pusher:ping":
            try:
                ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            except Exception:
                pass
        elif event == "App\\Events\\ChatMessage":
            try:
                data = json.loads(msg["data"])
                text = data.get("message") or data.get("content") or ""
                user = (data.get("user") or {}).get("username") or "anon"
                if text:
                    self.on_event(time.time(), user, text)
            except (KeyError, json.JSONDecodeError, TypeError):
                pass
