#!/usr/bin/env python3
"""
LUMEN — afficheur de limites Claude (ESP32)

Le Pi interroge l'endpoint d'usage de Claude (NON documente officiellement par
Anthropic : il peut changer sans preavis) puis pousse deux pourcentages et
deux comptes a rebours vers un petit ecran ESP32 (dossier claude-display/).
L'ecran ne detient jamais le jeton.

Tout est en bibliotheque standard. Desactive tant que le fichier de config
n'existe pas.

Configuration, HORS du dossier servi par server.py (qui sert tout le depot en
HTTP : un secret place ici serait telechargeable depuis le reseau) :
  ~/.config/lumen/claude.json   {"device_host": "...", "device_key": "..."}
  ~/.config/lumen/claude-token  le jeton d'acces OAuth, seul, sur une ligne
Dossier alternatif : variable d'environnement LUMEN_CONFIG_DIR.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONF_DIR = os.path.expanduser(os.environ.get("LUMEN_CONFIG_DIR", "~/.config/lumen"))
CONF_FILE = os.path.join(CONF_DIR, "claude.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

DEFAULTS = {
    "device_host": "claudeusage.local",    # nom mDNS de l'ESP32, ou son IP
    "device_key": "",                      # meme valeur que DEVICE_KEY dans secrets.h
    "token_file": os.path.join(CONF_DIR, "claude-token"),
    "interval": 300,                       # secondes entre deux interrogations de l'API
    "user_agent": "",                      # vide : aucun en-tete User-Agent particulier
    "usage_url": USAGE_URL,                # surchargeable pour les tests
}
HOST_RE = re.compile(r"^[0-9a-zA-Z.\-_]{1,64}(:\d{1,5})?$")
PUSH_EVERY = 120                           # l'ecran affiche « perime » apres 15 min sans nouvelle

_lock = threading.Lock()
_status = {"five": None, "seven": None, "fetched_at": None, "error": None,
           "device": None, "enabled": False}


class FetchError(Exception):
    def __init__(self, code, body, retry_after=None):
        super().__init__(f"HTTP {code}")
        self.code, self.body, self.retry_after = code, body, retry_after


def load_conf():
    """None si l'afficheur n'est pas configure."""
    try:
        with open(CONF_FILE) as f:
            user = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    conf = {**DEFAULTS, **{k: v for k, v in user.items() if k in DEFAULTS}}
    conf["token_file"] = os.path.expanduser(str(conf["token_file"]))
    return conf


def read_token(conf):
    try:
        with open(conf["token_file"]) as f:
            return f.read().strip()
    except OSError:
        return ""


def parse_iso(s):
    """'2026-11-22T18:00:00.123456+00:00' ou '...Z' -> epoch. None si illisible."""
    try:
        s = str(s).strip().replace("Z", "+00:00")
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def parse_usage(raw):
    d = json.loads(raw)
    out = {}
    for key, name in (("five_hour", "five"), ("seven_day", "seven")):
        o = d.get(key) if isinstance(d, dict) else None
        ok = isinstance(o, dict) and isinstance(o.get("utilization"), (int, float))
        out[name] = ({"pct": int(round(o["utilization"])),
                      "reset": parse_iso(o.get("resets_at") or "")} if ok else None)
    if not out["five"] and not out["seven"]:
        raise ValueError("format inattendu")
    return out


def fetch(conf, token):
    req = urllib.request.Request(conf["usage_url"], headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json"})
    if conf["user_agent"]:
        req.add_header("User-Agent", str(conf["user_agent"]))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return parse_usage(r.read())
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace").replace("\n", " ")
        raise FetchError(e.code, body, e.headers.get("Retry-After"))


def push_device(conf, data):
    """Envoie les valeurs a l'ESP32. Renvoie None si OK, sinon un message d'erreur."""
    host, key = str(conf["device_host"]), str(conf["device_key"])
    if not key:
        return "device_key manquante"
    if not HOST_RE.match(host):
        return "device_host invalide"
    now = time.time()
    pct = lambda l: l["pct"] if l else -1
    secs = lambda l: max(0, int(l["reset"] - now)) if l and l["reset"] else -1
    body = urllib.parse.urlencode({
        "key": key,
        "five": pct(data["five"]), "seven": pct(data["seven"]),
        "r5": secs(data["five"]), "r7": secs(data["seven"])}).encode()
    req = urllib.request.Request(f"http://{host}/update", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return None
    except urllib.error.HTTPError as e:
        return f"l'ecran a repondu {e.code}" + (" (cle refusee)" if e.code == 403 else "")
    except Exception as e:
        return f"ecran injoignable : {str(e)[:60]}"


def snapshot():
    with _lock:
        return dict(_status)


def _set(**kw):
    with _lock:
        _status.update(kw)


def _loop():
    next_fetch = next_push = 0.0
    data = None
    while True:
        conf = load_conf()
        if conf is None:
            _set(enabled=False)
            time.sleep(30)
            continue
        _set(enabled=True)
        now = time.time()

        if now >= next_fetch:
            token = read_token(conf)
            if not token:
                _set(error="jeton absent : " + conf["token_file"])
                next_fetch = now + 60
            else:
                try:
                    data = fetch(conf, token)
                    _set(five=data["five"], seven=data["seven"], fetched_at=now, error=None)
                    next_fetch = now + max(60, int(conf["interval"]))
                    next_push = 0          # pousse tout de suite les nouvelles valeurs
                except FetchError as e:
                    _set(error=f"HTTP {e.code} : {e.body}")
                    wait = 600 if e.code in (401, 403, 429) else 120
                    try:
                        wait = max(wait, int(e.retry_after))
                    except (TypeError, ValueError):
                        pass
                    next_fetch = now + wait
                except Exception as e:
                    _set(error=f"{type(e).__name__} : {str(e)[:100]}")
                    next_fetch = now + 120

        if data and now >= next_push:
            _set(device=push_device(conf, data) or "ok")
            next_push = now + PUSH_EVERY

        time.sleep(5)


def start():
    t = threading.Thread(target=_loop, daemon=True, name="claude-usage")
    t.start()
    return t
