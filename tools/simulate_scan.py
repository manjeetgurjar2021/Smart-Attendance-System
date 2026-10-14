"""Simulate an ESP32-CAM scan so you can test the server without hardware.

  python tools/simulate_scan.py --uid 04A1B2C3
  python tools/simulate_scan.py --uid 04A1B2C3 --image face.jpg --url http://localhost:5000
"""
import argparse
import json
import urllib.error
import urllib.request

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://localhost:5000")
p.add_argument("--key", default="change-me", help="must match API_KEY on the server")
p.add_argument("--uid", required=True)
p.add_argument("--image", help="optional JPEG to upload with the scan")
p.add_argument("--epoch", help="optional unix time (simulates an offline-queued scan)")
a = p.parse_args()

body = open(a.image, "rb").read() if a.image else b""
headers = {"X-API-Key": a.key, "X-Card-UID": a.uid, "Content-Type": "image/jpeg"}
if a.epoch:
    headers["X-Scan-Epoch"] = a.epoch
req = urllib.request.Request(a.url.rstrip("/") + "/api/scan", data=body, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.status, json.loads(r.read()))
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
