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
import logging
import threading
import time
import uuid

import requests
import websocket

log = logging.getLogger("scf.chat")

try:                                   # requests ships certifi; use its CA
    import certifi                     # bundle so the WSS handshake works on
    _SSLOPT = {"ca_certs": certifi.where()}   # macOS/self-built Pythons too
except Exception:                      # pragma: no cover
    _SSLOPT = {}

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


def _auth_token():
    """Guest connection token for Kick's Centrifugo cluster.

    The chat/connection endpoint returns only a websocket URL; the connect
    frame still needs a JWT. Kick's web client gets it from the auth endpoint
    with `{"client_id": "<uuid>"}` — a plain `{"client": …}` body is 400."""
    r = requests.post(
        "https://web.kick.com/api/v1/realtime/auth/connection",
        headers=_UA, timeout=15,
        json={"client_id": str(uuid.uuid4())})
    r.raise_for_status()
    return ((r.json().get("data") or {}).get("token")) or None


def _negotiate(channel_id):
    """Return (websocket_url, guest_token) for Kick's realtime cluster."""
    url = _FALLBACK_WS
    try:
        r = requests.post(
            f"https://web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection",
            headers=_UA, timeout=15,
            json={"client": {"id": str(uuid.uuid4()), "type": "web"},
                  "capabilities": {"accepted_providers":
                                   [{"provider": "pusher"},
                                    {"provider": "centrifugo"}]}})
        r.raise_for_status()
        conns = (r.json().get("data") or {}).get("connections") or []
        for c in conns:
            if c.get("provider") == "centrifugo":
                url = (c.get("credentials") or {}).get("url") or url
    except Exception as exc:
        log.info("kick chat: connection lookup failed (%s) — using fallback",
                 exc)
    try:
        return url, _auth_token()
    except Exception as exc:
        log.info("kick chat: auth failed (%s)", exc)
        return url, None


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
    def __init__(self, channel, on_event, reconnect_delay=5):
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
            log.info("kick chat: %s has no chatroom id — audio only", self.channel)
            return
        url, token = _negotiate(channel_id)
        if not url or not token:
            log.info("kick chat: no realtime credentials for %s", self.channel)
            return
        # Kick's web client subscribes to a dot-namespaced channel:
        #   chatrooms.<chatroom_id>.v2
        # (the old underscore form subscribes fine but never receives).
        topic = f"chatrooms.{cid}.v2"
        next_id = [1]

        def _send(ws, obj):
            next_id[0] += 1
            ws.send(json.dumps({**obj, "id": next_id[0]},
                               separators=(",", ":")))

        def on_open(ws):
            _send(ws, {"connect": {"token": token}})

        def on_message(ws, raw):
            # Centrifugo application ping is a bare `{}`; answer with `{}`
            # or the server closes the socket with 3012 "no pong".
            if raw == "{}" or not raw:
                try:
                    ws.send("{}")
                except Exception:
                    pass
                return
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return
            if not isinstance(msg, dict):
                return
            if "ping" in msg:
                try:
                    ws.send("{}")
                except Exception:
                    pass
                return
            if "connect" in msg:
                if (msg.get("connect") or {}).get("error"):
                    log.info("kick chat: connect refused for %s: %s",
                             self.channel, msg["connect"]["error"])
                    return
                log.info("kick chat: connected %s", self.channel)
                _send(ws, {"subscribe": {"channel": topic}})
                return
            if "subscribe" in msg:
                log.info("kick chat: subscribed %s (%s)", topic, self.channel)
                return
            for user, text in _extract_chat(msg):
                self.on_event(time.time(), user, text)

        ws = websocket.WebSocketApp(
            url, on_open=on_open, on_message=on_message,
            on_error=lambda w, e: log.info("kick chat error %s: %s",
                                           self.channel, e),
            header=["Origin: https://kick.com"])
        ws.run_forever(ping_interval=0, sslopt=_SSLOPT)
