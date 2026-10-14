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
import signal
import stat
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
# Omarchy's notification service keeps Do Not Disturb under $HOME/.local/state
# whatever XDG_STATE_HOME says; read both, so a change on either side is safe.
NOTIFY_STATES = list(dict.fromkeys([
    Path.home() / ".local/state/omarchy/notifications.json",
    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    / "omarchy/notifications.json",
]))

DEFAULTS = {"readBody": True, "maxChars": 250, "ignoreApps": "", "readWhenLocked": False}
DEDUPE_SECONDS = 5
QUEUE_SIZE = 5
# Characters of a title or message looked at before cleanup, whatever maxChars says.
RAW_LIMIT = 10_000
PLAY_TIMEOUT = 120
# How often the quiet checks run while anything is waiting or playing.
POLL_SECONDS = 0.25
# Omarchy's own check, also used by its lock screen: asks Hyprland whether a
# session lock is held, whichever program locked it. Exits 0 locked, 1 unlocked,
# 2 undetermined.
LOCK_CHECK = "omarchy-hyprland-session-locked"
LOCK_CHECK_TIMEOUT = 2
# Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)
NOTIFY_SIGNATURE = "susssasa{sv}i"
# Temp files written by versions up to 1.0.0.
LEGACY_WAV = re.compile(r"glados-voice-\d+\.wav")

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
    for path in NOTIFY_STATES:
        try:
            if json.loads(path.read_text()).get("dnd"):
                return True
        except (OSError, ValueError, AttributeError):  # AttributeError: not an object
            continue
    return False


_lock_check = threading.Lock()


def screen_locked():
    """Anything but a clear "unlocked" counts as locked: when in doubt, stay quiet."""
    with _lock_check:  # one check at a time, however many threads ask
        try:
            check = subprocess.Popen(
                [LOCK_CHECK],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # its own process group, so a hang is killed whole
            )
        except OSError:
            return True
        try:
            return check.wait(timeout=LOCK_CHECK_TIMEOUT) != 1
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(check.pid, signal.SIGKILL)
            check.wait()
            return True


def quiet(settings):
    """Why GLaDOS has to stay silent right now, or None."""
    if muted():
        return "muted"
    if dnd_enabled():
        return "do not disturb"
    if settings["readWhenLocked"] is not True and screen_locked():
        return "screen locked"
    return None


class Hush:
    """Counts the times GLaDOS had to go quiet. Work tagged with an older count
    is dropped, even if she may speak again by the time it comes up."""

    def __init__(self):
        self.count = 0
        self.reason = None
        self.changed = threading.Condition()

    def now(self):
        with self.changed:
            return self.count

    def go_quiet(self, reason):
        with self.changed:
            self.count += 1
            self.reason = reason
            self.changed.notify_all()

    def wait(self, seen, timeout):
        """Wait up to timeout for a hush newer than `seen`; True if there was one."""
        with self.changed:
            return self.changed.wait_for(lambda: self.count != seen, timeout)


def ignored(app, settings):
    names = {n.strip().lower() for n in str(settings["ignoreApps"]).split(",")}
    return app.strip().lower() in names - {""}


def printable(name, limit=64):
    """App names come from other programs: keep them to one plain log line."""
    return "".join(c for c in name[:256] if c.isprintable())[:limit]


def clean(text):
    # <[^<>]*>, not <[^>]+>: a long run of "<" without ">" made that quadratic.
    text = re.sub(r"<[^<>]*>", " ", (text or "")[:RAW_LIMIT])
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


def remove_legacy_wavs():
    """Versions up to 1.0.0 spoke through a temp WAV that other users could
    read, left behind if the reader was stopped mid-sentence. Remove ours from
    every directory Python's tempfile could have chosen for it."""
    dirs = [os.environ.get(name) for name in ("TMPDIR", "TEMP", "TMP")]
    dirs += ["/tmp", "/var/tmp", "/usr/tmp"]
    with contextlib.suppress(OSError):
        dirs.append(os.getcwd())
    for directory in dict.fromkeys(d for d in dirs if d):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not LEGACY_WAV.fullmatch(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                    os.unlink(entry.path)
                    log(f"removed {entry.path}, left by an older version")
            except OSError as e:
                log(f"could not remove {entry.path}: {type(e).__name__}")


def feed(pipe, data):
    with contextlib.suppress(OSError):  # BrokenPipeError when cut off early
        with pipe:
            pipe.write(data)


def play(wav, sender, hush, seen):
    """Pipe the WAV to pw-play until it ends, GLaDOS has to go quiet, or time runs out."""
    # setpriv --pdeathsig: the kernel kills the player as soon as the thread that
    # started it dies. The speaker thread lives as long as the process, so
    # stopping the reader, even with SIGKILL, can never leave her talking.
    player = subprocess.Popen(
        ["setpriv", "--pdeathsig", "KILL", "pw-play", "-"], stdin=subprocess.PIPE
    )
    deadline = time.monotonic() + PLAY_TIMEOUT
    try:
        threading.Thread(target=feed, args=(player.stdin, wav), daemon=True).start()
        while player.poll() is None:
            if hush.wait(seen, POLL_SECONDS):
                log(f"[{sender}] cut off: {hush.reason}")
                return
            if time.monotonic() > deadline:
                log(f"[{sender}] cut off: timed out")
                return
    finally:  # never leave her talking, whatever went wrong
        if player.poll() is None:
            player.kill()
        player.wait()


def speaker(tts, jobs, speaking, settings, hush):
    while True:
        sender, text, seen = jobs.get()
        speaking.set()
        try:
            if hush.now() != seen:
                log(f"[{sender}] dropped: {hush.reason}")
                continue
            audio = tts.generate_speech_audio(text)
            # Build the WAV in memory and pipe it to pw-play, so notification
            # speech never touches disk where another user could read it.
            wav = io.BytesIO()
            tts.save_wav(audio, wav)
            # Things may have changed while this waited its turn.
            reason = quiet(settings.get())
            if not reason and hush.now() != seen:
                reason = hush.reason
            if reason:
                log(f"[{sender}] dropped: {reason}")
                continue
            play(wav.getvalue(), sender, hush, seen)
        except Exception as e:  # keep the worker alive; the message may quote the text
            log(f"[{sender}] speak failed: {type(e).__name__}")
        finally:
            speaking.clear()


def watch(jobs, speaking, settings, hush):
    """While anything is waiting or playing, notice when GLaDOS has to go quiet:
    cut off what is playing and drop everything still waiting."""
    while True:
        time.sleep(POLL_SECONDS)
        if jobs.empty() and not speaking.is_set():
            continue
        try:
            reason = quiet(settings.get())
        except Exception as e:  # when in doubt, stay quiet
            reason = f"check failed ({type(e).__name__})"
        if not reason:
            continue
        hush.go_quiet(reason)
        while True:
            try:
                sender, _text, _seen = jobs.get_nowait()
            except queue.Empty:
                break
            log(f"[{sender}] dropped: {reason}")


def main():
    log("loading GLaDOS models...")
    remove_legacy_wavs()
    tts = glados.TTS()
    settings = Settings()
    hush = Hush()
    speaking = threading.Event()
    jobs = queue.Queue(maxsize=QUEUE_SIZE)
    threading.Thread(
        target=speaker, args=(tts, jobs, speaking, settings, hush), daemon=True
    ).start()
    threading.Thread(target=watch, args=(jobs, speaking, settings, hush), daemon=True).start()
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
            jobs.put_nowait((sender, text, hush.now()))
            log(f"[{sender}] queued {len(text)} chars")
        except queue.Full:
            log(f"[{sender}] queue full, dropped {len(text)} chars")


if __name__ == "__main__":
    main()
