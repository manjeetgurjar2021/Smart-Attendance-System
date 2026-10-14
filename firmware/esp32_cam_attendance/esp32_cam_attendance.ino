/*
 * Smart Attendance System - ESP32-CAM firmware
 *
 * Tap an RFID card (MFRC522) -> capture a JPEG -> POST to the server over Wi-Fi.
 * If the network is down, scans are queued in RAM (with NTP timestamps) and
 * synced automatically when Wi-Fi returns (photo is skipped for queued scans).
 *
 * Board : "AI Thinker ESP32-CAM"  (Arduino IDE, esp32 board package)
 * Library: "MFRC522" by GithubCommunity / miguelbalboa (Library Manager)
 *
 * RC522 wiring (ESP32-CAM GPIO):
 *   SDA/SS -> 15    SCK -> 14    MOSI -> 13    MISO -> 12
 *   RST    -> 3V3   3.3V -> 3V3  GND -> GND
 *   (Power the RC522 from 3.3 V only. Use a 5 V / 2 A supply for the ESP32-CAM.)
 *
 * LED feedback (onboard red LED, GPIO33):
 *   1 long blink  = attendance marked
 *   2 short blink = already marked today
 *   3 fast blink  = error / unknown card / no network (queued)
 */
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>
#include <time.h>
#include "esp_camera.h"
#include "config.h"

// ---------------- RC522 pins ----------------
#define RC522_SS    15
#define RC522_SCK   14
#define RC522_MOSI  13
#define RC522_MISO  12

// ---------------- Onboard LED (active LOW) ----------------
#define LED_PIN     33

// ---------------- AI-Thinker camera pins ----------------
#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22

// ---------------- Settings ----------------
const uint32_t DEBOUNCE_MS     = 3000;    // ignore the same card for 3 s
const uint32_t WIFI_TIMEOUT_MS = 15000;
const uint32_t HTTP_TIMEOUT_MS = 8000;
const time_t   MIN_VALID_EPOCH = 1700000000;   // NTP considered synced after this
const uint8_t  QUEUE_SIZE      = 20;

MFRC522 rfid(RC522_SS, MFRC522::UNUSED_PIN);

struct Pending { char uid[24]; time_t ts; };
Pending queueBuf[QUEUE_SIZE];
uint8_t queueCount = 0;

String   lastUid;
uint32_t lastScanMs = 0;
uint32_t lastFlushMs = 0;
bool     cameraOk = false;

// ====================================================== LED helpers
void ledBlink(uint8_t times, uint16_t onMs, uint16_t offMs) {
  for (uint8_t i = 0; i < times; i++) {
    digitalWrite(LED_PIN, LOW);  delay(onMs);
    digitalWrite(LED_PIN, HIGH); delay(offMs);
  }
}

void feedback(int httpCode) {
  if (httpCode == 201)      ledBlink(1, 700, 100);   // marked
  else if (httpCode == 200) ledBlink(2, 150, 150);   // already marked
  else                      ledBlink(3, 80, 80);     // error / unknown / offline
}

// ====================================================== Camera
bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sscb_sda = SIOD_GPIO_NUM; c.pin_sscb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;   c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = psramFound() ? FRAMESIZE_VGA : FRAMESIZE_QVGA;   // 640x480 / 320x240
  c.jpeg_quality = 12;                                              // 10 best ... 63 worst
  c.fb_count     = 1;
  c.grab_mode    = CAMERA_GRAB_LATEST;
  return esp_camera_init(&c) == ESP_OK;
}

camera_fb_t* capturePhoto() {
  if (!cameraOk) return nullptr;
  camera_fb_t* stale = esp_camera_fb_get();   // throw away a possibly stale frame
  if (stale) esp_camera_fb_return(stale);
  return esp_camera_fb_get();
}

// ====================================================== Network
bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) delay(250);
  return WiFi.status() == WL_CONNECTED;
}

// POST one scan. Returns HTTP status code (<=0 means network failure).
int postScan(const String& uid, time_t ts, camera_fb_t* fb) {
  WiFiClient plain;
  WiFiClientSecure secure;
  HTTPClient http;

  if (String(SERVER_URL).startsWith("https")) {
    secure.setInsecure();   // skips certificate check; pin a root CA for production
    http.begin(secure, SERVER_URL);
  } else {
    http.begin(plain, SERVER_URL);
  }
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-API-Key", API_KEY);
  http.addHeader("X-Card-UID", uid);
  if (ts > MIN_VALID_EPOCH) http.addHeader("X-Scan-Epoch", String((unsigned long)ts));
  http.addHeader("Content-Type", fb ? "image/jpeg" : "application/octet-stream");

  int code = fb ? http.POST(fb->buf, fb->len) : http.POST("");
  if (code > 0) Serial.printf("  server: %d %s\n", code, http.getString().c_str());
  else          Serial.printf("  http error: %s\n", HTTPClient::errorToString(code).c_str());
  http.end();
  return code;
}

void enqueue(const String& uid, time_t ts) {
  if (queueCount >= QUEUE_SIZE) {                      // drop oldest
    memmove(&queueBuf[0], &queueBuf[1], sizeof(Pending) * (QUEUE_SIZE - 1));
    queueCount--;
  }
  uid.toCharArray(queueBuf[queueCount].uid, sizeof(queueBuf[0].uid));
  queueBuf[queueCount].ts = ts;
  queueCount++;
  Serial.printf("  queued offline (%u pending)\n", queueCount);
}

void flushQueue() {
  if (queueCount == 0 || millis() - lastFlushMs < 2000) return;
  lastFlushMs = millis();
  if (WiFi.status() != WL_CONNECTED) return;
  int code = postScan(String(queueBuf[0].uid), queueBuf[0].ts, nullptr);
  if (code > 0) {                                      // server answered: drop it from queue
    memmove(&queueBuf[0], &queueBuf[1], sizeof(Pending) * (queueCount - 1));
    queueCount--;
  }
}

// ====================================================== RFID
String uidToString(const MFRC522::Uid& u) {
  String s;
  for (byte i = 0; i < u.size; i++) {
    if (u.uidByte[i] < 0x10) s += "0";
    s += String(u.uidByte[i], HEX);
  }
  s.toUpperCase();
  return s;
}

// ====================================================== Arduino
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);

  cameraOk = initCamera();
  Serial.println(cameraOk ? "Camera OK" : "Camera FAILED (scans will be sent without photos)");

  SPI.begin(RC522_SCK, RC522_MISO, RC522_MOSI, RC522_SS);
  rfid.PCD_Init();
  Serial.print("RC522 firmware: ");
  rfid.PCD_DumpVersionToSerial();

  WiFi.mode(WIFI_STA);
  Serial.println(ensureWiFi() ? "Wi-Fi connected: " + WiFi.localIP().toString() : "Wi-Fi not connected yet");
  configTime(0, 0, "pool.ntp.org", "time.google.com");   // UTC epoch; server converts to local time

  ledBlink(2, 150, 150);
  Serial.println("Ready - tap a card.");
}

void loop() {
  ensureWiFi();
  flushQueue();

  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) { delay(40); return; }

  String uid = uidToString(rfid.uid);
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  if (uid == lastUid && millis() - lastScanMs < DEBOUNCE_MS) return;
  lastUid = uid;
  lastScanMs = millis();
  Serial.println("Card: " + uid);

  time_t now = time(nullptr);
  if (WiFi.status() != WL_CONNECTED) {
    enqueue(uid, now);
    feedback(0);
    return;
  }

  camera_fb_t* fb = capturePhoto();
  int code = postScan(uid, now, fb);
  if (fb) esp_camera_fb_return(fb);

  if (code <= 0) enqueue(uid, now);                   // network/server unreachable
  feedback(code);
}
