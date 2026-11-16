"""Chat readers. Twitch uses anonymous IRC; Kick uses the site's Pusher
websocket. Both call on_event(timestamp, username, text).
YouTube live chat is not read in v1 (audio + transcript cover it)."""

try:
    from .twitch import TwitchChat
except ImportError:  # pragma: no cover
    TwitchChat = None

try:
    from .kick import KickChat
except ImportError:  # pragma: no cover
    KickChat = None


def make_chat(platform, channel, on_event):
    """Return a started-ready chat thread (call .start()) or None."""
    if platform == "twitch" and TwitchChat:
        return TwitchChat(channel, on_event)
    if platform == "kick" and KickChat:
        return KickChat(channel, on_event)
    return None
