// Afficheur de limites Claude pour LUMEN – Waveshare ESP32-S3-Touch-LCD-1.54 (ST7789, 240x240).
// Le Raspberry Pi de LUMEN (claude_usage.py) pousse les valeurs ; la carte ne detient AUCUN jeton Claude.
// Brochage tire du code officiel Waveshare (exemple Arduino 04_gfx_helloworld de la carte tactile) :
//   DC=45 CS=21 SCK=38 MOSI=39 RST=40 BL=46
// Reglages Arduino IDE (doc Waveshare) : « ESP32S3 Dev Module », Flash 16 Mo, paquet esp32 3.2.0,
// bibliotheque GFX_Library_for_Arduino 1.6.0.
// NON TESTE sur le materiel : verifie le brochage si l'ecran reste noir.
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Arduino_GFX_Library.h>
#include "secrets.h"

#define GFX_BL 46
Arduino_DataBus *bus = new Arduino_ESP32SPI(45 /*DC*/, 21 /*CS*/, 38 /*SCK*/, 39 /*MOSI*/, -1 /*MISO*/);
Arduino_GFX *gfx = new Arduino_ST7789(bus, 40 /*RST*/, 0 /*rotation*/, true /*IPS*/, 240, 240);

WebServer server(80);

struct Lim { int pct = -1; long resetAtMs = -1; };   // pct < 0 : inconnu
Lim five, seven;
unsigned long lastUpdate = 0;
bool haveData = false, dirty = true;
const unsigned long STALE_MS = 15UL * 60UL * 1000UL;

const uint16_t BG = 0x0000, FG = 0xFFFF, DIM = 0x8410;

uint16_t tint(int p) { return p >= 90 ? RED : p >= 70 ? ORANGE : GREEN; }

void formatReset(Lim &l, char *out, size_t n) {
  if (l.resetAtMs < 0) { snprintf(out, n, "reset : ?"); return; }
  long s = (l.resetAtMs - (long)millis()) / 1000;
  if (s < 0) s = 0;
  long d = s / 86400, h = (s % 86400) / 3600, m = (s % 3600) / 60;
  if (d > 0) snprintf(out, n, "reset dans %ldj %ldh", d, h);
  else snprintf(out, n, "reset dans %ldh%02ld", h, m);
}

void drawBlock(int y, const char *title, Lim &l) {
  char buf[32];
  gfx->setTextSize(2);
  gfx->setTextColor(FG, BG);
  gfx->setCursor(12, y);
  gfx->print(title);
  gfx->setTextSize(3);
  snprintf(buf, sizeof buf, l.pct >= 0 ? "%3d%%" : "  --", l.pct);
  gfx->setCursor(150, y - 4);
  gfx->print(buf);
  const int bx = 12, bw = 216, bh = 18, by = y + 30;
  gfx->fillRoundRect(bx, by, bw, bh, 6, 0x2104);
  if (l.pct >= 0) {
    int w = (bw * min(l.pct, 100)) / 100;
    if (w > 0) gfx->fillRoundRect(bx, by, max(w, 8), bh, 6, tint(l.pct));
  }
  gfx->setTextSize(2);
  gfx->setTextColor(DIM, BG);
  formatReset(l, buf, sizeof buf);
  gfx->fillRect(12, by + 26, 216, 16, BG);
  gfx->setCursor(12, by + 26);
  gfx->print(buf);
}

void drawAll() {
  if (dirty) { gfx->fillScreen(BG); dirty = false; }
  drawBlock(14, "Session 5h", five);
  drawBlock(120, "Semaine", seven);
  bool stale = haveData && (millis() - lastUpdate > STALE_MS);
  gfx->setTextSize(1);
  gfx->setTextColor(stale ? ORANGE : DIM, BG);
  gfx->fillRect(0, 228, 240, 12, BG);
  gfx->setCursor(12, 228);
  if (!haveData) gfx->print("en attente de LUMEN...");
  else if (stale) gfx->print("donnees perimees - LUMEN eteint ?");
}

void handleUpdate() {
  // POST x-www-form-urlencoded : key, five, seven (pourcentage, -1 si inconnu), r5, r7 (secondes avant reset, -1 si inconnu)
  if (server.arg("key") != DEVICE_KEY) { server.send(403, "text/plain", "cle refusee"); return; }
  auto upd = [](Lim &l, const String &p, const String &r) {
    l.pct = p.toInt();
    long rs = r.toInt();
    l.resetAtMs = rs >= 0 ? (long)millis() + rs * 1000L : -1;
  };
  upd(five, server.arg("five"), server.arg("r5"));
  upd(seven, server.arg("seven"), server.arg("r7"));
  lastUpdate = millis(); haveData = true;
  server.send(200, "text/plain", "ok");
}

void setup() {
  Serial.begin(115200);
  pinMode(GFX_BL, OUTPUT);
  digitalWrite(GFX_BL, HIGH);          // actif a l'etat haut, d'apres le code officiel
  if (!gfx->begin()) Serial.println("gfx->begin() a echoue");
  gfx->fillScreen(BG);
  gfx->setTextColor(FG, BG);
  gfx->setTextSize(2);
  gfx->setCursor(12, 100);
  gfx->print("Connexion Wi-Fi...");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(250);

  dirty = true;
  if (WiFi.status() == WL_CONNECTED) {
    MDNS.begin("claudeusage");          // le Pi joint http://claudeusage.local/update
    server.on("/update", HTTP_POST, handleUpdate);
    server.begin();
    Serial.println(WiFi.localIP());     // utile si le nom .local ne se resout pas : mets l'IP dans claude.json
  } else {
    gfx->fillScreen(BG);
    gfx->setCursor(12, 100);
    gfx->setTextColor(RED, BG);
    gfx->print("Wi-Fi KO");
    delay(3000);
    dirty = true;
  }
}

unsigned long lastDraw = 0;
void loop() {
  server.handleClient();
  if (millis() - lastDraw > 1000) { lastDraw = millis(); drawAll(); }
}
