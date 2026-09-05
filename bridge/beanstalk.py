#!/usr/bin/env python3
"""
beanstalk — Telegram <-> MQTT bridge for the panel.

    user taps a button   -> sticky/from_kid -> Telegram message on your phone
    user taps a message  -> sticky/seen     -> Telegram message on your phone
    you type in Telegram -> sticky/to_kid   -> the panel

Runs on the Pi. Long-polls Telegram and holds an outbound MQTT connection, so
nothing needs an inbound port and nothing needs forwarding — same reasoning as
the device itself.

Two things here are load-bearing and easy to get wrong if you edit this later:

  MQTT_CLIENT_ID must differ from the device's. The device uses 'sticky1234'.
  Two clients sharing an id do not coexist — the broker kicks the first one off,
  so a collision here would knock the panel offline every time this service
  starts.

  clean_session is False. The broker then holds QoS 1 messages for this client
  while it is down, so a reply tapped during a bridge restart still arrives.
  Without it, that reply is lost.
"""

import html
import logging
import secrets
import re
import os
import ssl
import sys
import threading
import time

import paho.mqtt.client as mqtt
import requests

# --------------------------------------------------------------------------
# Config, all from the environment. See beanstalk.env.
# --------------------------------------------------------------------------


def _need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"beanstalk: {name} is not set. See beanstalk.env.")
    return v


TELEGRAM_TOKEN = _need("TELEGRAM_TOKEN")

# One chat talks to the panel. Any other chat is ignored and logged.
# Without this, anyone who finds the bot can put text on the panel.
#
# Set TELEGRAM_CHAT_ID to pin it. Leave it blank and the bridge pairs on first
# run: it prints a code, and the first chat that sends that code back becomes
# the owner, permanently. The code lives in the log and nowhere else.
# systemd creates this via StateDirectory= in beanstalk.service and keeps it
# writable under ProtectSystem=strict. Nowhere else is.
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/beanstalk")
_CHAT_FILE = os.path.join(STATE_DIR, "chat_id")
_CODE_FILE = os.path.join(STATE_DIR, "pairing_code")
# When the panel last spoke, as a unix time. Kept on disk so a bridge restart
# does not reset "last heard" to the restart, and so a panel that was already
# silent is reported at its true age.
_SEEN_FILE = os.path.join(STATE_DIR, "last_seen")

_PAIR_WORDS = ("amber anchor badger bamboo beacon cedar cobalt comet copper "
               "cricket delta ember falcon fennel garnet ginger granite "
               "harbor hazel indigo juniper lantern lichen magnet maple "
               "marble meadow nectar nutmeg olive onyx opal orbit otter "
               "pebble pepper pollen poppy quartz quince radish raven ribbon "
               "saffron sage shale silver sorrel spruce sumac teak thistle "
               "thyme topaz umber velvet walnut willow yarrow zinnia").split()


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write(path, value):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(str(value))
    os.replace(tmp, path)


def _load_chat_id():
    env = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env:
        return int(env)
    saved = _read(_CHAT_FILE)
    return int(saved) if saved else None


def _pairing_code():
    """Stable across restarts. A code that changed on every restart would
    invalidate the one the person is already holding."""
    saved = _read(_CODE_FILE)
    if saved:
        return saved
    code = "%s-%04d" % (secrets.choice(_PAIR_WORDS).upper(),
                        secrets.randbelow(10000))
    _write(_CODE_FILE, code)
    return code


ALLOWED_CHAT_ID = _load_chat_id()

MQTT_HOST = _need("MQTT_HOST")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = _need("MQTT_USER")
MQTT_PASS = _need("MQTT_PASS")

PREFIX = os.environ.get("TOPIC_PREFIX", "sticky")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "beanstalk")

TOPIC_TO_KID = f"{PREFIX}/to_kid"
TOPIC_FROM_KID = f"{PREFIX}/from_kid"
TOPIC_SEEN = f"{PREFIX}/seen"
TOPIC_BATTERY = f"{PREFIX}/sensor/battery_level/state"
# Unix time of the panel's last battery publish, retained by the panel. The
# only timestamp that survives on the broker; read at start.
TOPIC_HEARTBEAT = f"{PREFIX}/heartbeat"
# The device publishes its current triad here, retained. It is the source of
# truth: the labels live in the device's flash and survive reboots, while this
# service does not, so a copy kept here would go stale on the first restart.
TOPIC_BUTTONS = f"{PREFIX}/buttons"
# Joke bundles. Retained. The device holds them in RAM only; the broker
# redelivers after every device reboot. An empty
# payload means fall back to the twenty compiled into the firmware.
TOPIC_JOKES = f"{PREFIX}/jokes"
# What the device is showing: a pushed bundle if there is one, the twenty
# built into the firmware otherwise. Read only. /joke appends to this list.
TOPIC_JOKES_ACTIVE = f"{PREFIX}/jokes_active"
# Remote reboot. When the panel is somewhere you are not, a reboot is often
# the only fix. The device exposes this topic whether or not the bridge uses it.
#
# The topic comes from the button's NAME in sticky.yaml. Renaming that entity
# breaks this.
TOPIC_RESTART = f"{PREFIX}/button/restart_device/command"

# Remote firmware update. /update puts a URL on TOPIC_OTA, retained, and the
# panel pulls it. The panel answers on TOPIC_OTA_RESULT (pulling, flashed,
# failed N) and publishes TOPIC_VERSION, retained, after every connect.
TOPIC_OTA = f"{PREFIX}/ota"
TOPIC_OTA_RESULT = f"{PREFIX}/ota_result"
TOPIC_VERSION = f"{PREFIX}/version"
# The only host /update accepts. Blank disables /update.
OTA_HOST = os.environ.get("OTA_HOST", "").strip().lower()
# Lines of "<version> <folder>", appended by publish-firmware over ssh. The
# folder name is random and stays out of Telegram; /update <version> looks it
# up here. Last line for a version wins.
_RELEASES_FILE = os.path.join(STATE_DIR, "releases")

# One MQTT message has to carry the whole bundle, and the device parses it into
# RAM on every joke render. Twenty at about 90 bytes each is 1.8KB. Check the
# device still renders before raising either.
MAX_JOKES = 20
MAX_JOKES_BYTES = 2000

# Labels are refused, not truncated. The full text is what gets published to
# from_kid, so a cut label would send words the user did not see.
#
# The device handles this by dropping the label from 32px to 20px when it does
# not fit, which keeps the whole thing on screen. This cap is the floor below
# that: 20px in a 400px pill holds about 36 characters, and past that even the
# smaller size would truncate. It is a character-count proxy for a real text
# measurement, so it is set conservatively.
MAX_LABEL_CHARS = 32

# Named sets. Edit freely. Keys are what you type after /buttons, so keep them
# short.
#
# No "default" entry. /buttons default sends a bare "?" and the device applies
# the three it holds in firmware, so that set lives in one place.
PRESETS = {
    "answer":  ["yes", "no", "not yet"],
    "silly":   ["fart", "poop", "you smell"],
}

# The device publishes battery every 300s, which makes a free heartbeat. Silence
# for longer than this means the device is off, flat, or off the network. At
# the panel that looks like no activity.
SILENT_MINUTES = int(os.environ.get("DEVICE_SILENT_MINUTES", "20"))
LOW_BATTERY_PCT = float(os.environ.get("LOW_BATTERY_PCT", "15"))

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("beanstalk")


def check_credentials():
    """Say what was loaded, so a bad paste is a one-line answer rather
    than a guessing game. Never log a secret or a piece of one."""
    tok = TELEGRAM_TOKEN
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", tok):
        bad = [i for i, c in enumerate(tok) if not (c.isalnum() or c in ":_-")]
        log.error("TELEGRAM_TOKEN is not shaped like a bot token: expected "
                  "<digits>:<about 35 characters>, got length %d%s",
                  len(tok), f" with unexpected characters at {bad[:6]}" if bad else "")
    else:
        log.info("TELEGRAM_TOKEN shape OK")

    log.info("MQTT host=%r user=%r client_id=%r", MQTT_HOST, MQTT_USER, MQTT_CLIENT_ID)
    for name, val in (("MQTT_HOST", MQTT_HOST), ("MQTT_USER", MQTT_USER),
                      ("MQTT_PASS", MQTT_PASS)):
        odd = [(i, hex(ord(c))) for i, c in enumerate(val)
               if ord(c) < 32 or ord(c) > 126]
        if odd:
            log.error("%s contains non-printable or non-ASCII characters at %s "
                      "— almost always a copy-paste artefact.", name, odd[:6])

# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------

state_lock = threading.Lock()
def _load_last_seen():
    """None when nothing is known: no file yet, and no retained heartbeat
    seen. tg_status says so rather than showing the restart time."""
    try:
        return float(_read(_SEEN_FILE))
    except ValueError:
        return None


STARTED_AT = time.time()

state = {
    "last_device_msg": _load_last_seen(),  # any live message from the panel
    "last_seen_written": 0.0,   # throttle for _SEEN_FILE
    "device_alerted": False,    # have we already said it went quiet
    "battery_alerted": False,   # have we already said the battery is low
    "mqtt_up": False,
    "mqtt_down_since": 0.0,
    "mqtt_alerted": False,
    "last_battery": None,
    "buttons": None,            # from TOPIC_BUTTONS, the device's own copy
    "jokes": [],                # from TOPIC_JOKES_ACTIVE, what is on screen
    "version": None,            # from TOPIC_VERSION
    "ota_pending": None,        # URL on TOPIC_OTA, until flashed or failed
    "ota_pulling": False,       # the panel said "pulling"; a version publish
                                # after that means it rebooted
}

# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def tg_send(text, quiet=False):
    """Send to the one allowed chat. Never raises — a dead Telegram must not
    take the MQTT side down with it."""
    if ALLOWED_CHAT_ID is None:
        # Not paired yet. There is no chat to send to.
        log.info("not paired, dropping outbound message (%d chars)", len(text))
        return
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": ALLOWED_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": quiet,
            },
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("telegram send failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("telegram send error: %s", e)


HELP = (
    "<b>Beanstalk</b>\n"
    "Anything you type goes to the panel.\n\n"
    "<b>Reply buttons</b>\n"
    "<code>/buttons</code> — show what is on the screen now\n"
    "<code>/buttons ans1, ans2, ans3</code> — set your own\n"
    "<code>/buttons silly</code> — use a saved set\n"
    "<code>/buttons default</code> — back to the built-in three\n\n"
    "They stay set until you change them. An ordinary message does not "
    "disturb them. Separate with a comma or a <code>|</code>.\n\n"
    "Fewer than three leaves the extras blank and inert. Emoji work, but only "
    "the ones built into the firmware.\n\n"
    "You can also end any message with a <code>?</code> line, or send "
    "<code>?a, b, c</code> on its own — same thing, no autocomplete.\n\n"
    "<b>Jokes</b>\n"
    "Send a <b>.txt</b>, one joke per line, to replace the set on the panel. "
    "<code>|</code> inside a line is a break on the screen.\n\n"
    "<code>/jokes</code> — what is loaded\n"
    "<code>/joke &lt;text&gt;</code> — add one\n"
    "<code>/jokes default</code> — back to the twenty in the firmware\n\n"
    "/status — link, battery and firmware\n"
    "/reboot — restart the panel\n"
    "/update &lt;url&gt; — update the panel's firmware"
)

# Paste into BotFather /setcommands so Telegram autocompletes these. Without
# it the commands still work, typed in full.
BOTFATHER_COMMANDS = (
    "buttons - set the three reply buttons\n"
    "jokes - show or replace the joke bundle\n"
    "joke - add one joke\n"
    "reboot - restart the panel\n"
    "update - update the panel's firmware\n"
    "status - link, battery and firmware\n"
    "help - how this works"
)


# --------------------------------------------------------------------------
# Joke bundles
# --------------------------------------------------------------------------


def publish_jokes(client, jokes):
    """Replace the bundle. Returns (ok, kept, message)."""
    jokes = [j.strip() for j in jokes if j.strip()]
    # state["jokes"] is not updated here. The device republishes what it is
    # actually showing on TOPIC_JOKES_ACTIVE, and that is the only thing that
    # sets it. A rejected or clipped publish then cannot leave a stale copy here.
    dropped_count = max(0, len(jokes) - MAX_JOKES)
    jokes = jokes[:MAX_JOKES]

    # Trim to the byte budget too. Twenty long ones can overrun the single MQTT
    # message the device parses.
    payload = "\n".join(jokes)
    dropped_size = 0
    while len(payload.encode("utf-8")) > MAX_JOKES_BYTES and jokes:
        jokes.pop()
        dropped_size += 1
        payload = "\n".join(jokes)

    info = client.publish(TOPIC_JOKES, payload.encode("utf-8"), qos=1,
                          retain=True)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        log.warning("publish to jokes failed rc=%s", info.rc)
        return False, 0, "Could not reach the broker. Jokes unchanged."

    if not jokes:
        return True, 0, "Jokes back to the twenty built into the firmware."
    note = f"{len(jokes)} joke{'s' if len(jokes) != 1 else ''} loaded."
    if dropped_count:
        note += f" {dropped_count} over the limit of {MAX_JOKES} dropped."
    if dropped_size:
        note += f" {dropped_size} dropped to fit {MAX_JOKES_BYTES} bytes."
    return True, len(jokes), note


def fetch_document(file_id):
    """Download a Telegram document. Returns text, or raises."""
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id},
                     timeout=20)
    r.raise_for_status()
    info = r.json()["result"]
    size = info.get("file_size", 0)
    if size > 64 * 1024:
        raise ValueError(f"file is {size} bytes; keep it under 64KB")
    path = info["file_path"]
    d = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}",
        timeout=30)
    d.raise_for_status()
    return d.content.decode("utf-8", errors="replace")


def tg_status():
    with state_lock:
        last = state["last_device_msg"]
        batt = state["last_battery"]
        up = state["mqtt_up"]
        ver = state["version"]
        pending = state["ota_pending"]
    if last:
        age = int(time.time() - last)
        heard = f"{age // 60}m {age % 60}s ago" if age >= 60 else f"{age}s ago"
    else:
        age = int(time.time() - STARTED_AT)
        heard = f"not since the bridge started, {age // 60}m ago"
    out = (
        f"broker: {'connected' if up else 'DISCONNECTED'}\n"
        f"last heard from Sticky: {heard}\n"
        f"battery: {batt if batt is not None else 'unknown'}\n"
        f"firmware: {ver if ver else 'unknown'}"
    )
    if pending:
        out += "\nupdate queued, waiting for the panel"
    return out


def _releases():
    """{version: folder} from the file publish-firmware maintains."""
    out = {}
    try:
        with open(_RELEASES_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9._-]+", parts[1]):
                    out[parts[0].lstrip("v")] = parts[1]
    except OSError:
        pass
    return out


def _clear_ota(client):
    """Remove the retained URL so a reconnecting panel does not see it."""
    client.publish(TOPIC_OTA, b"", qos=1, retain=True)
    with state_lock:
        state["ota_pending"] = None
        state["ota_pulling"] = False


def telegram_loop(client):
    """Long-poll getUpdates. Runs forever; every error is caught and retried."""
    offset = None
    backoff = 1
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"timeout": 50, "offset": offset},
                timeout=70,
            )
            if r.status_code != 200:
                raise RuntimeError(f"getUpdates {r.status_code}: {r.text[:200]}")
            backoff = 1
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(client, upd)
                except Exception:
                    log.exception("update %s not handled", upd.get("update_id"))
        except Exception as e:
            log.warning("telegram poll error: %s (retry in %ss)", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def handle_update(client, upd):
    # An edited message is handled like a new one, so fixing a typo in
    # Telegram puts the corrected text on the panel.
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text")

    global ALLOWED_CHAT_ID

    if ALLOWED_CHAT_ID is None:
        # Pairing. The code was printed to the log at startup and exists
        # nowhere else, so whoever can read the log owns the panel. One match
        # and this is settled for good: there is no unpair, short of deleting
        # the state file on the Pi.
        code = _pairing_code()
        if (text or "").strip().upper() == code:
            ALLOWED_CHAT_ID = chat_id
            _write(_CHAT_FILE, chat_id)
            log.warning("paired to chat %s", chat_id)
            tg_send("Paired. This chat is now the only one I answer.")
        else:
            log.warning("unpaired: chat %s sent something that is not the "
                        "pairing code", chat_id)
        return

    if chat_id != ALLOWED_CHAT_ID:
        # Someone else found the bot. Do not answer them, do not let them near
        # the device, but do record it.
        log.warning("ignoring message from unauthorised chat %s", chat_id)
        return

    doc = msg.get("document")
    if doc:
        # A .txt of jokes, one per line. The bulk path. The bundle stays a file
        # you can edit and resend.
        name = doc.get("file_name", "")
        if not name.lower().endswith((".txt", ".text", ".md")):
            tg_send(f"Not sure what to do with <b>{html.escape(name)}</b>. "
                    "Send a .txt of jokes, one per line.")
            return
        try:
            body = fetch_document(doc["file_id"])
        except Exception as e:
            log.warning("document fetch failed: %s", e)
            tg_send(f"Could not read that file: {html.escape(str(e))}")
            return
        ok, n, note = publish_jokes(client, body.splitlines())
        tg_send(note)
        return

    if not text:
        tg_send("Text only — the screen can't show anything else.")
        return

    cmd = text.strip().lower()
    if cmd in ("/start", "/help"):
        tg_send(HELP)
        return
    if cmd == "/status":
        tg_send(tg_status())
        return
    if cmd == "/commands":
        # For pasting into BotFather. Not sent to the panel.
        tg_send("Paste into BotFather /setcommands:\n\n<code>"
                + BOTFATHER_COMMANDS + "</code>")
        return

    if cmd == "/update" or cmd.startswith("/update "):
        arg = text.strip()[len("/update"):].strip()
        with state_lock:
            ver = state["version"]
            pending = state["ota_pending"]
        if not OTA_HOST:
            tg_send("No OTA_HOST in beanstalk.env, so /update is off.")
            return
        if not arg:
            known = sorted(_releases(), key=lambda v: (len(v), v))
            avail = ("published: " + ", ".join(known[-5:])) if known else \
                    "nothing published yet"
            if pending:
                tail = ("queued, waiting for the panel. "
                        "<code>/update cancel</code> to withdraw it.")
            elif known:
                tail = f"<code>/update {known[-1]}</code> to queue one."
            else:
                tail = ""
            tg_send(f"firmware: {ver if ver else 'unknown'}\n{avail}\n{tail}")
            return
        if arg.lower() == "cancel":
            _clear_ota(client)
            tg_send("Update withdrawn." if pending else "Nothing was queued.")
            return
        # A version number, as publish-firmware prints it. A full URL on the
        # host is also accepted.
        m = re.fullmatch(r"v?(\d{1,6})", arg)
        if m:
            folder = _releases().get(m.group(1))
            if not folder:
                tg_send(f"No version {m.group(1)} registered on the Pi. "
                        "publish-firmware registers each build it publishes.")
                return
            url = f"https://{OTA_HOST}/{folder}/firmware.ota.bin"
        else:
            url = arg.split()[0]
            if not url.lower().startswith(f"https://{OTA_HOST}/") \
                    or not url.endswith("firmware.ota.bin") or " " in arg:
                tg_send("A version number, as in <code>/update 35</code>, or "
                        f"a <code>https://{OTA_HOST}/.../firmware.ota.bin</code> "
                        "URL.")
                return
        info = client.publish(TOPIC_OTA, url.encode("utf-8"), qos=1, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish to ota failed rc=%s", info.rc)
            tg_send("Could not reach the broker, so nothing was queued.")
            return
        with state_lock:
            state["ota_pending"] = url
            state["ota_pulling"] = False
        log.info("-> ota queued")
        tg_send("Update queued. The panel pulls it within 15 s of being online, "
                "shows <b>updating</b>, and reboots. You hear back here either "
                f"way.\nNow on firmware {ver if ver else 'unknown'}.")
        return

    if cmd in ("/reboot", "/restart"):
        info = client.publish(TOPIC_RESTART, b"PRESS", qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish to restart failed rc=%s", info.rc)
            tg_send("Could not reach the broker, so nothing was sent.")
            return
        log.info("-> restart")
        tg_send("Reboot sent. It goes quiet for about twenty seconds, then the "
                "screen refreshes. Messages, buttons and jokes all survive.\n\n"
                "If it does not come back, the link was already down and the "
                "command never arrived — nothing here can fix that remotely.")
        return

    if cmd == "/jokes" or cmd.startswith("/jokes "):
        arg = text.strip()[len("/jokes"):].strip()
        if arg.lower() in ("default", "reset", "firmware"):
            ok, n, note = publish_jokes(client, [])
            tg_send(note)
            return
        if not arg:
            with state_lock:
                jokes = list(state["jokes"])
            if jokes:
                preview = "\n".join("• " + html.escape(j[:60]) for j in jokes[:5])
                more = f"\n… and {len(jokes) - 5} more" if len(jokes) > 5 else ""
                tg_send(f"<b>{len(jokes)}</b> on the device.\n{preview}{more}\n\n"
                        "Send a .txt, one joke per line, to replace them. "
                        "<code>/joke &lt;text&gt;</code> adds one and drops the "
                        f"oldest past {MAX_JOKES}. "
                        "<code>/jokes default</code> goes back to the built-in "
                        "twenty.")
            else:
                tg_send("Not heard from the device yet — it reports its jokes a "
                        "few seconds after connecting.")
            return
        tg_send("<code>/jokes</code> shows the set, <code>/jokes default</code> "
                "restores the built-in twenty. To replace the whole set send a "
                ".txt file; to add one, <code>/joke &lt;text&gt;</code>.")
        return

    if cmd == "/joke" or cmd.startswith("/joke "):
        one = text.strip()[len("/joke"):].strip()
        if not one:
            tg_send("<code>/joke What is invisible and | smells like carrots? | "
                    "Bunny farts.</code>\n\n"
                    "<code>|</code> is a line break on the screen. Adds to the "
                    "current bundle; the oldest drops off at "
                    f"{MAX_JOKES}.")
            return
        with state_lock:
            jokes = list(state["jokes"])
        if not jokes:
            tg_send("Have not heard the current set from the device yet. It "
                    "reports in a few seconds after connecting — try again, or "
                    "check <code>/status</code>.")
            return
        # Appends to whatever is on the device, built-in twenty included, and
        # rolls the oldest off the end.
        dropped = jokes[0] if len(jokes) >= MAX_JOKES else None
        jokes.append(one)
        if len(jokes) > MAX_JOKES:
            jokes = jokes[-MAX_JOKES:]
        ok, n, note = publish_jokes(client, jokes)
        if ok and dropped:
            note += f"\nDropped the oldest: {html.escape(dropped[:60])}"
        tg_send(note)
        return

    # /buttons wraps the "?" line the firmware parses. Telegram autocompletes
    # registered commands, so on a phone it is "/bu" and tab.
    if cmd == "/buttons" or cmd.startswith("/buttons "):
        spec = text.strip()[len("/buttons"):].strip()

        # Bare /buttons shows. Resetting is /buttons default, spelled out, so a
        # bare command sent by mistake changes nothing.
        if not spec:
            with state_lock:
                cur = state["buttons"]
            body = (f"Buttons now: <b>{html.escape(cur)}</b>" if cur
                    else "Not heard from the device yet — it publishes its "
                         "buttons when it connects.")
            tg_send(body + "\n\nSets: <code>" +
                    "</code>  <code>".join(sorted(PRESETS)) +
                    "</code>  <code>default</code>\n"
                    "<code>/buttons ans1, ans2, ans3</code> for your own.")
            return

        key = spec.lower()
        if key in ("default", "reset"):
            # Bare "?" — the device applies the defaults it holds in firmware.
            spec = ""
        elif key in PRESETS:
            spec = ", ".join(PRESETS[key])
        elif not re.search(r"[|,]", spec):
            # One word, not a preset. Usually a typo or a misremembered set
            # name. Setting one button and blanking the other two is refused.
            tg_send(f"No set called <b>{html.escape(spec)}</b>. Try: <code>"
                    + "</code> <code>".join(sorted(PRESETS))
                    + "</code>\nFor one label only, add a comma: "
                      f"<code>/buttons {html.escape(spec)},</code>")
            return

        too_long = [p.strip() for p in re.split(r"[|,]", spec)
                    if len(p.strip()) > MAX_LABEL_CHARS]
        if too_long:
            tg_send("Too long for the button even at the smaller size, and a "
                    "cut label sends words the user never read:\n"
                    + "\n".join(f"• {html.escape(t)} ({len(t)})"
                                for t in too_long)
                    + f"\n\nKeep each under {MAX_LABEL_CHARS} characters.")
            return

        payload = "?" + spec
        info = client.publish(TOPIC_TO_KID, payload.encode("utf-8"), qos=1,
                              retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish to to_kid failed rc=%s", info.rc)
            tg_send("Could not reach the broker. Buttons not changed.")
            return
        log.info("-> to_kid, %d bytes", len(payload))
        log.debug("-> to_kid: %r", payload)
        if spec:
            labels = [p.strip() for p in re.split(r"[|,]", spec) if p.strip()]
            tg_send("Buttons: " + html.escape(" | ".join(labels[:3])))
        else:
            tg_send("Buttons back to the device defaults.")
        return

    # A mistyped or unknown command must NOT land on the panel. Telegram
    # autocompletes, but it also lets you send anything, and a bridge that does
    # not recognise a command must refuse it rather than forward it.
    #
    # "//" is the escape for a message that really does start with a slash.
    stripped = text.strip()
    if stripped.startswith("//"):
        text = stripped[1:]
    elif stripped.startswith("/"):
        word = stripped.split()[0]
        tg_send(f"Don't know <b>{html.escape(word)}</b>, so nothing went to the "
                "screen.\n\n<code>/buttons</code> <code>/jokes</code> "
                "<code>/joke</code> <code>/reboot</code> "
                "<code>/status</code> <code>/help</code>\n\n"
                "To send a message that starts with a slash, double it: "
                "<code>//like this</code>.")
        return

    # Anything else goes to the screen verbatim, including any ?a|b|c line.
    info = client.publish(TOPIC_TO_KID, text.encode("utf-8"), qos=1, retain=False)
    if info.rc == mqtt.MQTT_ERR_SUCCESS:
        log.info("-> to_kid, %d bytes", len(text))
        log.debug("-> to_kid: %r", text)
    else:
        log.warning("publish to to_kid failed rc=%s", info.rc)
        tg_send("Could not reach the broker. Message not sent — try again.")


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------


def _failed(rc):
    """paho 2.x hands back a ReasonCode, 1.x an int."""
    if hasattr(rc, "is_failure"):
        return rc.is_failure
    return rc != 0


def on_connect(client, userdata, flags, rc, properties=None):
    if _failed(rc):
        log.error("mqtt connect failed: %s", rc)
        return
    log.info("mqtt connected")
    with state_lock:
        was_down = not state["mqtt_up"] and state["mqtt_alerted"]
        state["mqtt_up"] = True
        state["mqtt_down_since"] = 0.0
        state["mqtt_alerted"] = False
    # QoS 1 with clean_session False is what makes the broker hold messages for
    # this bridge while it is restarting.
    for t in (TOPIC_FROM_KID, TOPIC_SEEN, TOPIC_BATTERY, TOPIC_BUTTONS,
              TOPIC_JOKES_ACTIVE, TOPIC_OTA_RESULT, TOPIC_VERSION, TOPIC_OTA,
              TOPIC_HEARTBEAT):
        client.subscribe(t, qos=1)
    if was_down:
        tg_send("Beanstalk is back on the broker.")


def on_disconnect(client, userdata, *args):
    # paho 1.x passes (rc); 2.x passes (flags, reason_code, properties). Taking
    # them positionally logs the wrong field on one of the two, so pick the
    # first argument that looks like a reason code.
    reason = next(
        (a for a in args if hasattr(a, "is_failure") or isinstance(a, int)), args
    )
    log.warning("mqtt disconnected: %s", reason)
    with state_lock:
        state["mqtt_up"] = False
        if not state["mqtt_down_since"]:
            state["mqtt_down_since"] = time.time()


def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8", "replace").strip()
    log.info("<- %s, %d bytes%s", msg.topic, len(payload),
             " (retained)" if msg.retain else "")
    log.debug("<- %s: %r", msg.topic, payload)

    # Our own retained URL, echoed back. Tells a restarted bridge what is
    # queued. Not the panel speaking, so it sits above the heartbeat below.
    if msg.topic == TOPIC_OTA:
        with state_lock:
            state["ota_pending"] = payload or None
        return

    # The panel's own clock, retained. On a retained replay it is the only
    # source for "last heard" a fresh bridge has; live, it is a heartbeat like
    # any other message and the block below handles that.
    if msg.topic == TOPIC_HEARTBEAT and msg.retain:
        try:
            ts = float(payload)
        except ValueError:
            return
        with state_lock:
            last = state["last_device_msg"]
            if last is None or ts > last:
                state["last_device_msg"] = ts
        return

    # A retained message is the broker replaying its last copy, not the panel
    # speaking. It arrives on every (re)connect, so counting it as a heartbeat
    # would reset the silence alert each time this bridge reconnects.
    if not msg.retain:
        now = time.time()
        with state_lock:
            was_alerted = state["device_alerted"]
            state["last_device_msg"] = now
            state["device_alerted"] = False
            persist = now - state["last_seen_written"] > 60
            if persist:
                state["last_seen_written"] = now
        if persist:
            try:
                _write(_SEEN_FILE, int(now))
            except OSError as e:
                log.warning("could not write %s: %s", _SEEN_FILE, e)
        if was_alerted:
            tg_send("Sticky is back online.")

    if msg.topic == TOPIC_JOKES_ACTIVE:
        jokes = [ln.strip() for ln in payload.splitlines() if ln.strip()]
        with state_lock:
            state["jokes"] = jokes
        log.info("   %d jokes on the device", len(jokes))
        return

    if msg.topic == TOPIC_BUTTONS:
        with state_lock:
            state["buttons"] = payload
        return

    if msg.topic == TOPIC_VERSION:
        with state_lock:
            before = state["version"]
            state["version"] = payload
            pulling = state["ota_pulling"]
        if pulling:
            # The panel reconnected after saying "pulling": it rebooted, so
            # the flash completed even if "flashed" was lost in the reboot.
            _clear_ota(client)
            tg_send(f"Panel is back on firmware {html.escape(payload)}.")
        elif before is not None and payload != before:
            tg_send(f"Panel is on firmware {html.escape(payload)}.")
        return

    if msg.topic == TOPIC_OTA_RESULT:
        if payload == "pulling":
            with state_lock:
                state["ota_pulling"] = True
            tg_send("Panel is pulling the update.", quiet=True)
        elif payload == "flashed":
            _clear_ota(client)
            tg_send("Flashed. The panel is rebooting.")
        elif payload.startswith("failed"):
            _clear_ota(client)
            tg_send(f"Update failed ({html.escape(payload)}). The panel kept "
                    "the old firmware.")
        else:
            log.warning("unknown ota_result %r", payload)
        return

    if msg.topic == TOPIC_FROM_KID:
        tg_send(f"<b>{html.escape(payload)}</b>")

    elif msg.topic == TOPIC_SEEN:
        tg_send("(message read)", quiet=True)

    elif msg.topic == TOPIC_BATTERY:
        try:
            pct = float(payload)
        except ValueError:
            return
        with state_lock:
            state["last_battery"] = f"{pct:.0f}%"
            alerted = state["battery_alerted"]
            if pct < LOW_BATTERY_PCT and not alerted:
                state["battery_alerted"] = True
                notify = True
            elif pct >= LOW_BATTERY_PCT + 10 and alerted:
                # Hysteresis, so a reading hovering at the threshold does not
                # send a message every five minutes.
                state["battery_alerted"] = False
                notify = False
            else:
                notify = False
        if notify:
            tg_send(f"Sticky battery is at {pct:.0f}%. It needs charging.")


# --------------------------------------------------------------------------
# Watchdog
#
# A bridge that dies silently takes the notifications with it. This is the
# bridge-side counterpart of the device's sync line.
# --------------------------------------------------------------------------


def watchdog():
    while True:
        time.sleep(60)
        now = time.time()
        with state_lock:
            last = state["last_device_msg"]
            if last is None:
                last = STARTED_AT
            dev_alerted = state["device_alerted"]
            up = state["mqtt_up"]
            down_since = state["mqtt_down_since"]
            mqtt_alerted = state["mqtt_alerted"]

            device_quiet = (
                not dev_alerted and (now - last) > SILENT_MINUTES * 60
            )
            if device_quiet:
                state["device_alerted"] = True

            broker_lost = (
                not up and down_since and not mqtt_alerted
                and (now - down_since) > 300
            )
            if broker_lost:
                state["mqtt_alerted"] = True

        if device_quiet:
            age = int(now - last) // 60
            since = (f"{age} minutes" if age < 120 else
                     f"{age // 60} hours" if age < 2880 else
                     f"{age // 1440} days")
            tg_send(
                f"No word from Sticky for {since}. "
                "It is powered off, flat, or off wifi. "
                "The panel shows the last message either way."
            )
        if broker_lost:
            tg_send("Beanstalk has lost the MQTT broker for 5 minutes.")


# --------------------------------------------------------------------------


def build_client():
    kwargs = dict(client_id=MQTT_CLIENT_ID, clean_session=False)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
    except AttributeError:
        client = mqtt.Client(**kwargs)  # paho 1.x
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set_context(ssl.create_default_context())
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def main():
    if MQTT_CLIENT_ID == "sticky1234":
        sys.exit(
            "beanstalk: MQTT_CLIENT_ID collides with the device's id. "
            "That would knock the panel offline. Pick another."
        )

    check_credentials()

    client = build_client()
    log.info("connecting to %s:%s as %s", MQTT_HOST, MQTT_PORT, MQTT_CLIENT_ID)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    threading.Thread(target=watchdog, daemon=True).start()

    if ALLOWED_CHAT_ID is None:
        # install-beanstalk greps the log for this exact string. Change it here
        # and you must change it there.
        log.warning("Not paired. PAIRING CODE: %s — send it to the bot in "
                    "Telegram and that chat becomes the only one I answer.",
                    _pairing_code())
    else:
        log.info("paired to chat %s", ALLOWED_CHAT_ID)
        tg_send("Beanstalk is up.", quiet=True)

    telegram_loop(client)


if __name__ == "__main__":
    main()
