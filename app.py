"""
V380 Camera Recorder -- desktop app.

Double-click this (or the desktop shortcut) to launch. No command line needed.

What it does:
  - Scans your local network for cameras (RTSP port 554) and tries known
    credentials + common URL paths to find working streams.
  - Lists what it finds with resolution/codec.
  - Lets you Start/Stop local recording (10-min segments, auto-delete after
    RETENTION_DAYS) for any camera in the list, independently.
  - If you save a YouTube stream key (via the button in the app), newly
    started recordings also back up to an unlisted YouTube livestream.
"""

import ctypes
import ipaddress
import json
import os
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import wintypes
from datetime import datetime, timedelta
from tkinter import ttk, messagebox, simpledialog, filedialog

# Suppress the console window Windows otherwise flashes for every console
# program (ffmpeg, ffprobe, powershell) launched from this GUI app.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
YOUTUBE_KEY_PATH = os.path.join(BASE_DIR, "youtube_key.txt")
YOUTUBE_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"
SEGMENT_SECONDS = 600
RETENTION_DAYS = 3
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
PYTHONW_PATH = r"C:\Python314\pythonw.exe"
APP_PATH = os.path.abspath(__file__)
STARTUP_SHORTCUT_NAME = "V380-Camera-Recorder-AutoStart.lnk"

VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]


def find_vlc():
    for path in VLC_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None

ALERTS_DIR = os.path.join(BASE_DIR, "alerts")
TELEGRAM_TOKEN_PATH = os.path.join(BASE_DIR, "telegram_token.txt")
MOBILENET_PROTOTXT = os.path.join(BASE_DIR, "MobileNetSSD_deploy.prototxt")
MOBILENET_MODEL = os.path.join(BASE_DIR, "MobileNetSSD_deploy.caffemodel")
DETECTION_CLASSES = [
    "background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus",
    "car", "cat", "chair", "cow", "diningtable", "dog", "horse", "motorbike",
    "person", "pottedplant", "sheep", "sofa", "train", "tvmonitor",
]
PERSON_CLASS_ID = DETECTION_CLASSES.index("person")
DETECTION_CONFIDENCE = 0.5
DETECTION_SAMPLE_INTERVAL = 1.0  # seconds between frames analyzed
ALERT_COOLDOWN_SECONDS = 30

_DNN_NET = None


def read_text_file(path):
    if not os.path.isfile(path):
        return None
    v = open(path, "r", encoding="utf-8").read().strip()
    return v or None


def get_dnn_net(log_fn=None):
    """Lazily load the shared person-detection model. Returns net or None."""
    global _DNN_NET
    if _DNN_NET is not None:
        return _DNN_NET
    if not (os.path.isfile(MOBILENET_PROTOTXT) and os.path.isfile(MOBILENET_MODEL)):
        if log_fn:
            log_fn("Detection model files not found -- person detection unavailable.")
        return None
    try:
        import cv2
        _DNN_NET = cv2.dnn.readNetFromCaffe(MOBILENET_PROTOTXT, MOBILENET_MODEL)
        return _DNN_NET
    except Exception as e:
        if log_fn:
            log_fn(f"Failed to load detection model: {e}")
        return None


def send_telegram_photo(photo_path, caption, log_fn):
    token = read_text_file(TELEGRAM_TOKEN_PATH)
    settings = load_settings()
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(photo_path, "rb") as f:
            r = requests.post(url, data={"chat_id": chat_id, "caption": caption},
                               files={"photo": f}, timeout=15)
        if r.status_code == 200:
            return True
        log_fn(f"Telegram alert failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        log_fn(f"Telegram alert error: {e}")
        return False


def send_ntfy_photo(photo_path, caption, log_fn):
    settings = load_settings()
    topic = settings.get("ntfy_topic")
    if not topic:
        return False
    try:
        import requests
        url = f"https://ntfy.sh/{topic}"
        with open(photo_path, "rb") as f:
            r = requests.put(url, data=f.read(), headers={
                "Title": "Person Detected",
                "Filename": os.path.basename(photo_path),
                "Message": caption,
            }, timeout=15)
        if r.status_code == 200:
            return True
        log_fn(f"ntfy alert failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        log_fn(f"ntfy alert error: {e}")
        return False


def send_alert(photo_path, caption, log_fn):
    ok_telegram = send_telegram_photo(photo_path, caption, log_fn)
    ok_ntfy = send_ntfy_photo(photo_path, caption, log_fn)
    if not ok_telegram and not ok_ntfy:
        log_fn("Alert not sent: no working alert channel configured "
                "(set a Telegram chat ID or ntfy topic).")
    return ok_telegram or ok_ntfy


class Detector:
    """Runs person detection on a camera's own RTSP connection (independent
    of recording) and fires a cooldown-limited photo alert on detection."""

    def __init__(self, ip, rtsp_url, log_fn):
        self.ip = ip
        self.rtsp_url = rtsp_url
        self.log = log_fn
        self._stop = threading.Event()
        self._thread = None
        self._last_alert = 0.0

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _detect_person(self, net, frame):
        import cv2
        blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)), 0.007843,
                                      (300, 300), 127.5)
        net.setInput(blob)
        detections = net.forward()
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            class_id = int(detections[0, 0, i, 1])
            if confidence > DETECTION_CONFIDENCE and class_id == PERSON_CLASS_ID:
                return True
        return False

    def _loop(self):
        import cv2
        net = get_dnn_net(self.log)
        if net is None:
            return
        os.makedirs(ALERTS_DIR, exist_ok=True)
        backoff = 5
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                cap.release()
                self.log(f"[{self.ip}] detection: could not open stream, "
                          f"retrying in {backoff}s")
                for _ in range(backoff * 2):
                    if self._stop.is_set():
                        break
                    time.sleep(0.5)
                backoff = min(backoff * 2, 60)
                continue

            self.log(f"[{self.ip}] detection started")
            backoff = 5
            last_sample = 0.0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    self.log(f"[{self.ip}] detection: stream read failed, reconnecting")
                    break
                now = time.time()
                if now - last_sample < DETECTION_SAMPLE_INTERVAL:
                    continue
                last_sample = now
                try:
                    found = self._detect_person(net, frame)
                except Exception as e:
                    self.log(f"[{self.ip}] detection error: {e}")
                    continue
                if found and (now - self._last_alert) >= ALERT_COOLDOWN_SECONDS:
                    self._last_alert = now
                    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    safe_ip = self.ip.replace(".", "_")
                    photo_path = os.path.join(ALERTS_DIR, f"alert_{safe_ip}_{ts}.jpg")
                    cv2.imwrite(photo_path, frame)
                    caption = (f"Person detected at {self.ip} — "
                                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    self.log(f"[{self.ip}] person detected, sending alert")
                    threading.Thread(target=send_alert,
                                      args=(photo_path, caption, self.log),
                                      daemon=True).start()
            cap.release()
        self.log(f"[{self.ip}] detection stopped")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f)


def set_startup_shortcut(enabled):
    """Create/remove a Startup-folder shortcut so the app auto-launches at logon."""
    ps = f'''
$startup = [Environment]::GetFolderPath('Startup')
$path = "$startup\\{STARTUP_SHORTCUT_NAME}"
if ($env:ACTION -eq "create") {{
    $WshShell = New-Object -ComObject WScript.Shell
    $s = $WshShell.CreateShortcut($path)
    $s.TargetPath = "{PYTHONW_PATH}"
    $s.Arguments = '"{APP_PATH}"'
    $s.WorkingDirectory = "{BASE_DIR}"
    $s.Save()
}} else {{
    Remove-Item $path -Force -ErrorAction SilentlyContinue
}}
'''
    env = os.environ.copy()
    env["ACTION"] = "create" if enabled else "remove"
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                    env=env, capture_output=True, timeout=15, creationflags=NO_WINDOW)

# --- Windows Job Object: ensures ffmpeg children are killed the instant this
# app's process dies, even on a crash or forced kill (Task Manager, power loss),
# with no cleanup code needing to run. Kernel-enforced, not best-effort.
_JOB_HANDLE = None


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


def _get_or_create_job(log_fn=None):
    """Lazily create the kill-on-close job object. Returns handle or None on failure."""
    global _JOB_HANDLE
    if _JOB_HANDLE is not None:
        return _JOB_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW returned NULL")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise OSError("SetInformationJobObject failed")
        _JOB_HANDLE = job
        return job
    except Exception as e:
        if log_fn:
            log_fn(f"Warning: could not set up crash-safe process cleanup ({e}). "
                    f"ffmpeg processes may survive an abrupt app crash.")
        return None


def assign_to_job(proc, log_fn=None):
    """Put a Popen'd process under the kill-on-close job, best-effort."""
    job = _get_or_create_job(log_fn)
    if job is None:
        return
    try:
        ctypes.windll.kernel32.AssignProcessToJobObject(job, int(proc._handle))
    except Exception as e:
        if log_fn:
            log_fn(f"Warning: could not attach process {proc.pid} to crash-safe cleanup ({e}).")


DEFAULT_CREDS = [("admin", "admin")]
PATH_TEMPLATES = [
    "/live/ch00_0", "/live/ch01_0", "/live/ch0", "/live/ch00_1",
    "/onvif1", "/onvif2", "/11", "/12", "/h264", "/stream0", "/stream1",
    "/cam/realmonitor?channel=1&subtype=0",
]


def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(ip + "/24", strict=False)


def port_open(ip, port=554, timeout=0.4):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((str(ip), port)) == 0


def ffprobe_stream_info(url, timeout=6):
    """Return dict(codec, width, height) if url yields video, else None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-select_streams", "v", "-show_entries",
             "stream=codec_name,width,height", "-of", "json", url],
            capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        return {"codec": s.get("codec_name"), "width": s.get("width"),
                "height": s.get("height")}
    except Exception:
        return None


def get_known_creds():
    """Default admin/admin, plus any custom credentials the user has saved
    (via the app's "Set Camera Credentials..." button) tried first."""
    settings = load_settings()
    user = settings.get("camera_username")
    pw = settings.get("camera_password")
    if user and pw:
        return [(user, pw)] + DEFAULT_CREDS
    return DEFAULT_CREDS


def probe_ip(ip):
    """Try known creds/paths against one IP. Return working (url, info) or None."""
    for user, pw in get_known_creds():
        for path in PATH_TEMPLATES:
            url = f"rtsp://{user}:{pw}@{ip}:554{path}"
            info = ffprobe_stream_info(url)
            if info:
                return url, info
    return None


class Recorder:
    """Manages one camera's ffmpeg recording process + auto-restart + cleanup."""

    def __init__(self, ip, rtsp_url, log_fn, recordings_dir):
        self.ip = ip
        self.rtsp_url = rtsp_url
        self.log = log_fn
        self.recordings_dir = recordings_dir
        self._stop = threading.Event()
        self._thread = None
        self._proc = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._thread:
            self._thread.join(timeout=10)

    def _read_youtube_key(self):
        if not os.path.isfile(YOUTUBE_KEY_PATH):
            return None
        key = open(YOUTUBE_KEY_PATH, "r", encoding="utf-8").read().strip()
        return key or None

    def _build_cmd(self):
        os.makedirs(self.recordings_dir, exist_ok=True)
        safe_ip = self.ip.replace(".", "_")
        pattern = os.path.join(self.recordings_dir, f"cam_{safe_ip}_%Y-%m-%d_%H-%M-%S.mp4")
        base = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-i", self.rtsp_url,
            "-c", "copy", "-map", "0:v", "-map", "0:a?",
        ]
        key = self._read_youtube_key()
        if key:
            local_leg = (f"[f=segment:segment_time={SEGMENT_SECONDS}:"
                         f"reset_timestamps=1:strftime=1]{pattern}")
            yt_leg = f"[f=flv]{YOUTUBE_RTMP_BASE}/{key}"
            return base + ["-f", "tee", f"{local_leg}|{yt_leg}"], bool(key)
        return base + ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                        "-reset_timestamps", "1", "-strftime", "1", pattern], False

    def _cleanup_old(self):
        if not os.path.isdir(self.recordings_dir):
            return
        cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
        safe_ip = self.ip.replace(".", "_")
        for name in os.listdir(self.recordings_dir):
            if not name.startswith(f"cam_{safe_ip}_") or not name.endswith(".mp4"):
                continue
            path = os.path.join(self.recordings_dir, name)
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    os.remove(path)
                    self.log(f"[{self.ip}] deleted expired segment {name}")
            except OSError:
                pass

    def _loop(self):
        backoff = 5
        while not self._stop.is_set():
            self._cleanup_old()
            cmd, with_youtube = self._build_cmd()
            self.log(f"[{self.ip}] starting recording"
                      + (" (+ YouTube backup)" if with_youtube else ""))
            start = time.time()
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            assign_to_job(self._proc, self.log)
            while self._proc.poll() is None and not self._stop.is_set():
                time.sleep(0.5)
            if self._stop.is_set():
                if self._proc.poll() is None:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                break
            ran_for = time.time() - start
            backoff = 5 if ran_for > 60 else min(backoff * 2, 300)
            self.log(f"[{self.ip}] ffmpeg stopped after {ran_for:.0f}s, "
                      f"restarting in {backoff}s")
            for _ in range(backoff * 2):
                if self._stop.is_set():
                    break
                time.sleep(0.5)
        self.log(f"[{self.ip}] recording stopped")


class PreviewWindow(tk.Toplevel):
    """A small window showing a live-updating preview of one camera's RTSP
    stream, using its own independent connection (separate from recording
    and detection)."""

    MAX_WIDTH = 640

    def __init__(self, parent, ip, rtsp_url, log_fn):
        super().__init__(parent)
        self.ip = ip
        self.rtsp_url = rtsp_url
        self.log = log_fn
        self.title(f"Live Preview — {ip}")
        self.resizable(False, False)

        self.status_label = ttk.Label(self, text="Connecting...")
        self.status_label.pack(padx=4, pady=(4, 0))
        self.video_label = ttk.Label(self)
        self.video_label.pack(padx=4, pady=4)

        self._stop = threading.Event()
        self._photo = None  # keep a reference so Tkinter doesn't garbage-collect it
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _set_status(self, text):
        try:
            self.status_label.config(text=text)
        except tk.TclError:
            pass  # window already destroyed

    def _set_frame(self, photo):
        try:
            self._photo = photo
            self.video_label.config(image=photo)
        except tk.TclError:
            pass

    def _loop(self):
        import cv2
        from PIL import Image, ImageTk

        backoff = 5
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                cap.release()
                self.after(0, lambda: self._set_status(
                    f"Could not connect, retrying in {backoff}s..."))
                for _ in range(backoff * 2):
                    if self._stop.is_set():
                        break
                    time.sleep(0.5)
                backoff = min(backoff * 2, 30)
                continue

            backoff = 5
            self.after(0, lambda: self._set_status(f"Live — {self.ip}"))
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                h, w = frame.shape[:2]
                if w > self.MAX_WIDTH:
                    scale = self.MAX_WIDTH / w
                    frame = cv2.resize(frame, (self.MAX_WIDTH, int(h * scale)))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                photo = ImageTk.PhotoImage(image=img)
                self.after(0, lambda p=photo: self._set_frame(p))
                time.sleep(0.05)  # cap at ~20fps refresh, no need to redraw faster
            cap.release()
            if not self._stop.is_set():
                self.after(0, lambda: self._set_status("Connection lost, reconnecting..."))

    def _on_close(self):
        self._stop.set()
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("V380 Camera Recorder")
        self.geometry("780x640")
        self.minsize(700, 560)

        self.recorders = {}   # ip -> Recorder
        self.detectors = {}   # ip -> Detector
        self.preview_windows = {}  # ip -> PreviewWindow
        self.cam_info = {}    # ip -> (url, info)
        self.log_queue = queue.Queue()

        settings = load_settings()
        saved_dir = settings.get("recordings_dir")
        self.recordings_dir = saved_dir if saved_dir else RECORDINGS_DIR
        self._auto_start_record = bool(settings.get("auto_start_record", False))

        self._build_ui()
        self.auto_start_var.set(self._auto_start_record)
        self.after(100, self._drain_log_queue)
        self.after(300, self._refresh_status)
        self.after(500, self.scan_network)

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Rescan Network", command=self.scan_network).pack(side="left")
        self.scan_status = ttk.Label(top, text="Scanning...")
        self.scan_status.pack(side="left", padx=10)
        ttk.Button(top, text="Open Recordings Folder",
                   command=self.open_folder).pack(side="right")
        ttk.Button(top, text="Set YouTube Key...",
                   command=self.set_youtube_key).pack(side="right", padx=6)
        ttk.Button(top, text="Set Camera Credentials...",
                   command=self.set_camera_credentials).pack(side="right", padx=6)

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=False)

        columns = ("ip", "resolution", "codec", "status", "detection")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", height=6)
        for col, label, w in [("ip", "Camera IP", 130), ("resolution", "Resolution", 100),
                               ("codec", "Codec", 70), ("status", "Recording", 100),
                               ("detection", "Person Alerts", 140)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="x", expand=False)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill="x")
        ttk.Button(btns, text="Start Recording", command=self.start_selected).pack(side="left")
        ttk.Button(btns, text="Stop Recording", command=self.stop_selected).pack(side="left", padx=6)
        self.yt_status = ttk.Label(btns, text="YouTube backup: not configured")
        self.yt_status.pack(side="right")

        detect_btns = ttk.Frame(self, padding=(10, 0))
        detect_btns.pack(fill="x")
        ttk.Button(detect_btns, text="Start Person Alerts",
                   command=self.start_detection_selected).pack(side="left")
        ttk.Button(detect_btns, text="Stop Person Alerts",
                   command=self.stop_detection_selected).pack(side="left", padx=6)
        ttk.Button(detect_btns, text="Set Telegram Chat ID...",
                   command=self.set_telegram_chat_id).pack(side="right", padx=(6, 0))
        ttk.Button(detect_btns, text="Set ntfy Topic...",
                   command=self.set_ntfy_topic).pack(side="right")

        alert_status_row = ttk.Frame(self, padding=(10, 4))
        alert_status_row.pack(fill="x")
        self.alert_status = ttk.Label(alert_status_row, text="Alerts: not configured")
        self.alert_status.pack(side="left")

        preview_btns = ttk.Frame(self, padding=10)
        preview_btns.pack(fill="x")
        ttk.Button(preview_btns, text="Live Preview (In-App)",
                   command=self.open_preview_selected).pack(side="left")
        ttk.Button(preview_btns, text="Open in VLC",
                   command=self.open_vlc_selected).pack(side="left", padx=6)
        ttk.Button(preview_btns, text="Review Day's Footage...",
                   command=self.review_day_selected).pack(side="left", padx=6)

        folder_row = ttk.Frame(self, padding=(10, 0))
        folder_row.pack(fill="x")
        ttk.Button(folder_row, text="Choose Recordings Folder...",
                   command=self.choose_recordings_folder).pack(side="left")
        self.folder_label = ttk.Label(folder_row, text=self.recordings_dir)
        self.folder_label.pack(side="left", padx=10)

        auto_row = ttk.Frame(self, padding=10)
        auto_row.pack(fill="x")
        self.auto_start_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            auto_row, text="Auto-start with Windows and begin recording automatically",
            variable=self.auto_start_var, command=self.on_toggle_autostart
        ).pack(side="left")

        logframe = ttk.LabelFrame(self, text="Activity Log", padding=6)
        logframe.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(logframe, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

        self._refresh_youtube_status()
        self._refresh_alert_status()

    def log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.log_queue.put(line)
        try:
            with open(os.path.join(BASE_DIR, "app.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _drain_log_queue(self):
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(200, self._drain_log_queue)

    def scan_network(self):
        self.after(0, lambda: self.scan_status.config(text="Scanning network..."))
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            net = local_subnet()
        except OSError:
            self.log("Could not determine local network (no internet route).")
            self.after(0, lambda: self.scan_status.config(text="Scan failed"))
            return
        hosts = list(net.hosts())
        self.log(f"Scanning {len(hosts)} addresses on {net} for cameras...")
        candidates = []
        with ThreadPoolExecutor(max_workers=100) as ex:
            futs = {ex.submit(port_open, h): h for h in hosts}
            for f in as_completed(futs):
                if f.result():
                    candidates.append(str(futs[f]))

        found = 0
        for ip in candidates:
            result = probe_ip(ip)
            if result:
                url, info = result
                self.cam_info[ip] = (url, info)
                self.log(f"Camera found at {ip}: {info['width']}x{info['height']} {info['codec']}")
                found += 1
                if ip not in self.recorders:
                    self.recorders[ip] = Recorder(ip, url, self.log, self.recordings_dir)
                if ip not in self.detectors:
                    self.detectors[ip] = Detector(ip, url, self.log)
                self.after(0, lambda ip=ip, info=info: self._upsert_row(ip, info))

        final_text = (f"Scan complete — {found} camera(s) found" if found
                       else "Scan complete — no cameras found")
        self.after(0, lambda: self.scan_status.config(text=final_text))
        if found and self.auto_start_var.get():
            self.after(0, self._auto_start_all)

    def _auto_start_all(self):
        for ip, rec in self.recorders.items():
            if not rec.running:
                rec.start()
                self.log(f"Auto-start: recording began for {ip}")

    def _upsert_row(self, ip, info):
        res = f"{info['width']}x{info['height']}"
        for row in self.tree.get_children():
            if self.tree.set(row, "ip") == ip:
                self.tree.set(row, "resolution", res)
                self.tree.set(row, "codec", info["codec"])
                return
        self.tree.insert("", "end", values=(ip, res, info["codec"], "Idle", "Off"))

    def _selected_ip(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No camera selected", "Select a camera in the list first.")
            return None
        return self.tree.set(sel[0], "ip")

    def start_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        rec = self.recorders.get(ip)
        if not rec:
            messagebox.showerror("Error", "No stream URL known for this camera yet.")
            return
        rec.start()
        self.log(f"Start requested for {ip}")

    def stop_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        rec = self.recorders.get(ip)
        if rec:
            threading.Thread(target=rec.stop, daemon=True).start()
            self.log(f"Stop requested for {ip}")

    def start_detection_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        if get_dnn_net() is None:
            messagebox.showerror(
                "Detection model missing",
                "The person-detection model files aren't available yet. "
                "Wait for the download to finish, then try again.")
            return
        det = self.detectors.get(ip)
        if not det:
            messagebox.showerror("Error", "No stream URL known for this camera yet.")
            return
        det.start()
        self.log(f"Person alerts started for {ip}")

    def stop_detection_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        det = self.detectors.get(ip)
        if det:
            threading.Thread(target=det.stop, daemon=True).start()
            self.log(f"Person alerts stop requested for {ip}")

    def open_preview_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        info = self.cam_info.get(ip)
        if not info:
            messagebox.showerror("Error", "No stream URL known for this camera yet.")
            return
        existing = self.preview_windows.get(ip)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        url, _ = info
        win = PreviewWindow(self, ip, url, self.log)
        self.preview_windows[ip] = win
        self.log(f"Opened live preview for {ip}")

    def open_vlc_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        info = self.cam_info.get(ip)
        if not info:
            messagebox.showerror("Error", "No stream URL known for this camera yet.")
            return
        vlc_path = find_vlc()
        if not vlc_path:
            messagebox.showerror(
                "VLC not found",
                "Couldn't find VLC at the usual install location. "
                "Install VLC, or open this URL manually in any player:\n\n"
                f"{info[0]}")
            return
        url, _ = info
        subprocess.Popen([vlc_path, url])
        self.log(f"Opened {ip} in VLC")

    def _refresh_status(self):
        for row in self.tree.get_children():
            ip = self.tree.set(row, "ip")
            rec = self.recorders.get(ip)
            det = self.detectors.get(ip)
            status = "Recording" if rec and rec.running else "Idle"
            det_status = "On" if det and det.running else "Off"
            self.tree.set(row, "status", status)
            self.tree.set(row, "detection", det_status)
        self.after(1000, self._refresh_status)

    def open_folder(self):
        os.makedirs(self.recordings_dir, exist_ok=True)
        os.startfile(self.recordings_dir)

    def review_day_selected(self):
        ip = self._selected_ip()
        if not ip:
            return
        safe_ip = ip.replace(".", "_")
        prefix = f"cam_{safe_ip}_"
        dates = set()
        if os.path.isdir(self.recordings_dir):
            for name in os.listdir(self.recordings_dir):
                if name.startswith(prefix) and name.endswith(".mp4"):
                    # filename: cam_<ip>_YYYY-MM-DD_HH-MM-SS.mp4
                    rest = name[len(prefix):]
                    date_part = rest[:10]
                    dates.add(date_part)
        if not dates:
            messagebox.showinfo("No footage found",
                                 f"No recorded segments found for {ip} yet.")
            return
        picked = self._ask_pick_date(sorted(dates, reverse=True))
        if not picked:
            return
        threading.Thread(target=self._build_and_open_review,
                          args=(ip, safe_ip, picked), daemon=True).start()

    def _ask_pick_date(self, dates):
        dialog = tk.Toplevel(self)
        dialog.title("Pick a date to review")
        dialog.resizable(False, False)
        ttk.Label(dialog, text="Available days with footage:").pack(padx=10, pady=(10, 4))
        listbox = tk.Listbox(dialog, height=min(10, len(dates)), width=20)
        for d in dates:
            listbox.insert("end", d)
        listbox.selection_set(0)
        listbox.pack(padx=10, pady=4)
        result = {"value": None}

        def confirm():
            sel = listbox.curselection()
            if sel:
                result["value"] = listbox.get(sel[0])
            dialog.destroy()

        ttk.Button(dialog, text="Review This Day", command=confirm).pack(pady=(4, 10))
        dialog.transient(self)
        dialog.grab_set()
        self.wait_window(dialog)
        return result["value"]

    def _build_and_open_review(self, ip, safe_ip, date_str):
        prefix = f"cam_{safe_ip}_{date_str}_"
        segments = sorted(
            name for name in os.listdir(self.recordings_dir)
            if name.startswith(prefix) and name.endswith(".mp4")
        )
        if not segments:
            self.log(f"No segments found for {ip} on {date_str}.")
            return

        self.log(f"Building full-day review for {ip} on {date_str} "
                  f"({len(segments)} segments)...")
        reviews_dir = os.path.join(self.recordings_dir, "reviews")
        os.makedirs(reviews_dir, exist_ok=True)
        output_path = os.path.join(reviews_dir, f"review_{safe_ip}_{date_str}.mp4")
        list_path = os.path.join(reviews_dir, f".concat_{safe_ip}_{date_str}.txt")

        with open(list_path, "w", encoding="utf-8") as f:
            for name in segments:
                full_path = os.path.join(self.recordings_dir, name)
                f.write(f"file '{full_path}'\n")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
               "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
        try:
            os.remove(list_path)
        except OSError:
            pass

        if result.returncode != 0 or not os.path.isfile(output_path):
            self.log(f"Failed to build review for {ip} on {date_str}: "
                      f"{result.stderr[-300:]}")
            self.after(0, lambda: messagebox.showerror(
                "Review build failed",
                f"Could not stitch together footage for {date_str}. "
                f"Check the activity log for details."))
            return

        self.log(f"Review ready: {output_path}")
        vlc_path = find_vlc()
        if vlc_path:
            subprocess.Popen([vlc_path, output_path])
            self.log("Opened review in VLC. Use +/- keys or Playback > Speed "
                      "to fast-forward through the day.")
        else:
            os.startfile(output_path)
            self.log("Opened review in your default video player.")

    def choose_recordings_folder(self):
        chosen = filedialog.askdirectory(
            initialdir=self.recordings_dir, title="Choose folder to save recordings in")
        if not chosen:
            return
        self.recordings_dir = chosen
        self.folder_label.config(text=self.recordings_dir)
        for rec in self.recorders.values():
            rec.recordings_dir = self.recordings_dir
        settings = load_settings()
        settings["recordings_dir"] = self.recordings_dir
        save_settings(settings)
        self.log(f"Recordings folder set to: {self.recordings_dir} "
                  f"(applies to newly started/restarted recordings)")

    def on_toggle_autostart(self):
        enabled = self.auto_start_var.get()
        settings = load_settings()
        settings["auto_start_record"] = enabled
        save_settings(settings)
        threading.Thread(target=set_startup_shortcut, args=(enabled,), daemon=True).start()
        if enabled:
            self.log("Auto-start enabled: app will launch at Windows login and "
                      "start recording on all detected cameras automatically.")
            if self.recorders:
                self._auto_start_all()
        else:
            self.log("Auto-start disabled: app will no longer launch automatically at login.")

    def _refresh_youtube_status(self):
        configured = os.path.isfile(YOUTUBE_KEY_PATH) and \
            open(YOUTUBE_KEY_PATH, encoding="utf-8").read().strip()
        self.yt_status.config(
            text="YouTube backup: configured" if configured
            else "YouTube backup: not configured")

    def set_youtube_key(self):
        key = simpledialog.askstring(
            "YouTube Stream Key",
            "Paste your YouTube Live stream key (from YouTube Studio > Go Live):",
            show="*")
        if key and key.strip():
            with open(YOUTUBE_KEY_PATH, "w", encoding="utf-8") as f:
                f.write(key.strip())
            self.log("YouTube stream key saved. New recordings will back up to YouTube.")
            self._refresh_youtube_status()

    def set_camera_credentials(self):
        settings = load_settings()
        username = simpledialog.askstring(
            "Camera Username", "Camera login username (usually 'admin'):",
            initialvalue=settings.get("camera_username", "admin"))
        if not username:
            return
        password = simpledialog.askstring(
            "Camera Password", f"Camera login password for user '{username}':",
            show="*")
        if not password:
            return
        settings["camera_username"] = username.strip()
        settings["camera_password"] = password
        save_settings(settings)
        self.log("Camera credentials saved. They'll be tried first on the next scan.")

    def _refresh_alert_status(self):
        settings = load_settings()
        parts = []
        if os.path.isfile(TELEGRAM_TOKEN_PATH) and settings.get("telegram_chat_id"):
            parts.append("Telegram")
        if settings.get("ntfy_topic"):
            parts.append("ntfy")
        text = f"Alerts: {' + '.join(parts)} configured" if parts else "Alerts: not configured"
        self.alert_status.config(text=text)

    def set_telegram_chat_id(self):
        if not os.path.isfile(TELEGRAM_TOKEN_PATH):
            messagebox.showinfo(
                "Telegram bot token missing",
                "No telegram_token.txt found. Message @BotFather on Telegram to "
                "create a bot first, then save its token to that file.")
            return
        chat_id = simpledialog.askstring(
            "Telegram Chat ID",
            "Paste your Telegram chat ID (message your bot once, then check\n"
            "https://api.telegram.org/bot<TOKEN>/getUpdates to find it):")
        if chat_id and chat_id.strip():
            settings = load_settings()
            settings["telegram_chat_id"] = chat_id.strip()
            save_settings(settings)
            self.log("Telegram chat ID saved. Person alerts will be sent via Telegram.")
            self._refresh_alert_status()

    def set_ntfy_topic(self):
        topic = simpledialog.askstring(
            "ntfy.sh Topic",
            "Pick a unique topic name (like a password -- anyone who knows it\n"
            "can see your alerts), e.g. 'v380-adnan-alerts-8823':")
        if topic and topic.strip():
            settings = load_settings()
            settings["ntfy_topic"] = topic.strip()
            save_settings(settings)
            self.log(f"ntfy topic saved. Subscribe to it in the ntfy app/website "
                      f"(topic: {topic.strip()}) to receive alerts.")
            self._refresh_alert_status()

    def on_close(self):
        for rec in self.recorders.values():
            if rec.running:
                rec.stop()
        for det in self.detectors.values():
            if det.running:
                det.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
