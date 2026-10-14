import os
import sys
import tempfile
import time

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "test.db")
os.environ["CAPTURE_DIR"] = os.path.join(tmp, "captures")
os.environ["API_KEY"] = "testkey"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import app as server  # noqa: E402

client = server.app.test_client()
H = {"X-API-Key": "testkey", "X-Card-UID": "04A1B2C3"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"


def test_health():
    assert client.get("/api/health").status_code == 200


def test_rejects_bad_key():
    r = client.post("/api/scan", headers={**H, "X-API-Key": "nope"})
    assert r.status_code == 401


def test_rejects_bad_uid():
    r = client.post("/api/scan", headers={**H, "X-Card-UID": "zz"})
    assert r.status_code == 400


def test_unknown_card_then_register_and_mark():
    r = client.post("/api/scan", headers=H)
    assert r.status_code == 404
    assert client.get("/api/unknown").get_json()["unknown"][0]["uid"] == "04A1B2C3"

    r = client.post("/api/students", json={"uid": "04:a1:b2:c3", "name": "Manjeet", "roll_no": "01"})
    assert r.status_code == 201
    assert client.get("/api/unknown").get_json()["unknown"] == []

    r = client.post("/api/scan", headers=H, data=JPEG)
    assert r.status_code == 201 and r.get_json()["status"] == "marked"

    r = client.post("/api/scan", headers=H, data=JPEG)       # same day again
    assert r.status_code == 200 and r.get_json()["status"] == "duplicate"

    d = client.get("/api/attendance").get_json()
    assert len(d["present"]) == 1 and d["present"][0]["photo"].endswith(".jpg")
    assert d["absent"] == [] and d["total"] == 1


def test_duplicate_registration_rejected():
    r = client.post("/api/students", json={"uid": "04A1B2C3", "name": "Again"})
    assert r.status_code == 409


def test_offline_queued_scan_uses_device_time():
    client.post("/api/students", json={"uid": "AABBCCDD", "name": "Queued"})
    yesterday = int(time.time()) - 86400
    r = client.post("/api/scan", headers={**H, "X-Card-UID": "AABBCCDD", "X-Scan-Epoch": str(yesterday)})
    assert r.status_code == 201
    h = client.get("/api/history?days=3").get_json()["history"]
    assert sum(x["present"] for x in h) >= 2


def test_csv_export_and_history():
    r = client.get("/export.csv?all=1")
    assert r.status_code == 200 and b"Manjeet" in r.data
    assert len(client.get("/api/history?days=7").get_json()["history"]) == 7


def test_dashboard_renders():
    assert client.get("/").status_code == 200
