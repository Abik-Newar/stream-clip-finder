"""Anonymous Twitch IRC chat reader — std-lib only, no API keys."""

import random
import socket
import threading
import time

HOST, PORT = "irc.chat.twitch.tv", 6667


class TwitchChat(threading.Thread):
    def __init__(self, channel, on_event, reconnect_delay=5):
        super().__init__(daemon=True)
        self.channel = channel.lower().lstrip("#")
        self.on_event = on_event            # callable(seconds, message_text)
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception:
                pass
            if not self._stop.is_set():
                time.sleep(self.reconnect_delay)

    def _connect(self):
        sock = socket.create_connection((HOST, PORT), timeout=15)
        sock.settimeout(1.0)
        sock.sendall(
            f"PASS SCHMOOPIIE\r\nNICK justinfan{random.randint(1, 99999)}\r\n"
            f"JOIN #{self.channel}\r\n".encode())
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    self._handle(sock, line.decode("utf-8", "replace"))
        finally:
            sock.close()

    def _handle(self, sock, line):
        if line.startswith("PING"):
            try:
                sock.sendall(b"PONG :tmi.twitch.tv\r\n")
            except Exception:
                pass
            return
        user = None
        if line.startswith(":"):
            user = line[1:line.find("!")] if "!" in line else None
        idx = line.find("PRIVMSG")
        if idx == -1:
            return
        rest = line[idx:]
        if not rest.startswith(f"PRIVMSG #{self.channel} "):
            return
        _, sep, text = rest.partition(" :")
        if sep and text:
            self.on_event(time.time(), user, text.strip())
