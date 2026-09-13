#!/usr/bin/env python3
"""
LUMEN — serveur + moteur (Raspberry Pi)

Le Pi genere maintenant les animations lui-meme, en continu, et les envoie
a WLED en DDP. Le telephone n'est plus qu'une telecommande : il peut se
fermer, le panneau continue de tourner.

API :
  GET  /api/state          -> etat courant (anim, palette, params, bri, on, fps)
  POST /api/state          -> modifie l'etat  { anim, palette, params, bri, on, ip, audio_react }
  POST /api/audio          -> niveau sonore courant { level: 0..1 } — haute frequence, pas de sauvegarde disque
  POST /api/image          -> envoie une photo (64x64x3 octets RGB bruts), bascule sur anim=photo
  GET  /wled/info?ip=...   -> proxy (test de connexion)
"""

import http.server
import json
import os
import re
import socketserver
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import numpy as np

import engine

PORT = 8000
HOST_RE = re.compile(r'^[0-9a-zA-Z\.\-\_]{1,64}(:\d{1,5})?$')
CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lumen-state.json")


UPDATE_PAGE = """<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>LUMEN — Mise a jour</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{font-family:'Inter',-apple-system,sans-serif;background:#000;
    color:#fff;min-height:100vh;display:flex;flex-direction:column;
    align-items:center;justify-content:center;padding:24px;}
  h1{font-size:30px;font-weight:800;text-transform:uppercase;letter-spacing:-1px;margin-bottom:8px}
  p{color:rgba(255,255,255,.55);font-size:13px;text-align:center;margin-bottom:28px;line-height:1.6;
    text-transform:uppercase;letter-spacing:.5px;font-weight:600}
  .drop{width:100%;max-width:360px;border:1.5px solid rgba(255,255,255,.3);
    padding:36px 20px;text-align:center;background:transparent;cursor:pointer;transition:.15s}
  .drop:active{background:rgba(255,255,255,.08)}
  .drop b{display:block;font-size:15px;margin-bottom:6px;font-weight:700}
  .drop span{font-size:11px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:1px}
  input[type=file]{display:none}
  .btn{margin-top:20px;width:100%;max-width:360px;padding:16px;border:none;
    font-size:14px;font-weight:800;text-transform:uppercase;letter-spacing:1px;
    color:#000;cursor:pointer;background:#fff;opacity:.35;pointer-events:none;transition:.15s}
  .btn.ready{opacity:1;pointer-events:auto}
  .status{margin-top:20px;font-size:13px;text-align:center;min-height:20px;
    text-transform:uppercase;letter-spacing:.5px;font-weight:600}
  .ok{color:#39d353}.err{color:#ff6060}
  a{color:rgba(255,255,255,.5);font-size:11px;margin-top:28px;text-decoration:none;
    text-transform:uppercase;letter-spacing:1px;font-weight:700}
</style></head><body>
  <h1>LUMEN</h1>
  <p>Mise a jour de l'application.<br>Selectionne le fichier <b>lumen-pi-package.zip</b>.</p>
  <label class="drop" id="drop">
    <b id="dropTitle">Choisir le fichier .zip</b>
    <span id="dropSub">appuie ici</span>
    <input type="file" id="file" accept=".zip,application/zip">
  </label>
  <button class="btn" id="send">Installer la mise a jour</button>
  <div class="status" id="status"></div>
  <a href="/">&larr; Retour a LUMEN</a>
<script>
  const file=document.getElementById('file'), drop=document.getElementById('drop'),
        send=document.getElementById('send'), status=document.getElementById('status'),
        dropTitle=document.getElementById('dropTitle'), dropSub=document.getElementById('dropSub');
  let chosen=null;
  file.addEventListener('change',()=>{
    if(file.files.length){ chosen=file.files[0];
      dropTitle.textContent=chosen.name;
      dropSub.textContent=(chosen.size/1024).toFixed(0)+' Ko';
      send.classList.add('ready'); status.textContent=''; }
  });
  send.addEventListener('click',async()=>{
    if(!chosen) return;
    send.classList.remove('ready'); status.className='status'; status.textContent='Envoi en cours...';
    try{
      const buf=await chosen.arrayBuffer();
      const r=await fetch('/api/update',{method:'POST',
        headers:{'Content-Type':'application/zip'},body:buf});
      const j=await r.json();
      if(r.ok){ status.className='status ok';
        status.textContent='Mise a jour installee ! Le panneau redemarre... Recharge LUMEN dans 5s.'; }
      else{ status.className='status err'; status.textContent='Erreur : '+(j.error||'inconnue');
        send.classList.add('ready'); }
    }catch(e){ status.className='status err'; status.textContent='Echec reseau : '+e.message;
      send.classList.add('ready'); }
  });
</script></body></html>"""


def save_state():
    try:
        s = engine.STATE.snapshot()
        s.pop("fps", None)
        with open(CONF, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


def load_state():
    try:
        with open(CONF) as f:
            engine.STATE.update(json.load(f))
        print(f"  etat restaure : {engine.STATE.anim} / {engine.STATE.palette}")
    except Exception:
        pass


class Handler(http.server.SimpleHTTPRequestHandler):

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)

        if p.path == "/api/state":
            st = engine.STATE.snapshot()
            st["anims"] = list(engine.ANIMS.keys())
            st["palettes"] = engine.PALETTE_NAMES
            return self._json(200, st)

        if p.path == "/wled/info":
            qs = urllib.parse.parse_qs(p.query)
            host = (qs.get("ip") or [""])[0].strip()
            if not host or not HOST_RE.match(host):
                return self._json(400, {"error": "ip invalide"})
            try:
                with urllib.request.urlopen(f"http://{host}/json/info", timeout=2) as r:
                    body = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json(502, {"error": "wled injoignable", "detail": str(e)[:80]})
            return

        if p.path == "/api/frame":
            # image courante du panneau, 64x64 en RGB brut, soit 12 ko.
            # Sert d'apercu au telephone pour tout ce qu'il ne peut pas
            # recalculer lui-meme : camera, et rendu module par le micro.
            try:
                buf = np.clip(engine.PIX, 0, 255).astype(np.uint8).tobytes()
            except Exception:
                return self._json(500, {"error": "buffer indisponible"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Panel-Size", f"{engine.W}x{engine.H}")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)
            return

        if p.path == "/update":
            html = UPDATE_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        return super().do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)

        if p.path == "/api/audio":
            # frequence elevee (10-20x/s) -> jamais d'ecriture disque ici
            n = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                lvl = max(0.0, min(1.0, float(data.get("level", 0))))
            except Exception:
                return self._json(400, {"error": "donnees invalides"})
            with engine.STATE.lock:
                engine.STATE.audio = lvl
            return self._json(200, {"ok": True})

        if p.path == "/api/update":
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 50 * 1024 * 1024:   # garde-fou : 50 Mo max
                return self._json(400, {"error": "taille de fichier invalide"})
            raw = self.rfile.read(n)
            app_dir = os.path.dirname(os.path.abspath(__file__))
            try:
                # écrit le zip reçu dans un fichier temporaire
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                tmp.write(raw)
                tmp.close()
                # vérifie que c'est un zip valide contenant au moins index.html
                with zipfile.ZipFile(tmp.name) as z:
                    names = z.namelist()
                    if not any(nm.endswith("index.html") for nm in names):
                        os.unlink(tmp.name)
                        return self._json(400, {"error": "zip invalide (index.html manquant)"})
                    # extrait tout dans le dossier de l'app (écrase les fichiers)
                    z.extractall(app_dir)
                os.unlink(tmp.name)
            except zipfile.BadZipFile:
                return self._json(400, {"error": "fichier zip corrompu"})
            except Exception as e:
                return self._json(500, {"error": f"echec extraction : {e}"})
            # répond AVANT de redémarrer (sinon la réponse n'arrive jamais)
            self._json(200, {"ok": True, "message": "mise a jour appliquee, redemarrage..."})
            # planifie un redémarrage du service, en arrière-plan, après la réponse
            try:
                subprocess.Popen(
                    ["/bin/sh", "-c", "sleep 1 && sudo systemctl restart lumen"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return

        if p.path == "/api/image":
            n = int(self.headers.get("Content-Length", 0))
            expected = engine.W * engine.H * 3
            if n != expected:
                return self._json(400, {"error": f"taille attendue {expected} octets, recu {n}"})
            raw = self.rfile.read(n)
            try:
                arr = np.frombuffer(raw, dtype=np.uint8).reshape(engine.H, engine.W, 3)
            except Exception:
                return self._json(400, {"error": "donnees image invalides"})
            engine.PHOTO[:] = arr.astype(np.float32)
            engine.STATE.update({"anim": "photo"})
            save_state()
            return self._json(200, {"ok": True})

        if p.path != "/api/state":
            return self._json(404, {"error": "introuvable"})

        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "json invalide"})

        if "ip" in data and data["ip"] and not HOST_RE.match(str(data["ip"])):
            return self._json(400, {"error": "ip invalide"})
        if "anim" in data and data["anim"] not in engine.ANIMS:
            return self._json(400, {"error": "animation inconnue"})
        if "palette" in data and data["palette"] not in engine.PALETTES:
            return self._json(400, {"error": "palette inconnue"})

        engine.STATE.update(data)
        save_state()

        if "on" in data or "bri" in data:
            st = engine.STATE.snapshot()
            if st["ip"]:
                try:
                    payload = json.dumps({"on": st["on"], "bri": st["bri"]}).encode()
                    req = urllib.request.Request(
                        f"http://{st['ip']}/json/state", data=payload, method="POST",
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=1.5).read()
                except Exception:
                    pass

        return self._json(200, engine.STATE.snapshot())

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("LUMEN")
    print(f"  {len(engine.ANIMS)} animations · {len(engine.PALETTES)} palettes")
    load_state()
    engine.start()
    print(f"  moteur demarre · http://0.0.0.0:{PORT}")
    with Server(("", PORT), Handler) as httpd:
        httpd.serve_forever()
