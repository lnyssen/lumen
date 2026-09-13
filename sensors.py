#!/usr/bin/env python3
"""Sources camera et micro pour LUMEN.

Les deux passent par des outils systeme plutot que par des bibliotheques
Python lourdes : le Pi 3 A+ n'a que 512 Mo et un seul port USB.

  - camera : v4l2-ctl diffuse les images brutes, numpy convertit et reduit.
             v4l-utils est deja installe, rien de plus a ajouter.
  - micro  : arecord fournit des echantillons bruts, numpy calcule le niveau
             et trois bandes de frequences.

Les deux ne demarrent qu'a la demande et s'arretent seuls quand plus personne
ne lit, pour liberer l'USB et, pour la camera, eteindre son temoin.
"""

import math
import subprocess
import threading
import time

import numpy as np

# ─────────────────────────────────────────────────────────── camera

CAM_DEVICE = "/dev/video0"
CAM_IN_W, CAM_IN_H = 160, 120     # plus petit format offert par la C270
CAM_FPS = 15                      # inutile de capturer plus vite que le rendu
IDLE_STOP = 3.0                   # secondes sans lecture avant extinction


class CameraSource:
    """Flux camera reduit a la taille du panneau.

    On lit les images brutes fournies par v4l2-ctl, deja present sur le Pi
    avec v4l-utils : aucune dependance a installer, et bien moins de memoire
    qu'un decodeur complet. La conversion YUYV vers RGB, le recadrage et la
    reduction se font dans numpy, sur 19 200 pixels seulement.
    """

    def __init__(self, size=64, device=CAM_DEVICE):
        self.size = size
        self.device = device
        self.proc = None
        self.thread = None
        self.lock = threading.Lock()
        self.buf = None            # ndarray (size, size, 3) float32
        self.last_read = 0.0
        self.running = False
        self.error = None

        # indices de recadrage : carre centre pris dans le 160x120,
        # puis sous-echantillonnage vers la taille du panneau
        crop = CAM_IN_H
        x0 = (CAM_IN_W - crop) // 2
        xs = (np.arange(size) * crop // size) + x0
        ys = (np.arange(size) * crop // size)
        self._xs = xs[::-1]        # inverse : effet miroir
        self._ys = ys

    # ---- cycle de vie -------------------------------------------------
    def _cmd(self):
        return [
            "v4l2-ctl", "-d", self.device,
            "--set-fmt-video=width=%d,height=%d,pixelformat=YUYV" % (CAM_IN_W, CAM_IN_H),
            "--set-parm=%d" % CAM_FPS,     # 15 images par seconde suffisent,
            "--stream-mmap",                 # et menagent l'USB unique du Pi
            "--stream-to=-",
            "--silent",
        ]

    def start(self):
        if self.running:
            return
        self.running = True
        self.error = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        p, self.proc = self.proc, None
        if p:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
        with self.lock:
            self.buf = None

    # ---- conversion ---------------------------------------------------
    def _decode(self, raw):
        """YUYV empaquete -> RGB recadre et reduit a la taille du panneau."""
        a = np.frombuffer(raw, dtype=np.uint8)
        a = a.reshape(CAM_IN_H, CAM_IN_W // 2, 4).astype(np.int16)
        y0, u, y1, v = a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3]

        # la luminance existe pour chaque pixel, la chrominance pour deux
        y = np.empty((CAM_IN_H, CAM_IN_W), dtype=np.int16)
        y[:, 0::2] = y0
        y[:, 1::2] = y1
        u = np.repeat(u - 128, 2, axis=1)
        v = np.repeat(v - 128, 2, axis=1)

        # on ne convertit que les pixels retenus : 64x64 au lieu de 19 200
        yy = y[np.ix_(self._ys, self._xs)].astype(np.float32)
        uu = u[np.ix_(self._ys, self._xs)].astype(np.float32)
        vv = v[np.ix_(self._ys, self._xs)].astype(np.float32)

        r = yy + 1.402 * vv
        g = yy - 0.344136 * uu - 0.714136 * vv
        b = yy + 1.772 * uu
        return np.clip(np.stack((r, g, b), axis=-1), 0, 255)

    def _loop(self):
        n = CAM_IN_W * CAM_IN_H * 2      # YUYV : deux octets par pixel
        try:
            self.proc = subprocess.Popen(
                self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=n * 2)
        except FileNotFoundError:
            self.error = "v4l2-ctl absent"
            self.running = False
            return
        except Exception as e:
            self.error = str(e)
            self.running = False
            return

        while self.running:
            try:
                raw = self.proc.stdout.read(n)
            except Exception:
                break
            if not raw or len(raw) < n:
                break
            try:
                fr = self._decode(raw)
            except Exception as e:
                self.error = str(e)
                break
            with self.lock:
                self.buf = fr
            if time.time() - self.last_read > IDLE_STOP:
                break

        self.running = False
        p, self.proc = self.proc, None
        if p:
            try:
                p.kill()
            except Exception:
                pass

    # ---- lecture ------------------------------------------------------
    def frame(self):
        """Derniere image, ou None. Demarre la capture au premier appel."""
        self.last_read = time.time()
        if not self.running:
            self.start()
        with self.lock:
            return None if self.buf is None else self.buf.copy()

    @property
    def active(self):
        return self.running and self.buf is not None


# ─────────────────────────────────────────────────────────── micro

MIC_DEVICE = "plughw:CARD=WEBCAM,DEV=0"   # nom plutot que numero : stable au redemarrage
MIC_RATE = 16000
MIC_CHUNK = 1024
NB_BANDS = 16                             # une bande pour quatre colonnes du panneau

# Echelle en decibels plutot qu'en amplitude : l'oreille est logarithmique,
# et une piece calme comme une piece bruyante tombent dans la meme plage.
LVL_FLOOR_DB, LVL_TOP_DB = -58.0, -14.0
BAND_FLOOR_DB, BAND_TOP_DB = -78.0, -26.0


class AudioSource:
    """Niveau sonore, trois bandes larges et un spectre, via arecord."""

    def __init__(self, device=MIC_DEVICE, rate=MIC_RATE, chunk=MIC_CHUNK):
        self.device = device
        self.rate = rate
        self.chunk = chunk
        self.proc = None
        self.thread = None
        self.running = False
        self.error = None
        self.last_read = 0.0
        # niveaux lisses, 0..1
        self.level = 0.0
        self.low = 0.0
        self.mid = 0.0
        self.high = 0.0
        self.beat = 0.0                    # impulsion breve aux attaques
        self.spec = np.zeros(NB_BANDS, dtype=np.float32)
        self._win = np.hanning(chunk).astype(np.float32)
        self._avg = 0.0                    # moyenne glissante, pour detecter les attaques

        # decoupage logarithmique des bandes, de la basse au sifflant
        f = np.fft.rfftfreq(chunk, 1.0 / rate)
        edges = np.geomspace(60.0, 7000.0, NB_BANDS + 1)
        self._idx = [(np.searchsorted(f, edges[i]), max(np.searchsorted(f, edges[i + 1]),
                                                        np.searchsorted(f, edges[i]) + 1))
                     for i in range(NB_BANDS)]
        self._b_low = f < 250
        self._b_mid = (f >= 250) & (f < 2000)
        self._b_high = f >= 2000

    def start(self):
        if self.running:
            return
        self.running = True
        self.error = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        p, self.proc = self.proc, None
        if p:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
        self.level = self.low = self.mid = self.high = self.beat = 0.0
        self.spec[:] = 0.0

    def _cmd(self):
        return ["arecord", "-D", self.device, "-f", "S16_LE",
                "-r", str(self.rate), "-c", "1", "-t", "raw", "-q", "-"]

    def _loop(self):
        nbytes = self.chunk * 2
        try:
            self.proc = subprocess.Popen(
                self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=nbytes * 2)
        except FileNotFoundError:
            self.error = "arecord absent"
            self.running = False
            return
        except Exception as e:
            self.error = str(e)
            self.running = False
            return

        while self.running:
            try:
                raw = self.proc.stdout.read(nbytes)
            except Exception:
                break
            if not raw or len(raw) < nbytes:
                break

            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            # niveau general, en decibels
            rms = float(np.sqrt(np.mean(x * x))) + 1e-9
            db = 20.0 * math.log10(rms)
            lvl = (db - LVL_FLOOR_DB) / (LVL_TOP_DB - LVL_FLOOR_DB)
            lvl = min(1.0, max(0.0, lvl))

            # attaque : ecart brusque au-dessus de la moyenne recente
            self._avg = self._avg * 0.92 + lvl * 0.08
            hit = max(0.0, lvl - self._avg - 0.06) * 4.0
            self.beat = max(self.beat * 0.80, min(1.0, hit))

            # spectre
            mag = np.abs(np.fft.rfft(x * self._win)) + 1e-9
            vals = np.empty(NB_BANDS, dtype=np.float32)
            for i, (a, b) in enumerate(self._idx):
                vals[i] = mag[a:b].mean()
            vdb = 20.0 * np.log10(vals)
            v = np.clip((vdb - BAND_FLOOR_DB) / (BAND_TOP_DB - BAND_FLOOR_DB), 0.0, 1.0)
            # les aigus sont naturellement plus faibles : on redresse la pente
            v *= np.linspace(1.0, 1.45, NB_BANDS)
            v = np.clip(v, 0.0, 1.0)

            up, down = 0.60, 0.16
            k = np.where(v > self.spec, up, down).astype(np.float32)
            self.spec += (v - self.spec) * k

            def smooth(old, new, up=0.55, down=0.14):
                return old + (new - old) * (up if new > old else down)

            self.level = smooth(self.level, lvl)
            self.low = smooth(self.low, float(self.spec[:4].mean()))
            self.mid = smooth(self.mid, float(self.spec[4:10].mean()))
            self.high = smooth(self.high, float(self.spec[10:].mean()))

            if time.time() - self.last_read > IDLE_STOP:
                break

        self.running = False
        p, self.proc = self.proc, None
        if p:
            try:
                p.kill()
            except Exception:
                pass

    def read(self):
        """Niveaux courants. Demarre la capture au premier appel."""
        self.last_read = time.time()
        if not self.running:
            self.start()
        return self.level, self.low, self.mid, self.high

    def bands(self):
        """Spectre normalise, une valeur par bande. Demarre la capture."""
        self.last_read = time.time()
        if not self.running:
            self.start()
        return self.spec

    @property
    def active(self):
        return self.running


# instances partagees par le moteur
CAM = CameraSource()
MIC = AudioSource()
