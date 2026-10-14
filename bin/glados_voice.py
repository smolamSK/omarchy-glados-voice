"""Read desktop notifications aloud in GLaDOS's voice.

Passively monitors org.freedesktop.Notifications.Notify calls on the session
bus, so Omarchy's own notification service stays in charge of showing them.
Started by `glados-voice run`, which sets GLADOS_VOICE_DATA and
GLADOS_VOICE_STATE.
"""

import contextlib
import html
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

DATA = Path(os.environ["GLADOS_VOICE_DATA"])
STATE = Path(os.environ["GLADOS_VOICE_STATE"])
MUTED = STATE / "muted"
SETTINGS = STATE / "settings.json"
NOTIFY_STATE = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "omarchy/notifications.json"

DEFAULTS = {"readBody": True, "maxChars": 250, "ignoreApps": "", "readWhenLocked": False}
DEDUPE_SECONDS = 5
QUEUE_SIZE = 5
PLAY_TIMEOUT = 120
POLL_SECONDS = 0.5
# Omarchy's own check, also used by its lock screen: asks Hyprland whether a
# session lock is held, whichever program locked it. Exits 0 locked, 1 unlocked,
# 2 undetermined.
LOCK_CHECK = "omarchy-hyprland-session-locked"
# Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)
NOTIFY_SIGNATURE = "susssasa{sv}i"

# GLaDOS-TTS imports sounddevice for playback we don't use (PortAudio can hang
# under PipeWire); stub it so the PortAudio library is not needed at all.
sys.modules["sounddevice"] = types.ModuleType("sounddevice")
sys.path.insert(0, str(DATA / "GLaDOS-TTS"))
import glados  # noqa: E402
from jeepney import HeaderFields, MessageType  # noqa: E402
from jeepney.bus_messages import Monitoring  # noqa: E402
from jeepney.io.blocking import open_dbus_connection  # noqa: E402


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def status(msg):
    print(f"@status {msg}", flush=True)


class Settings:
    """settings.json, written by Service.qml, re-read when it changes."""

    def __init__(self):
        self.mtime = None
        self.values = dict(DEFAULTS)

    def get(self):
        try:
            mtime = SETTINGS.stat().st_mtime
        except OSError:
            return self.values
        if mtime != self.mtime:
            self.mtime = mtime
            try:
                self.values = {**DEFAULTS, **json.loads(SETTINGS.read_text())}
            except (OSError, ValueError, TypeError) as e:  # TypeError: not an object
                log(f"bad settings.json: {e!r}")
        return self.values


def muted():
    try:
        return MUTED.read_text().strip() == "1"
    except OSError:
        return False


def dnd_enabled():
    try:
        return bool(json.loads(NOTIFY_STATE.read_text()).get("dnd"))
    except (OSError, ValueError):
        return False


def screen_locked():
    """Anything but a clear "unlocked" counts as locked: when in doubt, stay quiet."""
    try:
        done = subprocess.run(
            [LOCK_CHECK],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return done.returncode != 1


def quiet(settings):
    """Why GLaDOS has to stay silent right now, or None."""
    if muted():
        return "muted"
    if dnd_enabled():
        return "do not disturb"
    if settings["readWhenLocked"] is not True and screen_locked():
        return "screen locked"
    return None


def ignored(app, settings):
    names = {n.strip().lower() for n in str(settings["ignoreApps"]).split(",")}
    return app.strip().lower() in names - {""}


def printable(name, limit=64):
    """App names come from other programs: keep them to one plain log line."""
    return "".join(c for c in name if c.isprintable())[:limit]


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def build_text(summary, body, settings):
    summary = clean(summary)
    body = clean(body) if settings["readBody"] else ""
    if summary and body:
        text = f"{summary.rstrip('.!?:')}. {body}"
    else:
        text = summary or body
    max_chars = max(50, int(settings["maxChars"]))
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."
    return text


def feed(pipe, data):
    with contextlib.suppress(OSError):  # BrokenPipeError when cut off early
        with pipe:
            pipe.write(data)


def play(wav, settings, sender):
    """Pipe the WAV to pw-play, cutting it off as soon as GLaDOS has to go quiet."""
    player = subprocess.Popen(["pw-play", "-"], stdin=subprocess.PIPE)
    threading.Thread(target=feed, args=(player.stdin, wav), daemon=True).start()
    deadline = time.monotonic() + PLAY_TIMEOUT
    try:
        while True:
            try:
                player.wait(timeout=POLL_SECONDS)
                return
            except subprocess.TimeoutExpired:
                pass
            reason = quiet(settings.get())
            if not reason and time.monotonic() > deadline:
                reason = "timed out"
            if reason:
                log(f"[{sender}] cut off: {reason}")
                return
    finally:  # never leave her talking, whatever went wrong
        if player.poll() is None:
            player.kill()
            player.wait()


def speaker(tts, jobs, settings):
    while True:
        sender, text = jobs.get()
        try:
            audio = tts.generate_speech_audio(text)
            # Build the WAV in memory and pipe it to pw-play, so notification
            # speech never touches disk where another user could read it.
            wav = io.BytesIO()
            tts.save_wav(audio, wav)
            # The screen may have locked while this waited its turn.
            reason = quiet(settings.get())
            if reason:
                log(f"[{sender}] dropped: {reason}")
                continue
            play(wav.getvalue(), settings, sender)
        except Exception as e:  # keep the worker alive; the message may quote the text
            log(f"[{sender}] speak failed: {type(e).__name__}")


def main():
    log("loading GLaDOS models...")
    tts = glados.TTS()
    settings = Settings()
    jobs = queue.Queue(maxsize=QUEUE_SIZE)
    threading.Thread(target=speaker, args=(tts, jobs, settings), daemon=True).start()
    if shutil.which(LOCK_CHECK) is None:
        log(f"{LOCK_CHECK} not found: cannot tell when the screen is locked, "
            "so GLaDOS stays quiet unless reading while locked is turned on")

    conn = open_dbus_connection(bus="SESSION")
    rule = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
    conn.send_and_get_reply(Monitoring().BecomeMonitor([rule]))
    log("monitoring notifications")
    status("ready")

    last_text, last_time = None, 0.0
    while True:
        msg = conn.receive()
        fields = msg.header.fields
        if (
            msg.header.message_type != MessageType.method_call
            or fields.get(HeaderFields.member) != "Notify"
            or fields.get(HeaderFields.signature) != NOTIFY_SIGNATURE
        ):
            continue
        app, _replaces, _icon, summary, body = msg.body[:5]
        sender = printable(app)
        try:
            current = settings.get()
            if ignored(app, current):
                continue
            text = build_text(summary, body, current)
        except Exception as e:  # keep monitoring; never log a traceback of the text
            log(f"[{sender}] skipped: {type(e).__name__}")
            continue
        now = time.monotonic()
        if not text or (text == last_text and now - last_time < DEDUPE_SECONDS):
            continue
        last_text, last_time = text, now
        reason = quiet(current)
        if reason:
            log(f"[{sender}] not read: {reason}")
            continue
        # Log only the sender and length, never the notification text itself.
        try:
            jobs.put_nowait((sender, text))
            log(f"[{sender}] queued {len(text)} chars")
        except queue.Full:
            log(f"[{sender}] queue full, dropped {len(text)} chars")


if __name__ == "__main__":
    main()
