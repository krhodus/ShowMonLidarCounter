#!/usr/bin/env python3
"""                                                                                                   
          ░██████   ░██     ░██   ░██████   ░██       ░██ ░███     ░███   ░██████   ░███    ░██ 
         ░██   ░██  ░██     ░██  ░██   ░██  ░██       ░██ ░████   ░████  ░██   ░██  ░████   ░██ 
        ░██         ░██     ░██ ░██     ░██ ░██  ░██  ░██ ░██░██ ░██░██ ░██     ░██ ░██░██  ░██ 
         ░████████  ░██████████ ░██     ░██ ░██ ░████ ░██ ░██ ░████ ░██ ░██     ░██ ░██ ░██ ░██ 
                ░██ ░██     ░██ ░██     ░██ ░██░██ ░██░██ ░██  ░██  ░██ ░██     ░██ ░██  ░██░██ 
         ░██   ░██  ░██     ░██  ░██   ░██  ░████   ░████ ░██       ░██  ░██   ░██  ░██   ░████ 
          ░██████   ░██     ░██   ░██████   ░███     ░███ ░██       ░██   ░██████   ░██    ░███ 


Light Show Network ShowMon Lidar Car Counter
v1.0

"""

import copy
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
import subprocess

import requests
import paho.mqtt.client as mqtt
import serial
from flask import Flask, jsonify, request, render_template

DEBUG_SENSOR = False

# ----------------- PATHS & CONFIG -----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "config.json")
SCHED_LOCAL_PATH = os.path.join(BASE_DIR, "schedule.json")

if not os.path.exists(CFG_PATH):
    raise SystemExit("Missing config.json")

with open(CFG_PATH, "r") as f:
    cfg = json.load(f)

# --- Top-level config ---
SERIAL_PORT = cfg.get("serial_port", "/dev/serial0")
BAUD = cfg.get("baudrate", 115200)
DB_PATH = os.path.join(BASE_DIR, cfg.get("database", "cars.db"))

MQTT_CFG = cfg.get("mqtt", {})
MQTT_BROKER = MQTT_CFG.get("broker", "localhost")
MQTT_PORT = MQTT_CFG.get("port", 1883)
MQTT_TOPIC = MQTT_CFG.get("topic", "carcount/car_detect")
MQTT_USER = MQTT_CFG.get("username") or None
MQTT_PASS = MQTT_CFG.get("password") or None

HTTP_CFG = cfg.get("http", {})
HTTP_HOST = HTTP_CFG.get("host", "0.0.0.0")
HTTP_PORT = int(HTTP_CFG.get("port", 80))

DETECTION_CFG = cfg.get("detection", {})
DEBOUNCE_MS = int(DETECTION_CFG.get("debounce_ms", 200))
IGNORE_ZERO_DISTANCE = bool(DETECTION_CFG.get("ignore_zero_distance", True))
MIN_STRENGTH = int(DETECTION_CFG.get("min_strength", 0))
TEST_MODE_DEFAULT = bool(DETECTION_CFG.get("test_mode", False))

SCHEDULE_CFG = cfg.get("schedule", {})
SCHEDULE_URL = SCHEDULE_CFG.get("url", "")
GITHUB_TOKEN = SCHEDULE_CFG.get("github_token", "")
SCHEDULE_REFRESH_SECONDS = int(SCHEDULE_CFG.get("check_interval_sec", 1200))

# Webhook URL is read from cfg at post time so changes apply without a restart

cfg_lock = threading.RLock()


def write_json_atomic(path, data):
    """Write JSON via temp file + rename so a power cut can't leave a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ----------------- DB -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            distance_mm INTEGER NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    cur.execute(
        "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?);",
        ("total_count", "0"),
    )
    conn.commit()
    return conn


db_conn = init_db()
db_lock = threading.Lock()


def increment_count_and_save(distance_mm: int):
    ts = datetime.now(timezone.utc).isoformat()
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO detections (ts_utc, distance_mm) VALUES (?, ?);",
            (ts, int(distance_mm)),
        )
        cur.execute(
            "UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'total_count';"
        )
        db_conn.commit()


def get_total_count() -> int:
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT value FROM metadata WHERE key = 'total_count';")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def reset_total_count():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("UPDATE metadata SET value = 0 WHERE key='total_count';")
        db_conn.commit()


def wipe_database():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("DELETE FROM detections;")
        cur.execute("UPDATE metadata SET value = 0 WHERE key='total_count';")
        db_conn.commit()

# ----------------- MQTT -----------------
mqtt_client = mqtt.Client()
if MQTT_USER:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.max_queued_messages_set(200)  # don't grow without bound while broker is down
if MQTT_BROKER:
    try:
        # connect_async + loop_start keeps retrying if the broker is down at boot
        mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
    except Exception as e:
        print("Warning: could not start MQTT client:", e)
else:
    print("MQTT broker not configured; MQTT publishing disabled")


def publish_detection(distance_mm: int):
    now_utc = datetime.now(timezone.utc).isoformat()

    with db_lock:
        cur = db_conn.cursor()
        # Today (local)
        cur.execute(
            "SELECT COUNT(*) FROM detections "
            "WHERE date(ts_utc, 'localtime') = date('now', 'localtime');"
        )
        count_today = cur.fetchone()[0]

        # Last 7 days
        cur.execute(
            "SELECT COUNT(*) FROM detections "
            "WHERE datetime(ts_utc) >= datetime('now','-7 days');"
        )
        count_week = cur.fetchone()[0]

        # All time
        cur.execute("SELECT value FROM metadata WHERE key='total_count';")
        row = cur.fetchone()
        total_all_time = int(row[0]) if row else 0

    payload = {
        "ts_utc": now_utc,
        "count_today": count_today,
        "count_week": count_week,
        "total_all_time": total_all_time,
        "last_distance_mm": int(distance_mm),
    }

    if not MQTT_BROKER:
        return
    try:
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=1)
    except Exception as e:
        print("MQTT publish failed:", e)

# ----------------- TFmini parsing -----------------
def parse_tfmini_frame(buf: bytes):
    """
    TFmini/TFmini-Plus UART frame:
    0: 0x59
    1: 0x59
    2: dist_L
    3: dist_H
    4: strength_L
    5: strength_H
    6: temp_L
    7: temp_H
    8: checksum = sum(bytes[0:8]) & 0xFF
    """
    if len(buf) < 9:
        return None
    if buf[0] != 0x59 or buf[1] != 0x59:
        return None
    dist_l = buf[2]
    dist_h = buf[3]
    distance = dist_l + (dist_h << 8)
    strength = buf[4] + (buf[5] << 8)
    chksum = sum(buf[0:8]) & 0xFF
    if chksum != buf[8]:
        return None
    return {"distance_mm": distance, "strength": strength}

# ----------------- Global state -----------------
state_lock = threading.Lock()

state = {
    "last_distance_mm": None,
    "last_strength": None,
    "last_valid_reading": False,
    "car_present": False,
    "last_transition_ts": 0,
}

# test mode and manual override
test_mode = TEST_MODE_DEFAULT
test_count_today = 0
manual_override = False

# schedule
schedule_data = None  # list of 7 entries or None
schedule_last_fetch_utc = None
schedule_last_status = "Not fetched yet"
schedule_valid = False
schedule_active = False

# ----------------- Schedule helpers -----------------
def compute_schedule_flags(now_local: datetime):
    """
    Returns (valid, active, enable) for the given local time.
    - valid: schedule entry is present and times parse
    - active: now between StartShow and ShowStop (including overnight wrap)
    - enable: entry.Enable is True
    """
    global schedule_data

    with state_lock:
        data = schedule_data

    if not data:
        return False, False, False

    try:
        # Python: Monday = 0 ... Sunday = 6
        # Schedule: Sunday index 0 ... Saturday = 6
        weekday = now_local.weekday()  # 0=Mon..6=Sun
        idx = (weekday + 1) % 7  # map Mon->1, ..., Sun->0

        entry = data[idx]
        enable = bool(entry.get("Enable", False))
        start_str = entry.get("StartShow")
        stop_str = entry.get("ShowStop")

        if not start_str or not stop_str:
            return False, False, enable

        start_t = datetime.strptime(start_str, "%H:%M").time()
        stop_t = datetime.strptime(stop_str, "%H:%M").time()
    except Exception:
        return False, False, False

    now_t = now_local.time()

    # support overnight ranges (e.g. 23:00–02:00)
    if start_t <= stop_t:
        active = start_t <= now_t <= stop_t
    else:
        active = now_t >= start_t or now_t <= stop_t

    return True, active, enable


def refresh_schedule():
    """
    Fetch schedule JSON from GitHub (or other URL) and update schedule_data.
    Also writes to local schedule.json if successful.
    """
    global schedule_data, schedule_last_fetch_utc, schedule_last_status

    url = SCHEDULE_URL
    if not url:
        with state_lock:
            schedule_last_fetch_utc = datetime.now(timezone.utc).isoformat()
            schedule_last_status = "No schedule URL configured"
        return False

    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or len(data) != 7:
            raise ValueError("Schedule JSON must be a list of 7 entries")

        # Store schedule
        with state_lock:
            globals()["schedule_data"] = data
            globals()["schedule_last_fetch_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            globals()["schedule_last_status"] = "OK"

        # Write local copy
        try:
            write_json_atomic(SCHED_LOCAL_PATH, data)
        except Exception as e:
            print("Warning: could not write local schedule.json:", e)
            
        # schedule evaluation
        try:
            now_local = datetime.now()
            valid, active, enabled = compute_schedule_flags(now_local)
            with state_lock:
                globals()["schedule_valid"] = valid
                globals()["schedule_active"] = (
                    True if manual_override else (valid and enabled and active)
                )
        except Exception as e:
            print("Immediate schedule eval failed:", e)

        return True

    except Exception as e:
        with state_lock:
            globals()["schedule_last_fetch_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            globals()["schedule_last_status"] = f"Error: {e}"
        return False
    

def load_local_schedule():
    """
    Load schedule from local schedule.json and update schedule_data.
    Used by /api/schedule/local.
    """
    global schedule_data, schedule_last_fetch_utc, schedule_last_status

    if not os.path.exists(SCHED_LOCAL_PATH):
        return False

    try:
        with open(SCHED_LOCAL_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list) or len(data) != 7:
            raise ValueError("schedule.json must be a list of 7 entries")

        with state_lock:
            globals()["schedule_data"] = data
            globals()["schedule_last_fetch_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            globals()["schedule_last_status"] = "Loaded from local schedule.json"

        return True
    except Exception as e:
        print("Error loading local schedule:", e)
        return False


def schedule_loop():
    # initial fetch
    refresh_schedule()
    while True:
        time.sleep(max(30, SCHEDULE_REFRESH_SECONDS))
        refresh_schedule()

# ----------------- Sensor loop -----------------
def dlog(msg):
    if DEBUG_SENSOR:
        print("DEBUG:", msg)


def sensor_loop():
    global IGNORE_ZERO_DISTANCE, MIN_STRENGTH, DEBOUNCE_MS, DEBUG_SENSOR

    def open_serial():
        """Block until the serial port opens, retrying every 5 seconds."""
        while True:
            try:
                port = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
                print(f"Opened serial port {SERIAL_PORT} @ {BAUD}")
                return port
            except Exception as e:
                print("Could not open serial port (retrying in 5s):", e)
                time.sleep(5)

    ser = open_serial()

    read_buf = bytearray()

    car_active = False
    last_state_is_car = False
    car_since_ms = None
    empty_since_ms = int(time.time() * 1000)

    dlog("Sensor loop started...")

    while True:
        try:
            chunk = ser.read(64)
            if not chunk:
                continue
            read_buf.extend(chunk)

            while True:
                idx = read_buf.find(b"\x59\x59")
                if idx == -1:
                    if len(read_buf) > 2:
                        read_buf = read_buf[-2:]
                    break

                if idx > 0:
                    read_buf = read_buf[idx:]

                if len(read_buf) < 9:
                    break

                frame = bytes(read_buf[:9])
                read_buf = read_buf[9:]

                parsed = parse_tfmini_frame(frame)
                if parsed is None:
                    continue

                distance_mm = parsed["distance_mm"]
                strength = parsed["strength"]
                now_ms = int(time.time() * 1000)

                # ---- Classify sample ----
                valid_reading = True
                if IGNORE_ZERO_DISTANCE and distance_mm == 0:
                    valid_reading = False
                if MIN_STRENGTH and strength < MIN_STRENGTH:
                    valid_reading = False

                # "Car sample" = any valid sample with non-zero distance
                is_car_sample = valid_reading and distance_mm > 0

                # ---- Update state for API ----
                with state_lock:
                    state["last_distance_mm"] = distance_mm
                    state["last_strength"] = strength
                    state["last_valid_reading"] = valid_reading

                    sched_ok = schedule_active or manual_override
                    tm = test_mode

                # ---- Debounce logic ----
                if is_car_sample:
                    if not last_state_is_car:
                        car_since_ms = now_ms
                    last_state_is_car = True

                    if not car_active and car_since_ms is not None:
                        if now_ms - car_since_ms >= DEBOUNCE_MS:

                            # Enforce schedule BEFORE counting
                            if not sched_ok:
                                dlog(
                                    "*** CAR DETECTED but schedule inactive — IGNORING ***"
                                )
                                car_active = True  # latch but do NOT count
                                with state_lock:
                                    state["car_present"] = True
                                continue

                            dlog("*** CAR DETECTED! Counting car now ***")
                            car_active = True

                            threading.Thread(
                                target=handle_detection,
                                args=(distance_mm, not tm),  # log only if not test mode
                                daemon=True,
                            ).start()

                else:
                    if last_state_is_car:
                        empty_since_ms = now_ms
                    last_state_is_car = False

                    if car_active and empty_since_ms is not None:
                        if now_ms - empty_since_ms >= DEBOUNCE_MS:
                            dlog("Car ended — empty state stable")
                            car_active = False

                with state_lock:
                    state["car_present"] = car_active

        except Exception as e:
            print("Serial read error:", e)
            time.sleep(0.5)
            if isinstance(e, (serial.SerialException, OSError)):
                # Port went away (unplugged / glitch): close and reopen
                try:
                    ser.close()
                except Exception:
                    pass
                ser = open_serial()
                read_buf = bytearray()
                car_active = False
                last_state_is_car = False
                car_since_ms = None
                with state_lock:
                    state["car_present"] = False


def handle_detection(distance_mm: int, log_to_db: bool):
    global test_count_today

    if log_to_db:
        try:
            increment_count_and_save(distance_mm)
        except Exception as e:
            print("DB insert error:", e)
    else:
        # Test mode: keep a local counter only
        try:
            with state_lock:
                test_count_today += 1
        except Exception as e:
            print("Test counter error:", e)

    # Always publish MQTT (even in test mode)
    try:
        publish_detection(distance_mm)
    except Exception as e:
        print("MQTT publish error:", e)

# ----------------- HOURLY WEBHOOK -----------------
DEFAULT_WEBHOOK_START_HOUR = 16   # used when no usable schedule entry exists
WEBHOOK_TICK_SECONDS = 30
WEBHOOK_ATTEMPTS = 3
WEBHOOK_RETRY_DELAY = 5


def build_hourly_payload(now_local: datetime, include_current: bool = False) -> dict:
    """
    Build the hourly attendance table.
    Keys run from today's StartShow hour through 23. Hours that have completed
    carry their real count; the current hour (unless include_current) and
    later hours are 0.
    """
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT CAST(strftime('%H', ts_utc,'localtime') AS INTEGER) AS h, COUNT(*) "
            "FROM detections "
            "WHERE date(ts_utc,'localtime') = date('now','localtime') "
            "GROUP BY h;"
        )
        per_hour = {int(h): int(c) for h, c in cur.fetchall()}

        cur.execute("SELECT value FROM metadata WHERE key='total_count';")
        row = cur.fetchone()
        overall = int(row[0]) if row else 0

    with state_lock:
        tm = test_mode
        today_test = test_count_today
        data = schedule_data

    current = today_test if tm else sum(per_hour.values())

    try:
        entry = data[(now_local.weekday() + 1) % 7]
        start_hour = int(str(entry.get("StartShow", "")).split(":")[0])
    except Exception:
        start_hour = DEFAULT_WEBHOOK_START_HOUR

    threshold = now_local.hour + (1 if include_current else 0)

    payload = {}
    for h in range(start_hour, 24):
        payload[str(h)] = per_hour.get(h, 0) if h < threshold else 0

    payload["OverallAttendance"] = overall
    payload["CurrentAttendance"] = current
    payload["Message"] = ""
    return payload


def get_webhook_url() -> str:
    with cfg_lock:
        return str((cfg.get("webhook") or {}).get("url", "") or "").strip()


def send_webhook(payload: dict, url: str, attempts: int = WEBHOOK_ATTEMPTS):
    """POST payload as JSON. Returns (ok, detail)."""
    detail = "no attempt made"
    for n in range(1, attempts + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.ok:
                dlog(f"Webhook POST ok ({resp.status_code})")
                return True, resp.status_code
            detail = f"HTTP {resp.status_code}"
        except Exception as e:
            detail = str(e)
        print(f"Webhook attempt {n}/{attempts} failed: {detail}")
        if n < attempts:
            time.sleep(WEBHOOK_RETRY_DELAY)
    return False, detail


def webhook_schedule_ok(at: datetime) -> bool:
    valid, active, enabled = compute_schedule_flags(at)
    with state_lock:
        mo = manual_override
    return bool(mo or (valid and enabled and active))


def post_hourly_webhook(now_local: datetime, include_current: bool = False):
    url = get_webhook_url()
    if not url:
        return
    send_webhook(build_hourly_payload(now_local, include_current), url)


def webhook_loop():
    """
    Poll the clock every few seconds (robust to NTP clock jumps on a Pi
    without an RTC) and:
      - post when the hour rolls over, if the schedule was active at the
        top of that hour (completed hours only);
      - post a final table (including the current partial hour) when the
        show ends part-way through an hour.
    """
    now = datetime.now()
    last_key = (now.date(), now.hour)
    was_active = webhook_schedule_ok(now)

    while True:
        time.sleep(WEBHOOK_TICK_SECONDS)
        try:
            now = datetime.now()
            key = (now.date(), now.hour)
            active = webhook_schedule_ok(now)

            if key != last_key:
                moved_forward = key > last_key
                last_key = key
                boundary = now.replace(minute=0, second=0, microsecond=0)
                # Judge the schedule at the top of the hour, not a few seconds after
                if moved_forward and webhook_schedule_ok(boundary):
                    post_hourly_webhook(now)
            elif was_active and not active and now.minute != 0:
                # Show ended mid-hour: send a final table with the partial hour
                post_hourly_webhook(now, include_current=True)

            was_active = active
        except Exception as e:
            print("Webhook loop error:", e)


# ----------------- SCHEDULE EVAL LOOP -----------------
def schedule_eval_loop():
    """Recompute schedule_active & schedule_valid every few seconds."""
    global schedule_valid, schedule_active

    while True:
        try:
            now_local = datetime.now()  # Pi is assumed to keep local timezone
            valid, active, enabled = compute_schedule_flags(now_local)

            with state_lock:
                schedule_valid = valid
                if manual_override:
                    schedule_active = True
                else:
                    schedule_active = valid and enabled and active
        except Exception as e:
            print("Schedule eval error:", e)

        time.sleep(5)  # recompute every few seconds so show start/stop is prompt

# ----------------- Flask app -----------------
app = Flask(__name__, template_folder="templates")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/schedule")
def schedule_page():
    return render_template("schedule.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    with state_lock:
        last_distance = state.get("last_distance_mm")
        last_strength = state.get("last_strength")
        present = state.get("car_present")
        last_valid = state.get("last_valid_reading", False)
        sched_active = schedule_active
        sched_valid = schedule_valid
        last_fetch = schedule_last_fetch_utc
        last_status = schedule_last_status
        tm = test_mode
        mo = manual_override
    return jsonify(
        {
            "last_distance_mm": last_distance,
            "last_strength": last_strength,
            "last_valid_reading": last_valid,
            "car_present": bool(present),
            "schedule_active": bool(sched_active),
            "schedule_valid": bool(sched_valid),
            "schedule_last_fetch_utc": last_fetch,
            "schedule_last_status": last_status,
            "test_mode": tm,
            "manual_override": mo,
        }
    )

# ------------- CONFIG API -------------
SECRET_MASK = "********"
SECRET_FIELDS = {"mqtt": ("password",), "schedule": ("github_token",)}


def _public_cfg():
    """Config copy with secrets masked (the config page never needs the real values)."""
    out = copy.deepcopy(cfg)
    for section, fields in SECRET_FIELDS.items():
        sec = out.get(section)
        if isinstance(sec, dict):
            for f in fields:
                if sec.get(f):
                    sec[f] = SECRET_MASK
    return out


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global cfg, SCHEDULE_URL, GITHUB_TOKEN, SCHEDULE_REFRESH_SECONDS
    global DEBOUNCE_MS, IGNORE_ZERO_DISTANCE, MIN_STRENGTH, test_mode

    if request.method == "GET":
        with cfg_lock:
            return jsonify(_public_cfg())

    data = request.get_json(silent=True) or {}

    with cfg_lock:
        new_cfg = copy.deepcopy(cfg)

        for k in ["serial_port", "baudrate", "database"]:
            if k in data:
                new_cfg[k] = data[k]

        for section in ("mqtt", "http", "detection", "webhook", "schedule"):
            incoming = data.get(section)
            if not isinstance(incoming, dict):
                continue
            incoming = dict(incoming)
            # A masked secret means "unchanged"
            for f in SECRET_FIELDS.get(section, ()):
                if incoming.get(f) == SECRET_MASK:
                    incoming.pop(f)
            if not isinstance(new_cfg.get(section), dict):
                new_cfg[section] = {}
            new_cfg[section].update(incoming)

        # Validate everything BEFORE touching the file or runtime state
        try:
            det = new_cfg.get("detection", {})
            new_debounce = int(det.get("debounce_ms", DEBOUNCE_MS))
            new_ignore = bool(det.get("ignore_zero_distance", IGNORE_ZERO_DISTANCE))
            new_min_strength = int(det.get("min_strength", MIN_STRENGTH))
            sch = new_cfg.get("schedule", {})
            new_interval = int(sch.get("check_interval_sec", SCHEDULE_REFRESH_SECONDS))
            int(new_cfg.get("baudrate", BAUD))
            int(new_cfg.get("http", {}).get("port", HTTP_PORT))
            int(new_cfg.get("mqtt", {}).get("port", MQTT_PORT))
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": f"Invalid value: {e}"}), 400

        try:
            write_json_atomic(CFG_PATH, new_cfg)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not save: {e}"}), 500

        cfg = new_cfg

        # Apply selected runtime fields
        DEBOUNCE_MS = new_debounce
        IGNORE_ZERO_DISTANCE = new_ignore
        MIN_STRENGTH = new_min_strength
        if "test_mode" in det:
            with state_lock:
                test_mode = bool(det["test_mode"])

        SCHEDULE_URL = sch.get("url", SCHEDULE_URL)
        GITHUB_TOKEN = sch.get("github_token", GITHUB_TOKEN)
        SCHEDULE_REFRESH_SECONDS = new_interval

        return jsonify({"ok": True, "config": _public_cfg()})


@app.route("/api/webhook/test", methods=["POST"])
def api_webhook_test():
    """Send the hourly table to the saved webhook URL right now (ignores the schedule)."""
    url = get_webhook_url()
    if not url:
        return jsonify({"ok": False, "detail": "No webhook URL configured"}), 400
    payload = build_hourly_payload(datetime.now(), include_current=True)
    ok, detail = send_webhook(payload, url, attempts=1)
    return jsonify({"ok": ok, "detail": detail, "payload": payload}), (200 if ok else 502)

# ------------- MODE API -------------
@app.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    global test_mode, test_count_today, manual_override

    if request.method == "GET":
        with state_lock:
            return jsonify(
                {"test_mode": bool(test_mode), "manual_override": bool(manual_override)}
            )

    data = request.json or {}
    with state_lock:
        if "test_mode" in data:
            new_tm = bool(data["test_mode"])
            if new_tm and not test_mode:
                test_count_today = 0
            test_mode = new_tm
        if "manual_override" in data:
            manual_override = bool(data["manual_override"])

        tm = test_mode
        mo = manual_override

    # Also update cfg.detection.test_mode for persistence
    with cfg_lock:
        if not isinstance(cfg.get("detection"), dict):
            cfg["detection"] = {}
        cfg["detection"]["test_mode"] = tm
        try:
            write_json_atomic(CFG_PATH, cfg)
        except Exception as e:
            print("Could not persist test_mode:", e)

    return jsonify({"ok": True, "test_mode": tm, "manual_override": mo})

# ------------- SCHEDULE APIs -------------
@app.route("/api/schedule/refresh", methods=["POST"])
def api_schedule_refresh():
    ok = refresh_schedule()
    with state_lock:
        last_fetch = schedule_last_fetch_utc
        last_status = schedule_last_status
    return jsonify(
        {
            "ok": ok,
            "schedule_last_fetch_utc": last_fetch,
            "schedule_last_status": last_status,
        }
    )


@app.route("/api/schedule/local", methods=["GET", "POST"])
def api_schedule_local():
    global schedule_data, schedule_last_fetch_utc, schedule_last_status

    if request.method == "GET":
        # Prefer in-memory schedule if present
        with state_lock:
            data = schedule_data

        if data is None:
            # Try to load from local file
            if not load_local_schedule():
                # Return a default blank schedule
                default = []
                for _ in range(7):
                    default.append(
                        {
                            "Enable": False,
                            "StartShow": "16:30",
                            "ShowStop": "22:00"
                        }
                    )
                return jsonify(default)

            with state_lock:
                data = schedule_data

        return jsonify(data)

    # POST: Save new schedule locally and apply immediately
    new_sched = request.json
    if not isinstance(new_sched, list) or len(new_sched) != 7:
        return jsonify({"ok": False, "error": "Schedule must be a list of 7 entries"}), 400

    # Basic validation
    for i, entry in enumerate(new_sched):
        if not isinstance(entry, dict):
            return jsonify({"ok": False, "error": f"Entry {i} must be an object"}), 400
        if "Enable" not in entry or "StartShow" not in entry or "ShowStop" not in entry:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Entry {i} must include Enable, StartShow, ShowStop",
                }
            ), 400

    # Save to disk
    try:
        write_json_atomic(SCHED_LOCAL_PATH, new_sched)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Update in-memory
    with state_lock:
        globals()["schedule_data"] = new_sched
        globals()["schedule_last_fetch_utc"] = datetime.now(
            timezone.utc
        ).isoformat()
        globals()["schedule_last_status"] = "Updated via local editor"

    return jsonify({"ok": True})

# ------------- STATS APIs -------------
@app.route("/api/stats", methods=["GET"])
def api_stats():
    # DB-based stats
    with db_lock:
        cur = db_conn.cursor()
        # today (local time)
        cur.execute(
            "SELECT COUNT(*) FROM detections WHERE "
            "date(ts_utc,'localtime') = date('now','localtime');"
        )
        today_db = cur.fetchone()[0]

        # yesterday
        cur.execute(
            "SELECT COUNT(*) FROM detections WHERE "
            "date(ts_utc,'localtime') = date('now','localtime','-1 day');"
        )
        yesterday = cur.fetchone()[0]

        # last 7 days
        cur.execute(
            "SELECT COUNT(*) FROM detections WHERE "
            "datetime(ts_utc) >= datetime('now','-7 days');"
        )
        week = cur.fetchone()[0]

        # all time (metadata)
        cur.execute("SELECT value FROM metadata WHERE key='total_count';")
        row = cur.fetchone()
        total_all_time = int(row[0]) if row else 0

    with state_lock:
        tm = test_mode
        today_test = test_count_today

    current = today_test if tm else today_db

    return jsonify(
        {
            "current_count": current,
            "yesterday_count": yesterday,
            "week_count": week,
            "total_all_time": total_all_time,
            "test_mode": tm,
        }
    )


@app.route("/api/stats/hourly", methods=["GET"])
def api_stats_hourly():
    """
    Returns hourly counts for today, filtered to only hours where
    the schedule is active (Enable + StartShow/ShowStop).
    """
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT strftime('%H', ts_utc,'localtime') AS h, "
            "COUNT(*) FROM detections "
            "WHERE date(ts_utc,'localtime') = date('now','localtime') "
            "GROUP BY h ORDER BY h;"
        )
        rows = cur.fetchall()

    now_local = datetime.now()
    hours = []
    counts = []

    for h_str, count in rows:
        try:
            h_int = int(h_str)
        except (TypeError, ValueError):
            continue

        dt_candidate = now_local.replace(hour=h_int, minute=0, second=0, microsecond=0)
        valid, active, enabled = compute_schedule_flags(dt_candidate)
        if not (valid and enabled and active):
            continue

        hours.append(h_str)
        counts.append(count)

    return jsonify({"hours": hours, "counts": counts})

# ------------- OLD COMPAT / MAINTENANCE -------------
@app.route("/reset_count", methods=["POST"])
def reset_count():
    """
    Kept for backward compatibility; resets DB total_count and test counter.
    """
    reset_total_count()
    with state_lock:
        globals()["test_count_today"] = 0
    return jsonify({"total_count": 0})


@app.route("/wipe_all", methods=["POST"])
def wipe_all():
    wipe_database()
    with state_lock:
        globals()["test_count_today"] = 0
    return jsonify({"ok": True})


@app.route("/api/total", methods=["GET"])
def api_total():
    return jsonify({"total_count": get_total_count()})


@app.route("/api/recent", methods=["GET"])
def api_recent():
    limit = int(request.args.get("limit", 20))
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT id, ts_utc, distance_mm "
            "FROM detections ORDER BY id DESC LIMIT ?;",
            (limit,),
        )
        rows = cur.fetchall()
    out = [{"id": r[0], "ts_utc": r[1], "distance_mm": r[2]} for r in rows]
    return jsonify(out)

@app.route("/api/service/restart", methods=["POST"])
def restart_service():
    try:
        subprocess.Popen(
            [
                "/usr/bin/sudo",
                "-n",
                "/bin/systemctl",
                "restart",
                "--no-block",
                "ShowMonLidarCounter"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
    
# ----------------- MAIN -----------------
if __name__ == "__main__":
    # Load the last-known local schedule first so counting works even if
    # the remote schedule is unreachable at boot.
    load_local_schedule()

    # sensor thread
    t = threading.Thread(target=sensor_loop, daemon=True)
    t.start()

    # schedule refresh thread (pull from GitHub)
    s = threading.Thread(target=schedule_loop, daemon=True)
    s.start()

    # real-time schedule evaluator (local clock)
    e = threading.Thread(target=schedule_eval_loop, daemon=True)
    e.start()

    # hourly webhook poster
    w = threading.Thread(target=webhook_loop, daemon=True)
    w.start()

    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)
