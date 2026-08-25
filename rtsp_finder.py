#!/usr/bin/env python3
"""
V380 / generic IP-camera RTSP finder.
Run this on the SAME WiFi as your camera (at home).

What it does:
  1. Figures out your local network (e.g. 192.168.1.x).
  2. Scans it for devices with camera ports open (554 RTSP, 8899/80 ONVIF).
  3. For each candidate, tries every common V380 RTSP URL and reports which stream(s) work.

Usage:
  python rtsp_finder.py                       # auto-scan whole network
  python rtsp_finder.py --ip 192.168.1.50     # test one known camera IP
  python rtsp_finder.py --user admin --password YOURPASS

Needs: Python 3, and ffmpeg (for ffprobe). Install ffmpeg:
  Windows: download from https://ffmpeg.org  (or:  winget install Gyan.FFmpeg)
  Mac:     brew install ffmpeg
  Linux:   sudo apt install ffmpeg
"""

import argparse, ipaddress, socket, subprocess, sys, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

# Common RTSP paths used by V380 / V380 Pro and generic ONVIF cams.
# {u}/{p}/{ip} get filled in. Dual-lens cams often expose ch00 and ch01.
PATH_TEMPLATES = [
    "rtsp://{ip}:554/live/ch0",
    "rtsp://{ip}:554/live/ch00_0",
    "rtsp://{ip}:554/live/ch01_0",
    "rtsp://{ip}:554/live/ch00_1",
    "rtsp://{u}:{p}@{ip}:554/live/ch0",
    "rtsp://{u}:{p}@{ip}:554/live/ch00_0",
    "rtsp://{u}:{p}@{ip}:554/live/ch01_0",
    "rtsp://{u}:{p}@{ip}:554/onvif1",
    "rtsp://{u}:{p}@{ip}:554/onvif2",
    "rtsp://{u}:{p}@{ip}:554/11",
    "rtsp://{u}:{p}@{ip}:554/12",
    "rtsp://{u}:{p}@{ip}:554/h264",
    "rtsp://{u}:{p}@{ip}:554/stream1",
    "rtsp://{u}:{p}@{ip}:554/stream0",
    "rtsp://{u}:{p}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
    "rtsp://{ip}:8554/live/ch0",
]
CAMERA_PORTS = [554, 8899, 80]


def local_subnet():
    """Best-effort guess of the local /24 this machine is on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip, ipaddress.ip_network(ip + "/24", strict=False)


def port_open(ip, port, timeout=0.4):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((str(ip), port)) == 0


def find_candidates(net):
    hosts = list(net.hosts())
    print(f"Scanning {len(hosts)} addresses on {net} for camera ports...")
    found = []
    with ThreadPoolExecutor(max_workers=100) as ex:
        futs = {ex.submit(port_open, h, 554): h for h in hosts}
        for f in as_completed(futs):
            h = futs[f]
            if f.result():
                extra = [p for p in (8899, 80) if port_open(h, p)]
                tag = " (+ONVIF/HTTP)" if extra else ""
                print(f"  candidate: {h}{tag}")
                found.append(str(h))
    return found


def probe(url, timeout=6):
    """Return True if ffprobe can read a video stream from url."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-select_streams", "v", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0 and r.stdout.strip() != ""
    except subprocess.TimeoutExpired:
        return False


def test_ip(ip, user, pw):
    print(f"\nTrying RTSP URLs on {ip} ...")
    working = []
    for t in PATH_TEMPLATES:
        url = t.format(ip=ip, u=user, p=pw)
        ok = probe(url)
        shown = url.replace(f"{user}:{pw}@", "***:***@")
        print(f"  [{'OK ' if ok else 'no '}] {shown}")
        if ok:
            working.append(url)
    return working


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", help="Test only this camera IP")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin")
    args = ap.parse_args()

    if not shutil.which("ffprobe"):
        print("ERROR: ffprobe (part of ffmpeg) not found. Install ffmpeg first — see top of this file.")
        sys.exit(1)

    targets = [args.ip] if args.ip else None
    if not targets:
        my_ip, net = local_subnet()
        print(f"This machine: {my_ip}")
        targets = find_candidates(net)
        if not targets:
            print("\nNo devices with RTSP port 554 open. Either the camera is off this")
            print("network, or it simply doesn't serve RTSP (common for V380). ")
            print("Tip: check your router's device list for the camera's IP and re-run")
            print("with:  python rtsp_finder.py --ip <that_ip> --password <yourpass>")
            return

    all_working = []
    for ip in targets:
        all_working += test_ip(ip, args.user, args.password)

    print("\n" + "=" * 60)
    if all_working:
        print("SUCCESS — your camera streams RTSP. Working URL(s):")
        for u in all_working:
            print("   " + u)
        print("\nPaste one into VLC (Media > Open Network Stream) to confirm the video.")
        print("This URL is what every recording / AI / alert project will use.")
    else:
        print("No working RTSP URL found.")
        print("If a device WAS found on 554 but no path worked, the password may be")
        print("wrong (re-run with --password) or the firmware blocks RTSP.")
        print("If nothing was found at all, this V380 likely has no RTSP — in that")
        print("case a ~$30 ONVIF camera (Reolink/Dahua/Hikvision) is the clean fix.")


if __name__ == "__main__":
    main()
