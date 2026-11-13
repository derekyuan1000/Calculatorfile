# G-Intelligence: Seeed XIAO ESP32-S3 version (Hack Club AI only).
# Runs MicroPython. Connects to WiFi, reads newline-terminated queries from
# the calculator over UART, asks Hack Club AI (ai.hackclub.com), and writes
# the reply back over UART.
#
# Besides plain queries, the calculator's Settings menu sends these commands:
#   SET_MODEL:<name>      -- Hack Club AI model id, e.g. anthropic/claude-sonnet-5.5
#   SET_SSID:<ssid>       -- start adding/updating a network (remembered until SET_PASS)
#   SET_PASS:<password>   -- save the password for that SSID in the known list
#   SET_JOIN:<ssid>       -- connect to one saved network
#   SET_FORGET:<ssid>     -- remove a saved network
#   SET_CONNECT:1         -- auto-connect: strongest saved network that is in range
#   SET_NEWCHAT:1         -- forget the conversation so far (start a new chat)
#   GET_ALL               -- reply OK:model|saved_count|wifi(1/S/0)|connected_ssid
#   GET_NETS              -- scan; reply OK:saved|connected|visible|secured|ssid~...
#   GET_TEST              -- check WiFi, DNS and a tiny Hack Club AI call
#   GET_DIA:<question>    -- diagram: reply DIA:<drawing commands> (see below)
#   GET_UPDATE            -- download a new main.py from UPDATE_URL (.env),
#                            check it, swap it in and restart (see do_update)
#   GET_SNAP              -- take a photo: reply SNAP:256x192:<base64 4-bit greys>
#   GET_ASKSNAP:<q>       -- ask about the photo (text reply)
#   GET_DIASNAP:<q>       -- diagram from the photo (DIA: reply)
# Saved networks (like a phone) and the model live in settings.json on the
# XIAO, so they survive a power cycle. WiFi joins on the first question (the
# strongest access point of the strongest saved network) and is switched off
# again WIFI_OFF_S seconds after the last reply.
# Every request is acknowledged at once with an empty status line (0x02 LF),
# so GI knows it arrived; "0x02 !boot LF" tells GI the XIAO has restarted.
# Battery: after IDLE_OFF_S seconds without a request the XIAO goes into deep
# sleep (microamps instead of ~25 mA). Any byte from the calculator wakes it:
# the calculator's TX also goes to D8 (GPIO7, an RTC pin) through the D7-D8
# link. Waking restarts this program (~1-2 s); the calculator sends GET_ALL
# until it gets an answer, then its real request. The first question after a
# sleep rejoins the last network directly (no scan). If the TX line is low
# when it goes to sleep (calculator off, or its port closed), it wakes when
# the line goes high instead, i.e. when GI opens the port.
# To wipe everything back to the defaults below:
#   mpremote connect com9 fs rm :settings.json
#
# Setup:
#   1. Flash MicroPython ESP32_GENERIC_S3 (Octal-SPIRAM build) with esptool.
#   2. Put your key in .env (HC_KEY=...), then copy both files:
#        mpremote connect com9 cp .env :.env
#        mpremote connect com9 cp main.py :main.py
#        mpremote connect com9 reset
#
# Link: 115200 baud 8N1, fixed. (It used to fall back to 9600 on line noise,
# which could leave GI and the XIAO at different speeds until GI was reopened.)
# Wiring: D6 = GPIO43 (XIAO TX -> calculator RX, jack's left pad),
#         D7 = GPIO44 (XIAO RX <- calculator TX via 1k, jack's back pad),
#         GND -> jack's right pad. Fit the WiFi antenna.
#
# Keep .env (your key and WiFi password) out of GitHub. Hack Club AI is for
# people 18 and under; don't share the key.

import network
import socket
import time
try:
    import requests as urequests  # newer MicroPython builds ship "requests"
except ImportError:
    import urequests
import binascii
import gc
import micropython
import json
import math
import machine
from machine import UART, Pin
try:
    import esp32
except ImportError:
    esp32 = None

# --- defaults (changeable later from the calculator's Settings menu) ---
# Optional: one starting network. More are added from the calculator.
WIFI_SSID = "your-network"
WIFI_PASSWORD = "your-password"
MAX_NETWORKS = 8

# Secrets live in a separate .env file on the XIAO, so updating main.py never
# needs editing. Copy it once (and again after reflashing the firmware):
#   mpremote connect COM11 cp .env :.env
# .env lines are NAME=value, e.g.
#   HC_KEY=sk-hc-v1-...        (key from https://ai.hackclub.com/dashboard)
#   WIFI_SSID=...              (optional starting network)
#   WIFI_PASSWORD=...
# Keep .env out of GitHub and don't share it.
HC_KEY = ""
HC_URL = "https://ai.hackclub.com/proxy/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-5.5"


def _load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        print("no .env file: copy one with  mpremote connect COMx cp .env :.env")
    return env


_ENV = _load_env()
HC_KEY = _ENV.get("HC_KEY", HC_KEY)
# Self-update (GI: Settings > Update). Put these in .env on the XIAO:
#   UPDATE_URL=https://raw.githubusercontent.com/<you>/<repo>/main/xiao/main.py
#   UPDATE_TOKEN=...     (only for a private repo: a read-only access token)
# The file is checked (it must compile and look like this program) before it
# replaces main.py, and the old one is kept as main.bak.
UPDATE_URL = _ENV.get("UPDATE_URL", "")
UPDATE_TOKEN = _ENV.get("UPDATE_TOKEN", "")
UPDATE_MAX = 400000   # bytes: refuse anything bigger
VERSION = "2026.11.13"
WIFI_SSID = _ENV.get("WIFI_SSID", WIFI_SSID)
WIFI_PASSWORD = _ENV.get("WIFI_PASSWORD", WIFI_PASSWORD)
BAUD = 115200       # calculator link; matches GI.g3a
# Replies go out in paced bursts so the calculator's small receive buffer
# never overflows: ~4 KB/s.
PACING = (32, 5)    # (bytes per burst, gap ms)
# Time budgets (seconds), each a little under what GI.g3a waits, so the XIAO
# always answers -- even if only with an error -- before the calculator gives up.
TEXT_BUDGET = 110    # GI waits 120 s for an answer
DIA_BUDGET = 110     # GI waits 120 s for a diagram
PHOTO_BUDGET = 170   # GI waits 180 s for a photo question
API_TIMEOUT = 100    # default per request
IDLE_OFF_S = 45    # deep sleep after this long with no requests
WIFI_OFF_S = 20    # radio off this long after the last reply (a follow-up
                   # question inside that time needs no rejoin)
FIRST_SLEEP_S = 120  # after power-on/reset: time to connect mpremote first
UNSYNCED_S = 5     # woken, bytes arriving, but no GET_ALL: back to sleep
QUIET_S = 2.5      # woken and nothing at all arrived: back to sleep sooner
# Noise on the serial line (calculator off, wire picking up interference)
# can wake the XIAO over and over. After BACKOFF_AFTER false wakes in a row
# it stops trusting the line: it sleeps on a timer and listens for QUIET_S
# every BACKOFF_S seconds instead (GI keeps calling for longer than that).
BACKOFF_AFTER = 3
BACKOFF_S = (15, 30)
WEAK_DBM = -72     # a WiFi join weaker than this looks for a closer access point
ROAM_GAIN_DB = 8   # ...and moves only if one is at least this much stronger
CAM_STANDBY = True   # put the camera chip to sleep when not in use (set False
                     # if photos stop working after installing this version)
SAFETY_WAKE_MS = 3600000  # also wake hourly, in case a line change was missed
WAKE_PIN = 7       # D8 = GPIO7, linked to D7 (UART RX)
FAST_HZ = 240000000  # CPU speed while working (TLS needs it)
IDLE_HZ = 80000000   # CPU speed while WiFi is asleep
LED_PIN = 21         # XIAO ESP32-S3 user LED (lit when low)

SYSTEM_PROMPT = (
    "You are displayed on a graphing calculator's small screen: about 30 "
    "characters wide, plain text only.\n\n"
    "STRICT FORMAT RULES (the screen breaks on anything else):\n"
    "1. Use ONLY simple ASCII characters: letters, digits, spaces and these "
    "symbols: + - * / ^ = ( ) . , : ; ! ? % ' \" < >\n"
    "2. NEVER use Unicode or special symbols. Write sqrt(x) not the root "
    "sign, pi not the pi sign, x^2 not x with a superscript, 1/2 not a "
    "fraction character, * not the times sign, / not the divide sign, - not "
    "a long dash, and 'degrees' not the degree sign.\n"
    "3. NEVER use markdown, LaTeX, tables, bullets, asterisks for bold, "
    "backticks, hash headings or dollar signs.\n"
    "4. Put each separate item on its own short line (about 30 characters "
    "or fewer where possible). Do not use the tilde character.\n\n"
    "Keep every answer as short as possible: no preamble, no restating the "
    "question, no filler, no caveats unless essential. One short sentence or "
    "phrase is ideal for ordinary questions.\n\n"
    "Exception: for maths problems, show the full working. Write each step "
    "of the solution on its own line, in order, ending with the final "
    "answer clearly stated on its own line. Do not skip or compress steps."
)

# Safety net: even if the model ignores the rules above, the calculator only
# gets plain ASCII. Anything unknown becomes "?". "~" is reserved for line breaks.
_ASCII_MAP = {
    "\u00d7": "*", "\u00b7": "*", "\u22c5": "*", "\u00f7": "/", "\u2212": "-",
    "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"',
    "\u201d": '"', "\u2248": "=", "\u2260": "!=", "\u2264": "<=", "\u2265": ">=",
    "\u2192": "->", "\u2190": "<-", "\u00b0": " deg", "\u03c0": "pi", "\u221a": "sqrt",
    "\u00b2": "^2", "\u00b3": "^3", "\u2074": "^4", "\u00b9": "^1", "\u221e": "inf",
    "\u00b1": "+-", "\u03b8": "theta", "\u03b1": "alpha", "\u03b2": "beta",
    "\u03bb": "lambda", "\u03bc": "mu", "\u03c3": "sigma", "\u0394": "Delta",
    "\u03a3": "Sum", "\u00bd": "1/2", "\u00bc": "1/4", "\u00be": "3/4",
    "\u2026": "...", "\u2022": "-", "\u00a0": " ", "\u2009": " ", "\u202f": " ",
    "~": "-", "\t": " ", "\r": "", "\u2208": "in", "\u222b": "int", "\u2211": "Sum",
}


def to_ascii(text):
    out = []
    for ch in text:
        if ch == "\n" or 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            out.append(_ASCII_MAP.get(ch, "?"))
    text = "".join(out).replace("~", "-")
    # strip markdown the model sometimes adds anyway
    for junk in ("**", "__", "`", "$"):
        text = text.replace(junk, "")
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        while line.startswith("#"):
            line = line[1:]
        lines.append(line)
    return "\n".join(lines)


uart = UART(1, baudrate=BAUD, tx=Pin(43), rx=Pin(44), rxbuf=1024)  # big buffer: long questions arrive while we are busy
wlan = network.WLAN(network.STA_IF)

SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"   # the conversation so far, kept across power cycles
HISTORY = []                    # [[role, text], ...] oldest first
MAX_HISTORY_CHARS = 4000        # older turns are dropped past this
NETWORKS = []      # saved networks: [[ssid, password], ...], most recently used last
CURRENT = ""       # SSID we are connected to
PENDING_SSID = ""  # set by SET_SSID, completed by SET_PASS
BSSIDS = {}        # ssid -> router MAC from the last scan (skips the scan on wake)
ASLEEP = False     # True while WiFi is switched off to save battery
LAST_USE = time.ticks_ms()
HOLD_S = 0         # stay awake longer after a photo (you're deciding what to ask)


def _load_json(path):
    """Read a JSON file; if it is missing or damaged (power cut mid-write),
    fall back to the copy saved before it, else None."""
    for p in (path, path + ".bak"):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_json(path, obj):
    """Write to a temp file first, then swap it in: a flat battery during the
    write leaves the old file (or its .bak) intact instead of half a file."""
    import os
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        try:
            os.rename(path, path + ".bak")
        except OSError:
            pass  # first save: nothing to keep yet
        os.rename(tmp, path)
    except Exception as e:
        print("could not save %s: %s" % (path, e))


def _valid_net(n):
    return (isinstance(n, (list, tuple)) and len(n) == 2 and isinstance(n[0], str)
            and n[0] and isinstance(n[1], str))


def load_settings():
    global MODEL, NETWORKS
    d = _load_json(SETTINGS_FILE)
    if not isinstance(d, dict):
        d = {}
    m = d.get("model", d.get("cloud_model_hackclub", MODEL))
    MODEL = m if isinstance(m, str) and m else MODEL
    nets = d.get("networks", [])
    NETWORKS = [list(n) for n in nets if _valid_net(n)] if isinstance(nets, list) else []
    if not NETWORKS and isinstance(d.get("ssid"), str) and d["ssid"]:  # older settings.json
        NETWORKS = [[d["ssid"], str(d.get("pass", ""))]]
    if not NETWORKS and WIFI_SSID != "your-network":
        NETWORKS = [[WIFI_SSID, WIFI_PASSWORD]]


def save_settings():
    _save_json(SETTINGS_FILE, {"model": MODEL, "networks": NETWORKS})


def known_index(ssid):
    for i, n in enumerate(NETWORKS):
        if n[0] == ssid:
            return i
    return -1


def remember(ssid, password):
    """Add or update a saved network."""
    i = known_index(ssid)
    if i >= 0:
        NETWORKS[i][1] = password
    else:
        NETWORKS.append([ssid, password])
        while len(NETWORKS) > MAX_NETWORKS:
            NETWORKS.pop(0)
    save_settings()


def mark_recent(ssid):
    """Move a network to the end of the list (= most recently used)."""
    i = known_index(ssid)
    if i >= 0 and i != len(NETWORKS) - 1:
        NETWORKS.append(NETWORKS.pop(i))
        save_settings()


# WiFi failure codes differ between boards, so look them up by name.
WIFI_FAIL = {}
for _name, _msg in (("STAT_WRONG_PASSWORD", "wrong password"),
                    ("STAT_NO_AP_FOUND", "network not found"),
                    ("STAT_CONNECT_FAIL", "connection failed"),
                    ("STAT_ASSOC_FAIL", "connection failed"),
                    ("STAT_HANDSHAKE_TIMEOUT", "wrong password?"),
                    ("STAT_BEACON_TIMEOUT", "signal lost")):
    if hasattr(network, _name):
        WIFI_FAIL[getattr(network, _name)] = _msg


def rssi():
    """Signal strength of the current connection in dBm (0 if unknown)."""
    try:
        return wlan.status("rssi")
    except Exception:
        return 0


def wifi_connect(ssid, password, secs=12, bssid=None):
    """Try one network. Returns (ok, message). bssid picks one access point:
    without it the ESP32 joins the first one it hears with that name, which
    on a network with many access points (school) is often a weak, far one."""
    global ASLEEP
    args = (ssid, password) if password else (ssid,)  # open network: no key
    msg = "%s: WiFi error" % ssid
    for attempt in range(3):
        # The ESP32's WiFi can get stuck after a failed attempt ("connecting"
        # forever, or "Wifi Internal State Error"), so restart it cleanly first.
        # Coming out of sleep the radio is already off, so skip that pause.
        if wlan.active():
            try:
                wlan.disconnect()
            except Exception:
                pass
            wlan.active(False)
            time.sleep(0.3 + 0.5 * attempt)
        try:
            wlan.active(True)
            ASLEEP = False
            try:
                if bssid:
                    wlan.connect(*args, bssid=bssid)
                else:
                    wlan.connect(*args)
            except TypeError:  # older builds without bssid=
                wlan.connect(*args)
        except OSError as e:
            # "Wifi Internal Error": the radio was not ready. It nearly always
            # works on the next go, so try again instead of giving up (this
            # used to end as "none of your saved networks answered").
            msg = "%s: WiFi error (%s)" % (ssid, e)
            print(msg, "- trying again")
            continue
        for i in range(secs * 2):
            if wlan.isconnected():
                radio_mode()
                return True, "%s: connected, %d dBm, IP %s" % (ssid, rssi(), wlan.ifconfig()[0])
            # stop early on a final failure (ignore the first moments: stale value)
            if i > 3 and wlan.status() in WIFI_FAIL:
                break
            time.sleep(0.5)
        # the router answered (wrong password, not found...): no point repeating
        return False, "%s: %s" % (ssid, WIFI_FAIL.get(wlan.status(), "timed out"))
    return False, msg


def scan_visible(quick=False):
    """Visible networks as {ssid: (rssi, secured)}; empty dict if the scan fails.
    BSSIDS remembers each name's strongest access point. quick=True stops
    after one scan if it already shows a saved network with a good signal."""
    seen = {}
    was_off = ASLEEP or not wlan.active()
    for attempt in range(3):
        try:
            if not wlan.active():
                wlan.active(True)
                time.sleep(0.5)  # a scan straight after power-up can come back empty
            if not wlan.isconnected():
                try:
                    wlan.disconnect()  # a half-finished join blocks scanning
                except Exception:
                    pass
            for r in wlan.scan():
                try:
                    name = r[0].decode()
                except Exception:
                    continue
                if not name:
                    continue  # hidden network
                if name not in seen or r[3] > seen[name][0]:
                    seen[name] = (r[3], r[4] != 0)
                    BSSIDS[name] = r[1]
        except Exception as e:
            print("scan failed:", e)
        # Weak signal makes each scan see a different subset, so always merge
        # two scans, and try a third if both came back empty.
        if attempt >= 1 and seen:
            break
        if quick and any(n[0] in seen and seen[n[0]][0] > -68 for n in NETWORKS):
            break
        if not seen:
            print("scan: nothing found, retrying")
        time.sleep(0.5)
    if was_off:
        wlan.active(False)  # a scan from Settings must not leave the radio on
    return seen


def auto_connect(blind=False):
    """Join the strongest saved network that is in range. Returns (ok, message).
    blind=True also tries the most recent saved network if none is visible
    (hidden networks never show up in a scan)."""
    global CURRENT
    if not NETWORKS:
        return False, "No saved networks"
    seen = scan_visible(quick=True)
    ranked = sorted(
        [(seen[n[0]][0], n) for n in NETWORKS if n[0] in seen],
        key=lambda t: -t[0],
    )
    candidates = [n for _, n in ranked][:2]
    if not candidates and blind:
        candidates = [NETWORKS[-1]]
        BSSIDS.pop(NETWORKS[-1][0], None)  # not seen: don't aim at an old access point
    if not candidates:
        return False, "No saved network in range"
    msg = ""
    for ssid, password in candidates:
        # straight to the strongest access point the scan found for this name
        ok, msg = wifi_connect(ssid, password, secs=10, bssid=BSSIDS.get(ssid))
        if not ok and BSSIDS.get(ssid) and "password" not in msg:
            ok, msg = wifi_connect(ssid, password, secs=10)  # let the radio choose
        if ok:
            CURRENT = ssid
            mark_recent(ssid)
            return True, msg
    return False, msg


# ---------------------------------------------------------------- power saving
def radio_mode():
    """No WiFi power-saving while connected: on the ESP32 it makes slow
    connections stall and drop (time-outs, "connection dropped") when the
    signal is not strong. The battery is saved by switching the radio right
    off WIFI_OFF_S after the last reply instead."""
    try:
        wlan.config(pm=getattr(wlan, "PM_NONE", 0))
    except Exception:
        pass


def set_cpu(hz):
    try:
        if machine.freq() != hz:
            machine.freq(hz)
    except Exception:
        pass


def touch():
    """Note activity: restarts the idle timer and brings the CPU to full speed."""
    global LAST_USE
    LAST_USE = time.ticks_ms()
    set_cpu(FAST_HZ)


def wifi_sleep():
    """Switch the radio off. The next question wakes it again."""
    global ASLEEP
    try:
        wlan.disconnect()
    except Exception:
        pass
    wlan.active(False)
    ASLEEP = True
    set_cpu(IDLE_HZ)
    print("idle: WiFi off")


def roam():
    """Connected, but the signal is weak. On a network with many access
    points (school) the one remembered from last time may now be far away:
    look for a much stronger one and move to it. Returns True if connected."""
    global CURRENT
    here = rssi()
    seen = scan_visible(quick=True)
    best = None
    for n in NETWORKS:
        v = seen.get(n[0])
        if v and (best is None or v[0] > best[0]):
            best = (v[0], n)
    if best and best[0] >= here + ROAM_GAIN_DB:
        ssid, password = best[1]
        ok, msg = wifi_connect(ssid, password, secs=8, bssid=BSSIDS.get(ssid))
        print("roam (%d -> %d dBm):" % (here, best[0]), msg)
        if ok:
            CURRENT = ssid
            mark_recent(ssid)
            return True
        ok, msg = auto_connect(blind=True)
        print("roam (search):", msg)
        return ok
    return wlan.isconnected()


def wake_wifi():
    """Rejoin quickly: the last network first, straight to the access point
    used last time, no scan. If that one is now weak, look for a closer one.
    Falls back to the normal strongest-saved-network search."""
    touch()
    if wlan.isconnected():
        return True
    i = known_index(CURRENT) if CURRENT else -1
    if i >= 0 and BSSIDS.get(CURRENT):
        ok, msg = wifi_connect(CURRENT, NETWORKS[i][1], secs=8, bssid=BSSIDS.get(CURRENT))
        print("wake:", msg)
        if ok:
            if rssi() < WEAK_DBM:
                roam()
            if wlan.isconnected():
                return True
    ok, msg = auto_connect(blind=True)
    print("wake (search):", msg)
    return ok


def recover_link():
    """Between tries of a failed request: rejoin if WiFi dropped; if it is
    still up but weak, look for a closer access point."""
    if not wlan.isconnected():
        stage("Rejoining WiFi")
        wake_wifi()
    elif rssi() < WEAK_DBM:
        stage("Weak WiFi: looking closer")
        roam()


def weak_note():
    """Extra line for a network error message when the signal is the cause."""
    r = rssi() if wlan.isconnected() else 0
    return "~WiFi signal here is weak (%d dBm)." % r if r and r < WEAK_DBM else ""


def camera_standby():
    """Put the camera chip into its software standby. The Sense board has no
    power-down wire to the camera, so otherwise the chip stays powered and
    idling the whole time, deep sleep included. Best effort: any problem is
    ignored (set CAM_STANDBY = False to skip it). The camera driver resets
    the chip when the next photo is taken, which wakes it again."""
    if not CAM_STANDBY:
        return
    try:
        from machine import PWM, SoftI2C
        clk = PWM(Pin(10), freq=10000000, duty_u16=32768)  # it only listens while clocked
        try:
            time.sleep_ms(20)
            i2c = SoftI2C(scl=Pin(39, Pin.IN, Pin.PULL_UP), sda=Pin(40, Pin.IN, Pin.PULL_UP), freq=100000)
            found = i2c.scan()
            if 0x3C in found:    # OV5640 / OV3660: 16-bit register addresses
                i2c.writeto_mem(0x3C, 0x3008, b"\x42", addrsize=16)
                print("camera: standby (OV5640)")
            elif 0x30 in found:  # OV2640
                i2c.writeto_mem(0x30, 0xFF, b"\x01")
                i2c.writeto_mem(0x30, 0x09, b"\x10")
                print("camera: standby (OV2640)")
            time.sleep_ms(10)
        finally:
            clk.deinit()
            for p in (10, 39, 40):
                Pin(p, Pin.IN)
    except Exception as e:
        print("camera standby skipped:", e)


# ---------------------------------------------------------------- deep sleep
def _rtc_get():
    try:
        d = json.loads(machine.RTC().memory() or b"{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _rtc_put(d):
    try:
        machine.RTC().memory(json.dumps(d).encode())
    except Exception as e:
        print("rtc save failed:", e)


def save_rtc():
    """Keep what a fast WiFi rejoin needs across deep sleep (RTC memory).
    (Going to sleep normally also clears the crash count; the wake counters
    are kept.)"""
    b = BSSIDS.get(CURRENT)
    d = _rtc_get()
    d.update({"cur": CURRENT, "crash": 0,
              "bssid": "".join("%02x" % x for x in b) if b else ""})
    _rtc_put(d)


def count_wake(key):
    """Battery diagnostics, kept in RTC memory across deep sleep:
      ext0  = woken by the calculator line   timer = hourly safety wake
      false = woke on the line but no GET_ALL came (noise on the line)
    Read them with:
      mpremote connect COMx exec "import machine; print(machine.RTC().memory())"
    """
    d = _rtc_get()
    d[key] = d.get(key, 0) + 1
    _rtc_put(d)
    # Also on flash: RTC memory is wiped if the battery goes flat.
    #   mpremote connect COMx fs cat :wakes.json
    w = _load_json("wakes.json")
    w = w if isinstance(w, dict) else {}
    w[key] = w.get(key, 0) + 1
    _save_json("wakes.json", w)


def count_crash():
    """Crash-loop guard: how many crashes in a row (kept across resets)."""
    d = _rtc_get()
    d["crash"] = d.get("crash", 0) + 1
    _rtc_put(d)
    return d["crash"]


def clear_crashes():
    d = _rtc_get()
    if d.get("crash"):
        d["crash"] = 0
        _rtc_put(d)


def load_rtc():
    global CURRENT
    d = _rtc_get()
    if not d:
        return
    cur = d.get("cur", "")
    CURRENT = cur if isinstance(cur, str) and cur else CURRENT
    h = d.get("bssid", "")
    if CURRENT and isinstance(h, str) and len(h) == 12:
        try:
            BSSIDS[CURRENT] = bytes(int(h[i:i + 2], 16) for i in range(0, 12, 2))
        except ValueError:
            pass


def false_wakes(n=None):
    """False wakes in a row (kept in RTC memory). n sets the count."""
    d = _rtc_get()
    if n is None:
        return d.get("fw", 0) if isinstance(d.get("fw", 0), int) else 0
    if d.get("fw", 0) != n:
        d["fw"] = n
        _rtc_put(d)
    return n


def deep_sleep():
    """Sleep until the calculator's TX line changes: idle-high -> low means it
    is sending; low -> high means GI has just opened the port. A long timer
    wake is a safety net; that path just goes back to sleep."""
    save_rtc()
    try:
        wlan.disconnect()
    except Exception:
        pass
    try:
        wlan.active(False)
    except Exception:
        pass
    # Is the calculator actually driving the line? Test with a pull-DOWN: only
    # a powered TX pin can hold it high against that. Then keep the line
    # firmly at its resting level while asleep, so noise on an undriven line
    # (calculator off) can't wake us over and over. (The old code always used
    # a pull-up, which leaves an undriven line half-way: a recipe for false
    # wakes that flatten the battery in hours.)
    pin = Pin(WAKE_PIN, Pin.IN, Pin.PULL_DOWN)
    time.sleep_ms(5)
    driven_high = pin.value() == 1
    if driven_high:   # GI / calculator idle: wake when it starts sending (low)
        pin = Pin(WAKE_PIN, Pin.IN, Pin.PULL_UP)
    fw = false_wakes()
    if fw >= BACKOFF_AFTER + 20:
        fw = false_wakes(BACKOFF_AFTER - 1)  # ~10 minutes on: trust the line again
    if fw >= BACKOFF_AFTER and not driven_high:
        # The line keeps waking us with nobody there: don't let it. Sleep on
        # a timer and listen briefly instead (main() handles that wake).
        secs = BACKOFF_S[0] if fw == BACKOFF_AFTER else BACKOFF_S[1]
        if esp32:
            try:
                esp32.wake_on_ext0(pin=None, level=esp32.WAKEUP_ANY_HIGH)
            except Exception:
                pass
        print("deep sleep: noisy line, listening again in %d s" % secs)
        machine.deepsleep(secs * 1000)
    if esp32:
        esp32.wake_on_ext0(pin=pin, level=esp32.WAKEUP_ALL_LOW if driven_high else esp32.WAKEUP_ANY_HIGH)
    print("deep sleep: wake when the calculator line goes %s" % ("low" if driven_high else "high"))
    machine.deepsleep(SAFETY_WAKE_MS)


def reconnect_wifi():
    ok, msg = auto_connect(blind=True)
    return ("OK:" if ok else "ERR:") + msg


def encode_reply(text):
    # A bare "\n" ends a message on the serial link, so newlines inside a
    # multi-line maths answer are sent as "~" (the calculator turns them back
    # into line breaks). GI.g3a holds 6143 characters; long working is cut
    # with a note rather than silently.
    text = to_ascii(text).strip().replace("\r\n", "\n").replace("\n", "~")
    if len(text) > REPLY_MAX:
        text = text[:REPLY_MAX - 20].rstrip("~") + "~(answer cut short)"
    return text


REPLY_MAX = 5500


def describe_status(code):
    if code == 401:
        return "API key rejected (check HC_KEY in .env)" if HC_KEY else "no API key: copy .env to the XIAO"
    if code == 402:
        return "daily $3 limit used up"
    if code == 403:
        return "blocked (not allowed)"
    if code == 429:
        return "rate limited, wait a bit"
    if code in (400, 404):
        return "model not accepted (HTTP %d): change Model" % code
    return "HTTP %d" % code


def net_error(e):
    """A network OSError as a short, useful message."""
    code = e.args[0] if e.args and isinstance(e.args[0], int) else None
    s = str(e)
    if code in (110, 116) or "timed out" in s.lower() or "ETIMEDOUT" in s:
        return "the AI server took too long.~Try again, or pick a faster model (Sonnet)."
    if code in (-2, -202) or "getaddrinfo" in s:
        return "can't reach the internet (DNS failed).~Hotspot data off, or a login page?"
    if code in (103, 104, 113, 118, 128):
        return "the connection dropped.~Check the WiFi signal and the antenna."
    return "network problem (%s).~Check the WiFi signal and the antenna." % s[:30]


def load_history():
    """Only well-formed [role, text] pairs: one bad entry would otherwise make
    every later question fail."""
    global HISTORY
    h = _load_json(HISTORY_FILE)
    HISTORY = []
    ok = [t for t in (h if isinstance(h, list) else [])
          if isinstance(t, (list, tuple)) and len(t) == 2 and isinstance(t[1], str)]
    for i in range(len(ok) - 1):  # keep only question -> answer pairs
        if ok[i][0] == "user" and ok[i + 1][0] == "assistant":
            HISTORY += [["user", ok[i][1]], ["assistant", ok[i + 1][1]]]


def save_history():
    _save_json(HISTORY_FILE, HISTORY)


def remember_turn(question, answer):
    """Add one question/answer pair, then drop the oldest pairs until the
    conversation fits in MAX_HISTORY_CHARS."""
    HISTORY.append(["user", question])
    HISTORY.append(["assistant", answer])
    while len(HISTORY) > 2 and sum(len(t[1]) for t in HISTORY) > MAX_HISTORY_CHARS:
        HISTORY.pop(0)
        HISTORY.pop(0)
    save_history()


def hc_post(prompt, system, max_tokens, history=(), timeout=None, model=None):
    """POST to Hack Club AI. Returns the response object (caller closes it)."""
    gc.collect()  # keep as much RAM free as possible for the TLS handshake
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    for role, text in history:
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": prompt})
    kwargs = dict(
        headers={
            "Authorization": "Bearer " + HC_KEY,
            "Content-Type": "application/json",
        },
        json={"model": model or MODEL, "messages": messages, "max_tokens": max_tokens},
    )
    # A stalled connection must not freeze the XIAO: give up after API_TIMEOUT s.
    # (Older urequests has no timeout argument, so fall back without it.)
    try:
        return urequests.post(HC_URL, timeout=timeout or API_TIMEOUT, **kwargs)
    except TypeError:
        return urequests.post(HC_URL, **kwargs)


def left(deadline):
    """Seconds until a ticks_ms deadline (negative once it has passed)."""
    return time.ticks_diff(deadline, time.ticks_ms()) / 1000


def short_model():
    """'anthropic/claude-sonnet-5.5' -> 'sonnet-5.5' for the wait screen."""
    m = MODEL.split("/")[-1]
    return m[7:] if m.startswith("claude-") else m


def stage(text):
    """Tell GI what we're doing; it shows this on its wait screen. A line
    starting with byte 0x02 is a status update, not the answer.
    (GI.g3a from Nov 2026 understands these: install both together.)"""
    try:
        uart.write(b"\x02" + to_ascii(text)[:40].encode() + b"\n")
    except Exception:
        pass


def ack():
    """Tell GI at once that its request arrived (an empty status line). No
    ack within a few seconds means the XIAO was asleep or restarting, and GI
    wakes it and sends the request again instead of waiting two minutes."""
    try:
        uart.write(b"\x02\n")
    except Exception:
        pass


def ready_to_ask():
    """'' if a question can go out now, else the reason it can't."""
    if not HC_KEY:
        return "no API key: copy .env to the XIAO~(mpremote connect COMx cp .env :.env)"
    return wifi_ready()


def wifi_ready():
    """'' if WiFi is up (joining it if needed), else the reason it isn't."""
    if not wlan.isconnected() and NETWORKS:
        stage("Joining WiFi")
        wake_wifi()
    if not wlan.isconnected():
        if not NETWORKS:
            return "no WiFi saved: add one in Settings > WiFi"
        return "no WiFi: none of your saved networks answered~Check Settings > WiFi"
    return ""


def reply_text(resp):
    """(text, why_empty) from a chat reply. Some models think privately
    first; if that uses the whole max_tokens, the visible answer is empty."""
    try:
        d = resp.json()
    except ValueError:
        return "", "the server sent back something that isn't JSON. Try again."
    finally:
        resp.close()
    if not isinstance(d, dict):
        return "", "unexpected reply from the server"
    try:
        ch = d["choices"][0]
        text = ch["message"].get("content") or ""
        fin = ch.get("finish_reason") or ""
    except (KeyError, IndexError, TypeError):
        return "", "unexpected reply from the server"
    if isinstance(text, list):  # some models return content as parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text.strip():
        return text, ""
    print("empty reply, finish_reason:", fin, "| usage:", d.get("usage"))
    if fin == "length":
        return "", "the model used its whole length limit thinking.~Try again, or use Sonnet (faster)."
    if fin in ("content_filter", "safety"):
        return "", "the model refused to answer this one."
    return "", "the model sent back an empty answer (%s).~Try again." % (fin or "no reason given")


def ask_ai(prompt, tries=3):
    deadline = time.ticks_add(time.ticks_ms(), TEXT_BUDGET * 1000)
    why = ready_to_ask()
    if why:
        return "error: " + why
    last = ""
    for attempt in range(tries):
        if attempt:
            recover_link()  # dropped or weak: rejoin / move before retrying
        t0 = time.ticks_ms()
        try:
            stage(("Asking %s" if not attempt else "Retrying %s") % short_model())
            resp = hc_post(prompt, SYSTEM_PROMPT, 2000, HISTORY, max(10, int(left(deadline))))
            if resp.status_code != 200:
                code = resp.status_code
                resp.close()
                return "error: " + describe_status(code)
            text, why = reply_text(resp)
            if not text:
                return "error: " + why
            reply = encode_reply(text)
            remember_turn(prompt, reply.replace("~", "\n"))
            return reply
        except OSError as e:
            # Network errors (ECONNRESET) are usually a dropped connection from
            # weak WiFi, so try again -- unless it was a slow timeout (the model
            # is just slow; asking again would only pay twice) or time is up.
            last = net_error(e) + weak_note()
            gc.collect()
            if time.ticks_diff(time.ticks_ms(), t0) > 20000 or left(deadline) < 25:
                break
            time.sleep(1 + attempt)
        except MemoryError:
            gc.collect()
            return "error: out of memory~Start a new chat (F4) and try again."
        except Exception as e:
            return "error: " + str(e)[:80]
    return "error: " + last


def run_test():
    """WiFi -> DNS -> a tiny real API call. Lines joined with ~."""
    if not wlan.isconnected():
        wake_wifi()
    if not wlan.isconnected():
        return "ERR:WiFi: not connected~Check SSID and password, then use the WiFi row."
    lines = ["main.py " + VERSION]
    try:
        lines.append("%s %d dBm" % (CURRENT or "WiFi ok", wlan.status("rssi")))
    except Exception:
        lines.append(CURRENT or "WiFi ok")
    try:
        socket.getaddrinfo("ai.hackclub.com", 443)
        lines.append("DNS ok")
    except Exception:
        lines.append("DNS failed: no internet?")
        lines.append("Hotspot data off or login page?")
        return "ERR:" + "~".join(lines)
    t0 = time.ticks_ms()
    try:
        resp = hc_post("hi", None, 16)
        code = resp.status_code
        resp.close()
    except Exception as e:
        lines.append("Server unreachable: " + str(e)[:30])
        return "ERR:" + "~".join(lines)
    secs = time.ticks_diff(time.ticks_ms(), t0) / 1000
    if code == 200:
        lines.append("Server ok, %.1fs" % secs)
        return "OK:" + "~".join(lines)
    lines.append("Server: " + describe_status(code))
    return "ERR:" + "~".join(lines)


def nets_reply():
    seen = scan_visible()
    up = wlan.isconnected()
    saved = []
    for n in NETWORKS:
        v = n[0] in seen
        cur = up and n[0] == CURRENT
        saved.append((
            0 if cur else 1,                      # connected first
            0 if v else 1,                        # then in range
            -(seen[n[0]][0] if v else -200),      # strongest first
            "1|%d|%d|%d|%s" % (1 if cur else 0, 1 if v else 0,
                               1 if (not v or seen[n[0]][1]) else 0, n[0]),
        ))
    saved.sort()
    new = sorted(
        [(r, sec, name) for name, (r, sec) in seen.items() if known_index(name) < 0],
        key=lambda t: -t[0],
    )
    entries = [e[3] for e in saved]
    entries += ["0|0|1|%d|%s" % (1 if sec else 0, name) for r, sec, name in new]
    return "OK:" + "~".join(entries[:10])


# ---------------------------------------------------------------- diagrams
# GET_DIA:<question> -> the chat model works out the diagram and writes a
# small SVG (viewBox 0 0 384 192, the calculator screen). The XIAO flattens
# it (groups, transforms, paths, arcs, arrow markers) into simple commands
# that GI draws at full resolution, with its own font for the labels:
#   P <col> <width> <dash> <arrows> <closed> x y x y ...   stroke a polyline
#   F <col> x y x y ...                                    fill a polygon
#   C <fill> <stroke> <width> cx cy r                      circle
#   T <col> <l|m|r> x y text                               label (baseline y)
# Commands are joined with "~"; colours are RGB565 hex, "-" = none;
# arrows: 1 = at the end, 2 = at the start, 3 = both. "`" = degree sign.

DIA_W, DIA_H = 384, 192
DIA_MAX = 6000          # reply size the calculator accepts
DIA_PROMPT = (
    "You draw diagrams on a graphing calculator's 384x192 pixel colour screen.\n"
    "Reply with ONLY one SVG: <svg viewBox=\"0 0 384 192\">...</svg>. No prose, "
    "no code fences, no explanation.\n\n"
    "Allowed: line, polyline, polygon, rect, circle, path (M L H V A Q C Z), text, "
    "and g with transform (translate, rotate, scale). Arrows: put "
    "marker-end=\"url(#a)\" (and marker-start for two-headed) on a line or path; "
    "you do not need to define the marker. Colours: black, red, blue, green, "
    "orange, purple, grey. The background is white; do not draw one. Use "
    "fill=\"none\" for outlines. stroke-width 1 or 2. stroke-dasharray for dashed "
    "construction lines.\n"
    "Labels: short (under 12 characters), plain text, roughly 14 px tall; the "
    "degree sign is fine. Place every label clear of lines and other labels, and "
    "keep everything inside x 4..380, y 16..188, and leave the bottom-left "
    "corner (x below 75 and y above 165) empty: a button covers it.\n\n"
    "Draw each force exactly ONCE, as one straight arrow (a line with "
    "marker-end); never draw arrowheads yourself with extra lines or polygons. "
    "Show only the forces acting on the body: no acceleration, velocity, "
    "motion or resultant arrows unless the question asks for them. Friction "
    "only if the surface is rough or friction is mentioned.\n"
    "Make it the correct, clear diagram an examiner would draw for the question. "
    "For mechanics: draw the body and surfaces (a slope as a triangle), forces "
    "as arrows starting at the point where they act and labelled with symbol or "
    "magnitude (W or mg straight down, R or N perpendicular to the surface, "
    "friction F along the surface opposing the motion or tendency to move, "
    "tension T along the string), angles marked with a small arc and labelled, "
    "and given lengths and directions shown. Use colour to separate forces from "
    "geometry. Draw the diagram only; do not solve the question."
)

_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (220, 30, 30),
    "darkred": (139, 0, 0), "blue": (30, 70, 220), "darkblue": (0, 0, 139),
    "navy": (0, 0, 128), "green": (20, 150, 40), "darkgreen": (0, 100, 0),
    "lime": (0, 200, 0), "orange": (240, 130, 0), "purple": (130, 40, 170),
    "magenta": (200, 0, 200), "pink": (240, 100, 160), "brown": (140, 80, 30),
    "grey": (128, 128, 128), "gray": (128, 128, 128), "darkgrey": (90, 90, 90),
    "darkgray": (90, 90, 90), "lightgrey": (200, 200, 200),
    "lightgray": (200, 200, 200), "silver": (190, 190, 190),
    "yellow": (230, 200, 0), "gold": (220, 170, 0), "cyan": (0, 170, 200),
    "teal": (0, 128, 128), "olive": (128, 128, 0), "maroon": (128, 0, 0),
}


def _color(v):
    """SVG colour -> 'XXXX' RGB565 hex, None for none, '' if not given."""
    if v is None:
        return ""
    v = v.strip().lower()
    if not v:
        return ""
    if v in ("none", "transparent"):
        return None
    rgb = None
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = h[0] * 2 + h[1] * 2 + h[2] * 2
        if len(h) >= 6:
            try:
                rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                pass
    elif v.startswith("rgb"):
        try:
            p = v[v.index("(") + 1:v.index(")")].split(",")
            rgb = tuple(int(float(x.strip().rstrip("%"))) for x in p[:3])
        except (ValueError, IndexError):
            pass
    else:
        rgb = _COLORS.get(v)
    if rgb is None:
        rgb = (0, 0, 0)
    r, g, b = rgb
    return "%04X" % (((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))


def _num(s, default=0.0):
    try:
        return float(s.strip().replace("px", ""))
    except (ValueError, AttributeError):
        return default


def _numbers(s):
    """All numbers in a string such as a points list or path data."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in "0123456789.-+":
            j = i + 1
            seen_dot = c == "."
            while j < n:
                d = s[j]
                if d.isdigit():
                    j += 1
                elif d == "." and not seen_dot:
                    seen_dot = True
                    j += 1
                elif d in "eE" and j + 1 < n and (s[j + 1].isdigit() or s[j + 1] in "+-"):
                    j += 2
                else:
                    break
            try:
                out.append(float(s[i:j]))
            except ValueError:
                pass
            i = j
        else:
            i += 1
    return out


def _mul(a, b):
    """Affine matrices (a, b, c, d, e, f): apply b first, then a."""
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
            a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _transform(s):
    m = _IDENT
    if not s:
        return m
    i = 0
    while True:
        j = s.find("(", i)
        if j < 0:
            break
        k = s.find(")", j)
        if k < 0:
            break
        name = s[i:j].strip(" ,").lower()
        v = _numbers(s[j + 1:k])
        t = _IDENT
        if name == "translate" and v:
            t = (1.0, 0.0, 0.0, 1.0, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            t = (v[0], 0.0, 0.0, v[1] if len(v) > 1 else v[0], 0.0, 0.0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            c, sn = math.cos(a), math.sin(a)
            t = (c, sn, -sn, c, 0.0, 0.0)
            if len(v) >= 3:
                t = _mul(_mul((1.0, 0.0, 0.0, 1.0, v[1], v[2]), t),
                         (1.0, 0.0, 0.0, 1.0, -v[1], -v[2]))
        elif name == "matrix" and len(v) >= 6:
            t = tuple(v[:6])
        m = _mul(m, t)
        i = k + 1
    return m


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _arc(x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """SVG elliptical arc -> points (excluding the start), per SVG spec F.6.5."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    p = math.radians(phi)
    cp, sp = math.cos(p), math.sin(p)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    xp = cp * dx + sp * dy
    yp = -sp * dx + cp * dy
    lam = (xp * xp) / (rx * rx) + (yp * yp) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * yp * yp - ry * ry * xp * xp
    den = rx * rx * yp * yp + ry * ry * xp * xp
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * yp / ry, -co * ry * xp / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2
    t1 = math.atan2((yp - cyp) / ry, (xp - cxp) / rx)
    t2 = math.atan2((-yp - cyp) / ry, (-xp - cxp) / rx)
    dt = t2 - t1
    if sweep and dt < 0:
        dt += 2 * math.pi
    elif not sweep and dt > 0:
        dt -= 2 * math.pi
    n = max(2, int(abs(dt) / math.radians(10)) + 1)
    pts = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * ex - sp * ey + cx, sp * ex + cp * ey + cy))
    return pts


def _path(d):
    """Path data -> list of (points, closed). Supports M L H V Z A Q T C S."""
    subs = []
    pts = []
    x = y = sx = sy = 0.0
    lastc = None
    i = 0
    n = len(d)
    cmd = None
    while i < n:
        c = d[i]
        if c.isalpha():
            cmd = c
            i += 1
            if cmd in "Zz":
                if pts:
                    subs.append((pts, True))
                pts = []
                x, y = sx, sy
            continue
        if cmd is None:
            i += 1
            continue
        j = i
        while j < n and not d[j].isalpha():
            j += 1
        v = _numbers(d[i:j])
        i = j
        rel = cmd.islower()
        C = cmd.upper()
        size = {"M": 2, "L": 2, "H": 1, "V": 1, "A": 7, "Q": 4, "T": 2, "C": 6, "S": 4}.get(C, 0)
        if not size:
            continue
        k = 0
        while k + size <= len(v):
            a = v[k:k + size]
            k += size
            ox, oy = (x, y) if rel else (0.0, 0.0)
            if C == "M":
                if pts:
                    subs.append((pts, False))
                x, y = a[0] + ox, a[1] + oy
                sx, sy = x, y
                pts = [(x, y)]
                C = "L"            # further pairs are line-tos
            elif C == "L" or C == "T":
                x, y = a[0] + ox, a[1] + oy
                pts.append((x, y))
            elif C == "H":
                x = a[0] + ox
                pts.append((x, y))
            elif C == "V":
                y = a[0] + oy
                pts.append((x, y))
            elif C == "A":
                ex, ey = a[5] + ox, a[6] + oy
                pts.extend(_arc(x, y, a[0], a[1], a[2], int(a[3]), int(a[4]), ex, ey))
                x, y = ex, ey
            elif C in "QCS":
                if C == "Q":
                    c1 = c2 = (a[0] + ox, a[1] + oy)
                    e = (a[2] + ox, a[3] + oy)
                elif C == "C":
                    c1, c2, e = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy), (a[4] + ox, a[5] + oy)
                else:
                    c1, c2, e = (x, y), (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
                for s in range(1, 9):
                    t = s / 8
                    u = 1 - t
                    px = u * u * u * x + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * e[0]
                    py = u * u * u * y + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * e[1]
                    pts.append((px, py))
                x, y = e
            if not pts:
                pts = [(x, y)]
    if len(pts) > 1:
        subs.append((pts, False))
    return subs


def _attrs(tag):
    """'line x1="1" style="stroke:red"' -> (name, {attr: value})."""
    tag = tag.strip().rstrip("/").strip()
    sp = 0
    while sp < len(tag) and not tag[sp].isspace():
        sp += 1
    name = tag[:sp].lower()
    if ":" in name:
        name = name.split(":")[-1]
    a = {}
    rest = tag[sp:]
    i = 0
    n = len(rest)
    while i < n:
        eq = rest.find("=", i)
        if eq < 0:
            break
        key = rest[i:eq].strip().lower()
        q = eq + 1
        while q < n and rest[q].isspace():
            q += 1
        if q >= n:
            break
        if rest[q] in "\"'":
            e = rest.find(rest[q], q + 1)
            if e < 0:
                e = n
            a[key] = rest[q + 1:e]
            i = e + 1
        else:
            e = q
            while e < n and not rest[e].isspace():
                e += 1
            a[key] = rest[q:e]
            i = e
    st = a.get("style")
    if st:
        for part in st.split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                a.setdefault(k.strip().lower(), v.strip())
    return name, a


_ENT = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"),
        ("&#176;", "°"), ("&deg;", "°"), ("&#x3b8;", "θ"),
        ("&theta;", "θ"), ("&mu;", "μ"), ("&alpha;", "α"),
        ("&nbsp;", " "), ("&amp;", "&"))
_GREEK = {"θ": "@", "α": "a", "β": "b", "μ": "u", "λ": "L",
          "ω": "w", "φ": "phi", "Δ": "D", "π": "pi"}


def _label(t):
    # numeric entities: &#952; &#x3b8;
    k = t.find("&#")
    while k >= 0:
        e = t.find(";", k)
        if e < 0 or e - k > 8:
            break
        code = t[k + 2:e]
        try:
            ch = chr(int(code[1:], 16) if code[:1] in "xX" else int(code))
        except ValueError:
            ch = "?"
        t = t[:k] + ch + t[e + 1:]
        k = t.find("&#", k + 1)
    for e, r in _ENT:
        t = t.replace(e, r)
    t = t.replace("°", "`").replace("θ", "theta")
    out = []
    for ch in t:
        if 32 <= ord(ch) < 127 and ch != "~":
            out.append(ch)
        elif ch == "`":
            out.append(ch)
        else:
            a = _ASCII_MAP.get(ch, "?") if "_ASCII_MAP" in globals() else "?"
            out.append(a.replace(" deg", "`").replace("~", "-"))
    return " ".join("".join(out).split())


def svg_to_commands(text):
    """Model reply -> 'DIA:...' command string, or None if there is no SVG."""
    s = text.find("<svg")
    if s < 0:
        return None
    e = text.find("</svg>", s)
    svg = text[s:e if e > 0 else len(text)]
    out = []
    stack = []                       # inherited state per open <g>
    st = {"m": _IDENT, "stroke": "", "fill": "", "sw": 1.0, "dash": False,
          "anchor": "start", "fs": 14.0}
    view = None
    i = 0
    n = len(svg)
    skip_until = None

    def emit_poly(pts, closed, a, state, arrows, is_open_shape):
        if len(pts) < 2:
            return
        m = state["m"]
        P = [_apply(m, x, y) for x, y in pts]
        P = [(int(round(fx(x))), int(round(fy(y)))) for x, y in P]
        fill = _color(a.get("fill")) if "fill" in a else state["fill"]
        if fill == "" and is_open_shape:
            fill = None              # models forget fill="none" on paths
        elif fill == "":
            fill = "0000"
        stroke = _color(a.get("stroke")) if "stroke" in a else state["stroke"]
        if stroke == "":
            stroke = "0000" if (fill is None or is_open_shape) else None
        sw = _num(a.get("stroke-width"), state["sw"]) if "stroke-width" in a else state["sw"]
        w = max(1, min(3, int(round(sw * scale_k))))
        dash = state["dash"] or (a.get("stroke-dasharray", "none").strip() not in ("", "none"))
        flat = " ".join("%d %d" % p for p in P[:60])
        if fill and closed:
            out.append("F %s %s" % (fill, flat))
        if stroke:
            out.append("P %s %d %d %d %d %s" % (stroke, w, 1 if dash else 0, arrows,
                                                1 if closed else 0, flat))

    def arrows_of(a):
        r = 0
        if "marker-end" in a:
            r |= 1
        if "marker-start" in a:
            r |= 2
        return r

    # viewBox -> screen mapping, filled in when <svg> is seen
    scale_k = 1.0
    off = [0.0, 0.0]

    def fx(x):
        return x * scale_k + off[0]

    def fy(y):
        return y * scale_k + off[1]

    while i < n:
        lt = svg.find("<", i)
        if lt < 0:
            break
        gt = svg.find(">", lt)
        if gt < 0:
            break
        raw = svg[lt + 1:gt]
        i = gt + 1
        if raw.startswith("!--"):
            ce = svg.find("-->", lt)
            i = ce + 3 if ce >= 0 else n
            continue
        if raw.startswith("?") or raw.startswith("!"):
            continue
        closing = raw.startswith("/")
        name, a = _attrs(raw[1:] if closing else raw)
        if skip_until:
            if closing and name == skip_until:
                skip_until = None
            continue
        if closing:
            if name == "g" and stack:
                st = stack.pop()
            continue
        if name in ("defs", "marker", "style", "title", "desc", "clippath", "mask", "pattern"):
            if not raw.endswith("/"):
                skip_until = name
            continue
        if name == "svg":
            vb = _numbers(a.get("viewbox", ""))
            if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
                view = vb
            elif "width" in a and "height" in a:
                view = [0, 0, _num(a["width"], DIA_W), _num(a["height"], DIA_H)]
            if view:
                scale_k = min(DIA_W / view[2], DIA_H / view[3])
                off[0] = (DIA_W - view[2] * scale_k) / 2 - view[0] * scale_k
                off[1] = (DIA_H - view[3] * scale_k) / 2 - view[1] * scale_k
            continue
        state = dict(st)
        if "transform" in a:
            state["m"] = _mul(st["m"], _transform(a["transform"]))
        for k, key in (("stroke", "stroke"), ("fill", "fill")):
            if key in a:
                state[k] = _color(a[key])
        if "stroke-width" in a:
            state["sw"] = _num(a["stroke-width"], 1.0)
        if a.get("stroke-dasharray", "none").strip() not in ("", "none"):
            state["dash"] = True
        if "text-anchor" in a:
            state["anchor"] = a["text-anchor"].strip()
        if name == "g":
            if not raw.endswith("/"):
                stack.append(st)
                st = state
            continue
        if name == "line":
            pts = [(_num(a.get("x1", "0")), _num(a.get("y1", "0"))),
                   (_num(a.get("x2", "0")), _num(a.get("y2", "0")))]
            emit_poly(pts, False, a, state, arrows_of(a), True)
        elif name in ("polyline", "polygon"):
            v = _numbers(a.get("points", ""))
            pts = [(v[k], v[k + 1]) for k in range(0, len(v) - 1, 2)]
            emit_poly(pts, name == "polygon", a, state, arrows_of(a), name == "polyline")
        elif name == "rect":
            x, y = _num(a.get("x", "0")), _num(a.get("y", "0"))
            w, h = _num(a.get("width", "0")), _num(a.get("height", "0"))
            if w > 0 and h > 0 and not (w >= view[2] * 0.95 if view else w >= DIA_W * 0.95):
                emit_poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True, a, state, 0, False)
        elif name == "path":
            subs = _path(a.get("d", ""))
            for k, (pts, closed) in enumerate(subs):
                arr = 0
                ar = arrows_of(a)
                if ar & 1 and k == len(subs) - 1:
                    arr |= 1
                if ar & 2 and k == 0:
                    arr |= 2
                emit_poly(pts, closed, a, state, arr, not closed)
        elif name in ("circle", "ellipse"):
            cx, cy = _apply(state["m"], _num(a.get("cx", "0")), _num(a.get("cy", "0")))
            r = _num(a.get("r", a.get("rx", "0")))
            mm = state["m"]
            r = r * math.sqrt(abs(mm[0] * mm[3] - mm[1] * mm[2])) * scale_k
            fill = state["fill"]
            if fill == "":
                fill = "0000"
            stroke = state["stroke"]
            if stroke == "":
                stroke = None
            w = max(1, min(3, int(round(state["sw"] * scale_k))))
            if r > 0 and (fill or stroke):
                out.append("C %s %s %d %d %d %d" % (fill or "-", stroke or "-", w,
                                                    round(fx(cx)), round(fy(cy)), max(1, round(r))))
        elif name == "text":
            end = svg.find("</text>", i)
            if end < 0:
                continue
            inner = svg[i:end]
            i = end + 7
            # strip <tspan> tags, keep their text
            txt = ""
            k = 0
            while k < len(inner):
                t = inner.find("<", k)
                if t < 0:
                    txt += inner[k:]
                    break
                txt += inner[k:t]
                g2 = inner.find(">", t)
                k = g2 + 1 if g2 >= 0 else len(inner)
            label = _label(txt)
            if not label:
                continue
            x, y = _apply(state["m"], _num(a.get("x", "0")) + _num(a.get("dx", "0")),
                          _num(a.get("y", "0")) + _num(a.get("dy", "0")))
            col = state["fill"] or "0000"
            anc = {"middle": "m", "end": "r"}.get(state["anchor"], "l")
            out.append("T %s %s %d %d %s" % (col, anc, round(fx(x)), round(fy(y)), label[:24]))
    if not out:
        return None
    # labels last, so lines never cover them; then keep within the size limit
    shapes = [c for c in out if c[0] != "T"]
    texts = [c for c in out if c[0] == "T"]
    res = "DIA:"
    for c in shapes + texts:
        if len(res) + len(c) + 1 > DIA_MAX:
            break
        res += ("" if res == "DIA:" else "~") + c
    return res

def make_diagram(question, image=None):
    """GET_DIA: the chat model draws the diagram as SVG; reply DIA:... or ERR:...
    image: a photo (JPEG bytes) of the question, sent along with it."""
    if not question and not image:
        return "ERR:Describe the diagram first"
    deadline = time.ticks_add(time.ticks_ms(), (PHOTO_BUDGET if image else DIA_BUDGET) * 1000)
    why = ready_to_ask()
    if why:
        return "ERR:" + why
    prompt = photo_content(image, question or "Draw the diagram for the question in this photo.") if image else question
    last = ""
    for attempt in range(2):
        if attempt:
            recover_link()
        t0 = time.ticks_ms()
        try:
            stage(("Drawing with %s" if not attempt else "Retrying %s") % short_model())
            resp = hc_post(prompt, DIA_PROMPT, 4000, (), max(10, int(left(deadline))))
            if resp.status_code != 200:
                code = resp.status_code
                resp.close()
                return "ERR:" + describe_status(code)
            text, why = reply_text(resp)
            if not text:
                return "ERR:No diagram: " + why
            gc.collect()
            print("diagram: %.1fs, %d chars of SVG" % (time.ticks_diff(time.ticks_ms(), t0) / 1000, len(text)))
            try:  # kept for checking: mpremote connect COMx fs cat :last_diagram.svg
                with open("last_diagram.svg", "w") as f:
                    f.write("<!-- " + (question or "photo").replace("--", "-") + " -->\n" + text)
            except OSError:
                pass
            cmds = svg_to_commands(text)
            if cmds:
                return cmds
            said = to_ascii(text).strip().replace("\n", " ")[:80]
            return "ERR:The model did not draw a diagram~" + said
        except OSError as e:
            last = net_error(e) + weak_note()
            gc.collect()
            if time.ticks_diff(time.ticks_ms(), t0) > 20000 or left(deadline) < 25:
                break
            time.sleep(1 + attempt)
        except MemoryError:
            gc.collect()
            return "ERR:Out of memory. Try a shorter description."
        except Exception as e:
            return "ERR:" + str(e)[:60]
    return "ERR:" + last

# ---------------------------------------------------------------- camera
# Needs the camera build of MicroPython (mpy_cam, XIAO_ESP32S3). This board's
# camera can't make JPEGs itself, so: one raw greyscale photo (1024x768, sharp
# enough for printed text), compressed to JPEG here (~50 KB, under 0.5 s);
# the 128x96 preview for the calculator comes from the same photo.
# F6 on the calculator: GET_SNAP -> SNAP:256x192:<base64 of 4-bit greys>;
# then GET_ASKSNAP:<question> / GET_DIASNAP:<question> send the JPEG and the
# question to the chat model (it must accept images, e.g. Claude or Gemini).
try:
    from camera import Camera, PixelFormat, FrameSize
    import jpeg
except ImportError:
    Camera = None
LAST_SNAP = None          # JPEG bytes of the last photo
SNAP_W, SNAP_H = 256, 192  # preview size, drawn 1:1 on the calculator
PHOTO_DEFAULT = "Solve the question in this photo. Show full working."
PHOTO_SIZES = (("SXGA", 1280, 1024), ("XGA", 1024, 768), ("SVGA", 800, 600), ("VGA", 640, 480))
DOC_ENHANCE = True        # clean up photos for reading: even light, crisp text
DOC_CELL = 32             # lighting is measured in 32x32 pixel cells
DOC_PROMPT = ("This is a photo of a page, board or screen. First read ALL the text exactly, "
              "including maths, and describe every diagram: shapes, labels, axes, arrows, values, "
              "units. Then answer the request. If something is unreadable say which part. Request: ")
PHOTO_MIRROR = True       # this camera's picture is mirrored left-to-right


@micropython.viper
def _mirror_rows(buf: ptr8, w: int, h: int):
    """Reverse every row in place (the camera's picture is mirrored)."""
    r = 0
    while r < h:
        i = r * w
        j = i + w - 1
        while i < j:
            t = buf[i]
            buf[i] = buf[j]
            buf[j] = t
            i += 1
            j -= 1
        r += 1


@micropython.viper
def _preview(raw: ptr8, w: int, sx: int, sy: int, out: ptr8, ow: int, oh: int):
    """Shrink the photo: each preview pixel = mean of 4 samples in its cell."""
    hx = sx // 2
    hy = (sy // 2) * w
    k = 0
    y = 0
    while y < oh:
        r0 = y * sy * w
        x = 0
        while x < ow:
            c = r0 + x * sx
            out[k] = (raw[c] + raw[c + hx] + raw[c + hy] + raw[c + hy + hx]) >> 2
            k += 1
            x += 1
        y += 1


@micropython.viper
def _pack4(grey: ptr8, out: ptr8, n: int, lo: int, span: int):
    """Stretch lo..lo+span to 0..15 and pack two pixels a byte."""
    k = 0
    while k < n:
        a = (grey[k] - lo) * 15 // span
        b = (grey[k + 1] - lo) * 15 // span
        if a < 0:
            a = 0
        if a > 15:
            a = 15
        if b < 0:
            b = 0
        if b > 15:
            b = 15
        out[k >> 1] = (a << 4) | b
        k += 2


@micropython.viper
def _cells(raw: ptr8, w: int, h: int, cell: int, grid: ptr8, gw: int, gh: int):
    """Mean brightness of each cell (16 samples), i.e. a tiny copy of the photo."""
    st = cell >> 2
    cy = 0
    while cy < gh:
        cx = 0
        while cx < gw:
            tot = 0
            j = 0
            while j < 4:
                y = cy * cell + j * st + (st >> 1)
                if y >= h:
                    y = h - 1
                i = 0
                while i < 4:
                    x = cx * cell + i * st + (st >> 1)
                    if x >= w:
                        x = w - 1
                    tot += raw[y * w + x]
                    i += 1
                j += 1
            grid[cy * gw + cx] = tot >> 4
            cx += 1
        cy += 1


@micropython.viper
def _paper(grid: ptr8, gw: int, gh: int, out: ptr8):
    """Paper brightness per cell: the lightest of the 3x3 cells around it (ink
    is dark, so the lightest neighbour is the paper, not the writing)."""
    y = 0
    while y < gh:
        x = 0
        while x < gw:
            m = 0
            dy = -1
            while dy < 2:
                yy = y + dy
                if yy >= 0 and yy < gh:
                    dx = -1
                    while dx < 2:
                        xx = x + dx
                        if xx >= 0 and xx < gw:
                            v = grid[yy * gw + xx]
                            if v > m:
                                m = v
                        dx += 1
                dy += 1
            if m < 40:
                m = 40
            out[y * gw + x] = m
            x += 1
        y += 1


@micropython.viper
def _flatten(raw: ptr8, w: int, h: int, cell: int, paper: ptr8, gw: int, gh: int,
             rowbg: ptr8, white: int):
    """Divide out uneven lighting: paper -> white, ink stays dark. Paper
    brightness is blended smoothly between cell centres."""
    half = cell >> 1
    y = 0
    while y < h:
        gy = y - half
        if gy < 0:
            gy = 0
        iy = gy // cell
        fy = ((gy - iy * cell) * 256) // cell
        if iy >= gh - 1:
            iy = gh - 1
            fy = 0
        i = 0
        while i < gw:
            a = int(paper[iy * gw + i])
            b = a
            if fy > 0:
                b = int(paper[(iy + 1) * gw + i])
            rowbg[i] = (a * (256 - fy) + b * fy) >> 8
            i += 1
        x = 0
        base = y * w
        while x < w:
            gx = x - half
            if gx < 0:
                gx = 0
            ix = gx // cell
            fx = ((gx - ix * cell) * 256) // cell
            if ix >= gw - 1:
                ix = gw - 1
                fx = 0
            bg = int(rowbg[ix])
            if fx > 0:
                bg = (bg * (256 - fx) + int(rowbg[ix + 1]) * fx) >> 8
            v = raw[base + x] * white // bg
            if v > 255:
                v = 255
            raw[base + x] = v
            x += 1
        y += 1


@micropython.viper
def _sharpen(src: ptr8, dst: ptr8, w: int, h: int, d: int, amt: int):
    """Unsharp mask: out = v + amt/8 * (4v - four neighbours d pixels away)."""
    y = 0
    while y < h:
        base = y * w
        x = 0
        while x < w:
            v = int(src[base + x])
            if x >= d and x < w - d and y >= d and y < h - d:
                s = int(src[base + x - d]) + int(src[base + x + d]) + int(src[base + x - d * w]) + int(src[base + x + d * w])
                v = v + (amt * (4 * v - s)) // 8
                if v < 0:
                    v = 0
                if v > 255:
                    v = 255
            dst[base + x] = v
            x += 1
        y += 1


@micropython.viper
def _apply_lut(buf: ptr8, n: int, lut: ptr8):
    i = 0
    while i < n:
        buf[i] = lut[buf[i]]
        i += 1


def enhance_page(raw, w, h):
    """Make a photo of a page easier to read (for people and for the AI):
    1. even out the light (shadows, glare gradients), paper -> white
    2. sharpen edges (mild focus blur, thin pen strokes)
    3. stretch contrast so ink is properly black
    Returns the new buffer (raw is reused/freed by the caller)."""
    gw = (w + DOC_CELL - 1) // DOC_CELL
    gh = (h + DOC_CELL - 1) // DOC_CELL
    grid = bytearray(gw * gh)
    paper = bytearray(gw * gh)
    _cells(raw, w, h, DOC_CELL, grid, gw, gh)
    _paper(grid, gw, gh, paper)
    _flatten(raw, w, h, DOC_CELL, paper, gw, gh, bytearray(gw + 1), 238)
    out = bytearray(w * h)
    _sharpen(raw, out, w, h, 2, 6)
    # contrast: darkest 1% -> black, paper stays white; a little extra bite
    hist = [0] * 256
    for k in range(0, w * h, 7):
        hist[out[k]] += 1
    n = sum(hist)
    acc = 0
    lo = 0
    for v in range(256):
        acc += hist[v]
        if acc >= n // 100:
            lo = v
            break
    hi = 235
    span = max(40, hi - lo)
    lut = bytearray(256)
    for v in range(256):
        t = (v - lo) * 255 // span
        lut[v] = 0 if t < 0 else (255 if t > 255 else t)
    _apply_lut(out, w * h, lut)
    return out


def _raw_photo():
    """(width, height, greyscale bytes) at the largest size that works. The
    camera is switched off again afterwards: left running it draws power."""
    last = ""
    for name, w, h in PHOTO_SIZES:
        size = getattr(FrameSize, name, None)
        if size is None:
            continue
        cam = None
        try:
            cam = Camera(pixel_format=PixelFormat.GRAYSCALE, frame_size=size)
            time.sleep_ms(300)
            raw = None
            for i in range(8):        # the first frames can be dark or missing
                img = cam.capture()
                if img is not None and len(img) >= w * h and i >= 2:
                    raw = bytearray(img)
                    break
                time.sleep_ms(100)
            if raw:
                return w, h, raw
            last = name + ": no frame"
        except Exception as e:
            last = "%s: %s" % (name, e)
        finally:
            try:
                if cam is not None:
                    cam.deinit()
            except Exception:
                pass
            camera_standby()
            gc.collect()
        print("camera", last)
    raise OSError(last)


def take_snapshot():
    """GET_SNAP: reply SNAP:256x192:<base64 of 4-bit greys> or ERR:..."""
    global LAST_SNAP
    if Camera is None:
        return "ERR:No camera support~Flash the camera firmware (mpy_cam XIAO_ESP32S3)."
    set_cpu(FAST_HZ)
    t0 = time.ticks_ms()
    try:
        w, h, raw = _raw_photo()
    except Exception as e:
        return "ERR:Camera failed~" + str(e)[:90] + "~Is the camera board clicked on fully?"
    # This camera's image comes out mirrored left-to-right: flip each row
    # (otherwise text reads backwards, for you and for the AI).
    if PHOTO_MIRROR:
        _mirror_rows(raw, w, h)
    if DOC_ENHANCE:
        try:
            raw = enhance_page(raw, w, h)
            gc.collect()
        except Exception as e:      # out of memory etc: send the plain photo
            print("enhance failed:", e)
            gc.collect()
    # preview: average 4 samples per preview pixel, then stretch the contrast
    # (darkest 2% -> black, brightest 2% -> white) into 16 grey levels
    grey = bytearray(SNAP_W * SNAP_H)
    _preview(raw, w, w // SNAP_W, h // SNAP_H, grey, SNAP_W, SNAP_H)
    srt = sorted(grey[k] for k in range(0, len(grey), 11))
    lo = srt[len(srt) // 50]
    hi = srt[-1 - len(srt) // 50]
    out = bytearray(SNAP_W * SNAP_H // 2)
    _pack4(grey, out, SNAP_W * SNAP_H, lo, max(1, hi - lo))
    del grey
    try:
        jpg = jpeg.Encoder(width=w, height=h, pixel_format="GRAY", quality=82).encode(raw)
    except Exception as e:
        return "ERR:Could not compress the photo~" + str(e)[:80]
    del raw
    gc.collect()
    LAST_SNAP = jpg
    try:  # for checking on a computer: mpremote connect COMx cp :last_photo.jpg .
        with open("last_photo.jpg", "wb") as f:
            f.write(jpg)
    except OSError:
        pass
    print("photo: %dx%d, %d KB, %.1fs" % (w, h, len(jpg) // 1024,
                                        time.ticks_diff(time.ticks_ms(), t0) / 1000))
    return b"SNAP:%dx%d:" % (SNAP_W, SNAP_H) + binascii.b2a_base64(out).strip()


def photo_content(jpg, question):
    """A user message with the photo and the question, for vision models."""
    return [
        {"type": "image_url",
         "image_url": {"url": "data:image/jpeg;base64," + binascii.b2a_base64(jpg).decode().strip()}},
        {"type": "text", "text": question},
    ]


def last_photo():
    """The last photo: from RAM, or from flash if the XIAO slept since."""
    global LAST_SNAP
    if LAST_SNAP is None:
        try:
            with open("last_photo.jpg", "rb") as f:
                LAST_SNAP = f.read()
        except OSError:
            pass
    return LAST_SNAP


def ask_snapshot(question):
    """GET_ASKSNAP: ask about the last photo; plain text reply like a question."""
    if last_photo() is None:
        return "error: Take a photo first (F6)."
    q = (DOC_PROMPT if DOC_ENHANCE else "") + (question or PHOTO_DEFAULT)
    deadline = time.ticks_add(time.ticks_ms(), PHOTO_BUDGET * 1000)
    why = ready_to_ask()
    if why:
        return "error: " + why
    last = ""
    for attempt in range(2):
        if attempt:
            recover_link()
        t0 = time.ticks_ms()
        try:
            gc.collect()
            stage(("Sending photo to %s" if not attempt else "Retrying %s") % short_model())
            resp = hc_post(photo_content(LAST_SNAP, q), SYSTEM_PROMPT, 4000, (),
                           max(10, int(left(deadline))))
            if resp.status_code != 200:
                code = resp.status_code
                resp.close()
                if code in (400, 404):
                    return "error: this model can't read photos (HTTP %d)~Pick Claude or Gemini in Settings." % code
                return "error: " + describe_status(code)
            text, why = reply_text(resp)
            if not text:
                return "error: " + why
            reply = encode_reply(text)
            remember_turn("[photo] " + q, reply.replace("~", "\n"))
            return reply
        except OSError as e:
            last = net_error(e) + weak_note()
            gc.collect()
            if time.ticks_diff(time.ticks_ms(), t0) > 20000 or left(deadline) < 25:
                break
            time.sleep(1 + attempt)
        except MemoryError:
            gc.collect()
            return "error: out of memory~Retake the photo (F6) and try again."
        except Exception as e:
            return "error: " + str(e)[:60]
    return "error: " + last


RESTART_AFTER = False   # set by do_update: restart once the reply has gone

BOOT_PY_NOTE = "# G-Intelligence: if an update was interrupted, put the old main.py back"
BOOT_PY = BOOT_PY_NOTE + """
import os
try:
    os.stat("main.py")
except OSError:
    try:
        os.rename("main.bak", "main.py")
    except OSError:
        pass
"""


def _exists(path):
    import os
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remove(path):
    import os
    try:
        os.remove(path)
    except OSError:
        pass


def _copy(src, dst):
    with open(src, "rb") as a, open(dst, "wb") as b:
        while True:
            chunk = a.read(1024)
            if not chunk:
                break
            b.write(chunk)


def _digest(path):
    """A fingerprint of a file (SHA-256 where available, else its size)."""
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024)
                if not chunk:
                    break
                h.update(chunk)
        return binascii.hexlify(h.digest())
    except Exception:
        import os
        return os.stat(path)[6]


def _version_of(src):
    i = src.find('\nVERSION = "')
    if i < 0:
        return "?"
    i += len('\nVERSION = "')
    return src[i:src.find('"', i)][:20]


def check_new(path):
    """(version, '') for a usable new main.py, else ('', reason)."""
    gc.collect()
    try:
        with open(path) as f:
            src = f.read()
    except Exception as e:
        return "", "could not read it back: " + str(e)[:30]
    if not src.rstrip().endswith("_run()") or "def main():" not in src \
            or "def handle_line(" not in src:
        # cut off part-way, or an error page, or some other file
        return "", "it is not a complete main.py"
    try:
        compile(src, "main.py", "exec")
    except MemoryError:
        pass      # not enough RAM to check: the markers above will do
    except NameError:
        pass      # this build has no compile()
    except SyntaxError as e:
        return "", "it has a syntax error: " + str(e)[:30]
    return _version_of(src), ""


def do_update():
    """GET_UPDATE: fetch main.py from UPDATE_URL, check it, keep the old one
    as main.bak, swap the new one in and restart (after the reply is sent).
    An update that crashes before it has served a request is rolled back
    automatically (see _run)."""
    global RESTART_AFTER
    import os
    if not UPDATE_URL:
        return ("ERR:No update address set.~Add this line to .env on the XIAO:~"
                "UPDATE_URL=https://.../main.py~then copy it over: "
                "mpremote connect COMx cp .env :.env")
    if not UPDATE_URL.startswith("https://"):
        return "ERR:UPDATE_URL must start with https://"
    why = wifi_ready()
    if why:
        return "ERR:" + why
    stage("Downloading update")
    headers = {"User-Agent": "G-Intelligence"}
    if UPDATE_TOKEN:
        headers["Authorization"] = "token " + UPDATE_TOKEN
    gc.collect()
    try:
        try:
            resp = urequests.get(UPDATE_URL, headers=headers, timeout=30)
        except TypeError:
            resp = urequests.get(UPDATE_URL, headers=headers)
    except Exception as e:
        return "ERR:Download failed: " + net_error(e)
    done = False
    try:
        if resp.status_code != 200:
            return "ERR:Update server: HTTP %d%s" % (
                resp.status_code, " (private repo? add UPDATE_TOKEN)" if resp.status_code == 404 else "")
        want = 0
        try:
            for k, v in resp.headers.items():
                if k.lower() == "content-length":
                    want = int(v)
        except Exception:
            pass
        if want > UPDATE_MAX:
            return "ERR:Update file is too big (%d bytes)" % want
        got = 0
        end = time.ticks_add(time.ticks_ms(), 60000)
        with open("main.new", "wb") as f:
            raw = getattr(resp, "raw", None)
            if raw is not None and hasattr(raw, "read"):
                while True:
                    chunk = raw.read(1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > UPDATE_MAX:
                        return "ERR:Update file is too big"
                    if time.ticks_diff(end, time.ticks_ms()) < 0:
                        return "ERR:Download too slow, try again"
                    f.write(chunk)
            else:
                data = resp.content
                got = len(data)
                f.write(data)
        if want and got != want:
            return "ERR:Download cut short (%d of %d bytes), try again" % (got, want)
        done = True
    except Exception as e:
        return "ERR:Download failed: " + net_error(e)
    finally:
        try:
            resp.close()
        except Exception:
            pass
        if not done:
            _remove("main.new")   # a half-downloaded file is never kept
    gc.collect()
    new_version, why = check_new("main.new")
    if why:
        _remove("main.new")
        return "ERR:Update refused: " + why + ".~The old main.py is unchanged."
    if _digest("main.new") == _digest("main.py"):
        _remove("main.new")
        return "OK:Already up to date.~main.py " + VERSION
    stage("Installing update")
    try:
        _copy("main.py", "main.bak")          # keep the working one
        if not _exists("boot.py") or BOOT_PY_NOTE not in open("boot.py").read():
            with open("boot.py", "a") as f:   # recovery if power dies mid-swap
                f.write("\n" + BOOT_PY)
        with open("upd.pending", "w") as f:
            f.write(VERSION + ">" + new_version)
        try:
            os.rename("main.new", "main.py")  # replaces main.py in one step
        except OSError:
            os.remove("main.py")
            os.rename("main.new", "main.py")
    except Exception as e:
        _remove("upd.pending")
        _remove("main.new")
        return "ERR:Could not install: " + str(e)[:40] + ".~The old main.py is kept."
    RESTART_AFTER = True
    return "OK:Updated %s to %s.~The XIAO restarts now.~If it fails to start it goes back to the old one." % (VERSION, new_version)


def update_ok():
    """First request served: the update works, stop watching it."""
    if _exists("upd.pending"):
        _remove("upd.pending")


def rollback():
    """The new main.py keeps crashing: put the old one back and restart."""
    import os
    print("update crashing: going back to main.bak")
    try:
        if _exists("main.bak"):
            if _exists("main.py"):
                os.remove("main.py")
            os.rename("main.bak", "main.py")
    except Exception as e:
        print("rollback failed:", e)
    _remove("upd.pending")
    time.sleep(1)
    machine.reset()


def handle_line(line):
    global MODEL, CURRENT, PENDING_SSID
    if line == "GET_UPDATE":
        try:
            return do_update()
        except Exception as e:
            return "ERR:Update crashed: " + str(e)[:50]

    if line == "GET_ALL":
        # wifi field: 1 connected, S asleep (rejoins on the next question), 0 offline
        up = wlan.isconnected()
        state = "1" if up else ("S" if ASLEEP and CURRENT else "0")
        return "OK:%s|%d|%s|%s" % (MODEL, len(NETWORKS), state, CURRENT if state != "0" else "")
    if line == "GET_NETS":
        try:
            return nets_reply()
        except Exception as e:
            return "ERR:Scan failed: " + str(e)[:40]
    if line == "GET_TEST":
        try:
            return run_test()
        except Exception as e:
            return "ERR:Test crashed: " + str(e)[:60]
    if line.startswith("SET_MODEL:"):
        name = line[len("SET_MODEL:"):].strip()
        if not name:
            return "ERR:Model name is empty"
        MODEL = name
        save_settings()
        return "OK:Model " + name
    if line == "GET_SNAP":
        return take_snapshot()
    if line.startswith("GET_ASKSNAP:"):
        return ask_snapshot(line[len("GET_ASKSNAP:"):].strip())
    if line.startswith("GET_DIASNAP:"):
        if last_photo() is None:
            return "ERR:Take a photo first (F6)."
        return make_diagram(line[len("GET_DIASNAP:"):].strip(), LAST_SNAP)
    if line.startswith("GET_DIA:"):
        return make_diagram(line[len("GET_DIA:"):].strip())
    if line.startswith("SET_NEWCHAT:"):
        HISTORY.clear()
        save_history()
        return "OK:New chat started"
    if line.startswith("SET_SSID:"):
        value = line[len("SET_SSID:"):].strip()
        if not value:
            return "ERR:SSID is empty"
        PENDING_SSID = value
        return "OK:SSID ready"
    if line.startswith("SET_PASS:"):
        if not PENDING_SSID:
            return "ERR:Pick a network first"
        remember(PENDING_SSID, line[len("SET_PASS:"):].strip())
        return "OK:Password saved"
    if line.startswith("SET_JOIN:"):
        ssid = line[len("SET_JOIN:"):].strip()
        i = known_index(ssid)
        if i < 0:
            return "ERR:Network not saved"
        ok, msg = wifi_connect(ssid, NETWORKS[i][1])
        if ok:
            CURRENT = ssid
            mark_recent(ssid)
        return ("OK:" if ok else "ERR:") + msg
    if line.startswith("SET_FORGET:"):
        ssid = line[len("SET_FORGET:"):].strip()
        i = known_index(ssid)
        if i < 0:
            return "ERR:Network not saved"
        NETWORKS.pop(i)
        if CURRENT == ssid:
            CURRENT = ""
        save_settings()
        return "OK:Forgot " + ssid
    if line.startswith("SET_CONNECT:"):
        return reconnect_wifi()
    if line.startswith("SET_") or line.startswith("GET_"):
        return "ERR:Unknown command"

    return ask_ai(line)


def send_reply(reply):
    """Send a reply in small paced chunks. The calculator's serial receive buffer
    is small, so a long reply sent in one go can overflow it and lose the newline."""
    data = (reply if isinstance(reply, bytes) else reply.encode()) + b"\n"
    chunk, gap = PACING
    if len(data) > 12000:
        chunk, gap = 64, 4  # photo previews: ~6.5 KB/s (the calculator only draws meanwhile)
    for i in range(0, len(data), chunk):
        uart.write(data[i:i + chunk])
        time.sleep_ms(gap)


def _printable(line):
    # keep only printable ASCII: MicroPython's bytes.decode() does not
    # accept errors=..., and the calculator only ever sends 32..126
    return "".join(chr(b) for b in line if 32 <= b < 127).strip()


def take_lines(buf):
    """Split complete lines off buf -> (list of non-empty lines, remainder)."""
    if b"\n" not in buf:
        return [], buf
    parts = buf.split(b"\n")
    rest = parts.pop()
    return [t for t in (_printable(p) for p in parts) if t], rest


def main():
    global ASLEEP, HOLD_S, LAST_USE
    try:
        Pin(LED_PIN, Pin.OUT, value=1)  # user LED off (the charge LED is hardware)
    except Exception:
        pass
    woke = machine.reset_cause() == machine.DEEPSLEEP_RESET
    by_line = woke and machine.wake_reason() == machine.EXT0_WAKE
    # A timer wake is either the hourly safety check (nothing to do), or,
    # after repeated false wakes, the "listen for a moment" poll.
    polling = woke and not by_line and false_wakes() >= BACKOFF_AFTER
    if woke and not by_line and not polling:
        count_wake("timer")
        load_rtc()    # keep the saved network for the next real wake
        deep_sleep()
    if by_line:
        count_wake("ext0")
    load_settings()
    load_history()
    buf = b""
    heard = False  # any byte at all since waking
    if woke:
        # Woken by the calculator: no WiFi yet (the first question joins it).
        # Bytes that arrived while booting are partial lines, so ignore
        # everything until a complete GET_ALL, the calculator's wake call.
        load_rtc()
        ASLEEP = True
        synced = False
        idle_limit = UNSYNCED_S
        print("listening after a noisy line" if polling else "woke for the calculator")
    else:
        # Power-up or reset. Log it: lots of these with nobody plugging
        # anything in means brownouts (WiFi current spikes resetting a weak
        # battery), which would keep the XIAO awake for ever.
        count_wake("boot")
        false_wakes(0)
        load_rtc()  # after a reset (not a power cut) the last network is still known
        camera_standby()
        print("remembered messages:", len(HISTORY))
        print("model:", MODEL, "| saved networks:", [n[0] for n in NETWORKS])
        # No WiFi yet: it costs 100-170 mA and the first question joins it
        # anyway. (Joining here on every power-up/reset kept the radio busy
        # for minutes and could feed a brownout reset loop.)
        ASLEEP = True
        print("WiFi joins on the first question")
        synced = True
        idle_limit = FIRST_SLEEP_S
        # If GI was waiting for an answer when we restarted (battery dip,
        # crash), this tells it to send the request again.
        try:
            uart.write(b"\x02!boot\n")
        except Exception:
            pass
    LAST_USE = time.ticks_ms()
    set_cpu(IDLE_HZ)  # full speed only once there is work to do
    print("listening for the calculator...")
    while True:
        # The idle timer only restarts on a complete, valid line, so check it
        # every pass. (It used to be checked only when no bytes were waiting:
        # noise on the line with the calculator off kept the XIAO awake at
        # ~40 mA forever -- the 8-hour battery.)
        idle = time.ticks_diff(time.ticks_ms(), LAST_USE)
        if idle > max(idle_limit, HOLD_S) * 1000 or \
                (not synced and not heard and idle > QUIET_S * 1000):
            if not synced:
                # woken with nobody there
                if by_line:
                    count_wake("false")
                false_wakes(false_wakes() + 1)
            deep_sleep()
        if uart.any():
            buf += uart.read() or b""
            heard = True
            if len(buf) > 600 and b"\n" not in buf:
                buf = b""  # garbage with no newline: start over
        lines, buf = take_lines(buf)
        if lines:
            if not synced:
                if "GET_ALL" not in lines:
                    print("ignored while waking:", lines)
                    continue
                lines = lines[lines.index("GET_ALL"):]
            # GI waits for one reply at a time, so if several lines queued up
            # (wake calls repeated while we booted), only the newest needs one.
            text = lines[-1]
            ack()
            if not synced:
                false_wakes(0)
            synced = True
            idle_limit = IDLE_OFF_S
            HOLD_S = 180 if text == "GET_SNAP" else 0
            touch()
            print("from calculator:", text)
            try:
                reply = handle_line(text)
            except MemoryError:
                gc.collect()
                reply = "ERR:Out of memory, try again"
            except Exception as e:  # never let one bad request kill the loop
                reply = "ERR:" + str(e)[:60]
            gc.collect()
            print("reply:", reply if len(reply) < 300 else reply[:40])
            # Did GI give up on this one (timeout or EXIT) and send something
            # new while we worked? Then it is waiting for THAT answer: drop
            # this stale reply and handle the new request instead.
            if uart.any():
                buf += uart.read() or b""
            if _printable(buf):
                print("calculator moved on: dropped that reply")
                touch()
                continue
            buf = b""
            send_reply(reply)
            clear_crashes()
            if RESTART_AFTER:
                time.sleep(1)
                machine.reset()
            if not reply.startswith("ERR:"):
                update_ok()
            touch()  # the idle timer starts once the reply has gone
        elif not uart.any():
            # No background WiFi rescans: every question rejoins by itself
            # (ready_to_ask -> wake_wifi). Radio off soon after the last
            # reply, or at once if the connection has dropped by itself.
            if not ASLEEP and (idle > WIFI_OFF_S * 1000 or
                               (idle > 5000 and not wlan.isconnected())):
                wifi_sleep()
            elif ASLEEP and idle > 3000:
                set_cpu(IDLE_HZ)
            time.sleep(0.05)

def _run():
    """Never leave the XIAO sitting at the >>> prompt: if GI crashes, save the
    error to crash.txt (read it with: mpremote connect COMx fs cat :crash.txt)
    and restart. Ctrl+C from mpremote still stops it for maintenance."""
    try:
        main()
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        try:
            import sys
            with open("crash.txt", "w") as f:
                f.write("t=%d\n" % time.ticks_ms())
                sys.print_exception(e, f)
            sys.print_exception(e)
        except Exception:
            pass
        n = count_crash()
        if n >= 2 and _exists("upd.pending"):
            rollback()   # a fresh update that keeps crashing: undo it
        if n >= 3:
            # Crashing over and over (bad file, broken camera...): don't
            # restart forever and flatten the battery. Sleep until the
            # calculator calls, then try again.
            print("GI crashed %d times in a row; sleeping (see crash.txt)" % n)
            time.sleep(3)
            try:
                deep_sleep()
            except BaseException:
                machine.deepsleep(SAFETY_WAKE_MS)
        print("GI crashed; restarting in 3 s (see crash.txt)")
        time.sleep(3)
        machine.reset()


_run()
