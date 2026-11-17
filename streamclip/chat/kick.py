"""Kick.com chat via Kick's realtime platform (Centrifugo websocket).

Kick retired its old Pusher app on 2026-10-13 ("App key not in this
cluster" forever after). The web client now negotiates a realtime
connection per channel:

  1. POST web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection
  2. the response carries a Centrifugo websocket URL (+ auth token)
  3. connect and subscribe to the same channel names as before
     (chatrooms_{chatroom_id}[_v2]) — publications still carry the old
     App\\Events\\ChatMessageEvent payloads.

Kick's REST API is sometimes protected by Cloudflare, so negotiation can
fail on datacenter IPs — in that case the thread stays off and scoring
relies on audio + transcript signals (single-signal mode in score.py).
"""

import json
import threading
import time
import uuid

import requests
import websocket

_FALLBACK_WS = "wss://realtime.us-east-1.platform.kick.com/connection/websocket"
_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://kick.com",
    "Referer": "https://kick.com/",
    "Accept": "application/json",
}


def _channel_info(channel):
    try:
        r = requests.get(f"https://kick.com/api/v2/channels/{channel}",
                         headers=_UA, timeout=15)
        r.raise_for_status()
        data = r.json()
        return int(data.get("id") or 0), int(data["chatroom"]["id"])
    except Exception:
        return 0, 0


def _negotiate(channel_id):
    """Ask Kick's realtime API for a Centrifugo (url, token) pair."""
    try:
        r = requests.post(
            f"https://web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection",
            headers=_UA, timeout=15,
            json={"client": {"id": str(uuid.uuid4()), "type": "web"},
                  "capabilities": {"accepted_providers":
                                   [{"provider": "pusher"},
                                    {"provider": "centrifugo"}]}})
        r.raise_for_status()
        data = r.json()
        conns = (data.get("data") or {}).get("connections") or []
        url, token = None, None
        for c in conns:
            if c.get("provider") == "centrifugo":
                creds = c.get("credentials") or {}
                url = creds.get("url") or url
                token = creds.get("token") or creds.get("connection_token") or token
        if url:
            return url, token
    except Exception:
        pass
    # the auth endpoint is the fallback for the connection token
    try:
        r = requests.post(
            "https://web.kick.com/api/v1/realtime/auth/connection",
            headers=_UA, timeout=15,
            json={"client": {"id": str(uuid.uuid4()), "type": "web"}})
        r.raise_for_status()
        data = r.json()
        token = ((data.get("data") or {}).get("credentials") or {}).get("token")
        return _FALLBACK_WS, token
    except Exception:
        return None, None


def _extract_chat(payload):
    """Yield (user, text) chat messages from any known frame shape."""
    # Centrifugo publication: {"push":{"channel":…,"pub":{"data":…}}}
    push = payload.get("push") if isinstance(payload, dict) else None
    if push:
        payload = (push.get("pub") or {}).get("data")
    if not isinstance(payload, dict):
        return
    event = str(payload.get("event") or "")
    if "ChatMessage" not in event and "chat.message" not in event.lower():
        return
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return
    if not isinstance(data, dict):
        return
    text = data.get("message") or data.get("content") or ""
    user = (data.get("user") or data.get("sender") or {})
    if isinstance(user, dict):
        user = user.get("username") or user.get("identity") and user["identity"].get("username") or "anon"
    if text:
        yield str(user), str(text)


class KickChat(threading.Thread):
    def __init__(self, channel, on_event, reconnect_delay=30):
        super().__init__(daemon=True)
        self.channel = channel
        self.on_event = on_event
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception:
                pass
            if not self._stop.is_set():
                time.sleep(self.reconnect_delay)

    def _session(self):
        channel_id, cid = _channel_info(self.channel)
        if not cid:
            return                    # chat unavailable — audio still covers
        url, token = _negotiate(channel_id)
        if not url:
            return
        next_id = [1]

        def _send(ws, obj):
            next_id[0] += 1
            ws.send(json.dumps({**obj, "id": next_id[0]}))

        def on_open(ws):
            cmd = {"connect": {"name": "scf", "version": "0.1"}}
            if token:
                cmd["connect"]["token"] = token
            ws.send(json.dumps({**cmd, "id": 1}))

        def on_message(ws, raw):
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return
            if not isinstance(msg, dict):
                return
            if "ping" in msg:
                try:
                    ws.send(json.dumps({"pong": {}, "id": msg.get("id")}))
                except Exception:
                    pass
                return
            if msg.get("connect"):
                for name in (f"chatrooms_{cid}.v2", f"chatrooms_{cid}"):
                    _send(ws, {"subscribe": {"channel": name}})
                return
            for user, text in _extract_chat(msg):
                self.on_event(time.time(), user, text)

        ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
        ws.run_forever(ping_interval=25)
