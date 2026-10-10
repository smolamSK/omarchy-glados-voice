"""Read desktop notifications aloud in GLaDOS's voice.

Passively monitors org.freedesktop.Notifications.Notify calls on the session
bus, so Omarchy's own notification service stays in charge of showing them.
Started by `glados-voice run`, which sets GLADOS_VOICE_DATA and
GLADOS_VOICE_STATE.
"""

import html
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
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

DEFAULTS = {"readBody": True, "maxChars": 250, "ignoreApps": ""}
DEDUPE_SECONDS = 5
QUEUE_SIZE = 5

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
            except (OSError, ValueError) as e:
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


def ignored(app, settings):
    names = {n.strip().lower() for n in str(settings["ignoreApps"]).split(",")}
    return app.strip().lower() in names - {""}


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


def speaker(tts, jobs):
    wav = Path(tempfile.gettempdir()) / f"glados-voice-{os.getpid()}.wav"
    while True:
        text = jobs.get()
        try:
            audio = tts.generate_speech_audio(text)
            tts.save_wav(audio, str(wav))
            subprocess.run(["pw-play", str(wav)], check=False, timeout=120)
        except Exception as e:  # keep the worker alive
            log(f"speak failed: {e!r}")
        finally:
            wav.unlink(missing_ok=True)


def main():
    log("loading GLaDOS models...")
    tts = glados.TTS()
    jobs = queue.Queue(maxsize=QUEUE_SIZE)
    threading.Thread(target=speaker, args=(tts, jobs), daemon=True).start()

    conn = open_dbus_connection(bus="SESSION")
    rule = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
    conn.send_and_get_reply(Monitoring().BecomeMonitor([rule]))
    log("monitoring notifications")
    status("ready")

    settings = Settings()
    last_text, last_time = None, 0.0
    while True:
        msg = conn.receive()
        fields = msg.header.fields
        if (
            msg.header.message_type != MessageType.method_call
            or fields.get(HeaderFields.member) != "Notify"
            or len(msg.body) < 5
        ):
            continue
        if muted() or dnd_enabled():
            continue
        app, _replaces, _icon, summary, body = msg.body[:5]
        current = settings.get()
        if ignored(app, current):
            continue
        text = build_text(summary, body, current)
        now = time.monotonic()
        if not text or (text == last_text and now - last_time < DEDUPE_SECONDS):
            continue
        last_text, last_time = text, now
        try:
            jobs.put_nowait(text)
            log(f"[{app}] {text}")
        except queue.Full:
            log(f"queue full, dropped: [{app}] {text}")


if __name__ == "__main__":
    main()
