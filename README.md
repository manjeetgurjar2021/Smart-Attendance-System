# Smart Attendance System (ESP32-CAM + RFID)

RFID-based attendance with photo capture. A student taps a card on an RC522 reader, the ESP32-CAM
captures a photo and sends both to a Flask server over Wi-Fi. The server logs attendance in SQLite
and shows it on a live web dashboard.

```
RFID card -> RC522 -> ESP32-CAM --(HTTP/HTTPS, JPEG + card UID)--> Flask server -> SQLite
                          |                                            |
                   offline queue (NTP time)                    Live dashboard + CSV export
```

## Features
- RFID check-in with a photo captured at the moment of the tap
- One attendance record per student per day (duplicate taps are ignored)
- Offline queue: scans made without Wi-Fi are stored with NTP timestamps and synced later
- Live dashboard (3 s refresh): present/absent counts, attendance log with photos, 7-day chart
- Register students from the dashboard; unknown cards are listed so you can register them in one click
- CSV export, API-key protected device endpoint, optional dashboard login

## Hardware
| Part | Notes |
|---|---|
| ESP32-CAM (AI-Thinker) + FTDI/USB programmer | Use a stable 5 V / 2 A supply |
| MFRC522 RFID reader + cards/tags | 3.3 V only |

### Wiring (RC522 -> ESP32-CAM)
| RC522 | ESP32-CAM |
|---|---|
| SDA (SS) | GPIO 15 |
| SCK | GPIO 14 |
| MOSI | GPIO 13 |
| MISO | GPIO 12 |
| RST | 3V3 |
| 3.3V / GND | 3V3 / GND |

GPIO 12 is a boot-strapping pin: if the board fails to boot or flash, disconnect the RC522 MISO wire
while uploading, then reconnect it.

## 1. Run the server
```bash
cd server
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

export API_KEY="pick-a-long-random-key"               # Windows: set API_KEY=...
export ADMIN_USER="admin" ADMIN_PASS="a-strong-password"   # optional dashboard login
python app.py
```
Open http://localhost:5000. Environment variables: `API_KEY`, `ADMIN_USER`, `ADMIN_PASS`,
`TIMEZONE` (default `Asia/Kolkata`), `PORT`, `DB_PATH`, `CAPTURE_DIR`.

### Test without hardware
```bash
python tools/simulate_scan.py --uid 04A1B2C3 --key pick-a-long-random-key   # unknown card (404)
# register the card on the dashboard, then:
python tools/simulate_scan.py --uid 04A1B2C3 --key pick-a-long-random-key   # marked (201)
python tools/simulate_scan.py --uid 04A1B2C3 --key pick-a-long-random-key   # duplicate (200)
```
Automated tests: `pip install pytest && python -m pytest tests -q`

## 2. Flash the ESP32-CAM
1. Arduino IDE -> install the **esp32** board package, select **AI Thinker ESP32-CAM**.
2. Library Manager -> install **MFRC522**.
3. In `firmware/esp32_cam_attendance/`, copy `config.h.example` to `config.h` and set Wi-Fi, `SERVER_URL`
   (your PC's LAN IP, e.g. `http://192.168.1.10:5000/api/scan`) and `API_KEY`.
4. Upload (hold GPIO0 to GND while resetting for flash mode), remove the jumper, reset, open Serial Monitor at 115200.
5. Tap a card: its UID is printed. Register that UID on the dashboard, then tap again.

LED (onboard red): 1 long blink = marked, 2 short = already marked today, 3 fast = error/unknown/queued offline.

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/scan` | Device endpoint. Headers `X-API-Key`, `X-Card-UID`, optional `X-Scan-Epoch`; body = JPEG. Returns 201 marked, 200 duplicate, 404 unknown |
| GET | `/api/attendance?date=YYYY-MM-DD` | Present + absent lists for a day |
| GET | `/api/history?days=7` | Daily present counts |
| GET/POST | `/api/students` | List / register students |
| DELETE | `/api/students/<uid>` | Remove a student (past records kept) |
| GET | `/api/unknown` | Recently seen unregistered cards |
| GET | `/export.csv?date=...` or `?all=1` | CSV export |

## Deploying to the cloud
Run `gunicorn app:app` from `server/` on a host such as Render or a VPS, set `API_KEY`, `ADMIN_USER`,
`ADMIN_PASS`, and use an `https://` `SERVER_URL` in `config.h`. SQLite and photos live on local disk,
so attach a persistent disk (or move to PostgreSQL + object storage). The firmware uses
`setInsecure()` for HTTPS to keep setup simple; pin your server's root CA for production.

## Limitations / future work
- RFID cards can be lent to a friend; the photo is a deterrent and audit trail, not identity verification.
- No face recognition yet (photos are stored for review only).
- Queued offline scans are held in RAM (lost on power loss) and sent without photos.
- Possible extensions: MQTT instead of HTTP, face matching, per-class schedules, email/SMS alerts.

## Project structure
```
firmware/esp32_cam_attendance/   Arduino sketch + config.h.example
server/                          Flask app, dashboard template, requirements
tools/simulate_scan.py           Fake device for testing
tests/test_api.py                Server tests
```
