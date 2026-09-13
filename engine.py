#!/usr/bin/env python3
"""
LUMEN — moteur d'animations (Raspberry Pi)

Portage fidele du moteur JS de l'app. Le Pi genere les 4096 pixels en continu
et les envoie a WLED en DDP. Le telephone devient une simple telecommande :
on peut le fermer, le panneau continue.

Meme modele que le JS : un buffer RGB persistant, que les animations
remplissent ou attenuent (fade) pour les effets de remanence.
Tout est vectorise numpy — une boucle pixel par pixel en Python plafonnerait
a 2-3 images/seconde sur un Pi 3 A+.
"""

import json
import math
import os
import socket
import struct
import threading
import random
import time
import urllib.request

import numpy as np

import sensors

W = H = 64
N = W * H
CX = (W - 1) / 2.0
CY = (H - 1) / 2.0

# ---------------------------------------------------------------- DDP
DDP_PORT = 4048
_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_seq = 0


# Cache de resolution : si l'adresse du panneau est un nom (ex. lumenpanel.local),
# on le resout en IP et on garde le resultat. Quand le reseau change (transport),
# l'IP du panneau change mais son nom, lui, ne bouge pas : on re-resout tout seul.
_resolve_cache = {}      # nom -> (ip, expiration)
_RESOLVE_TTL = 30        # secondes

def _resolve(host):
    import socket as _s
    # deja une IP ? on la rend telle quelle
    try:
        _s.inet_aton(host)
        return host
    except OSError:
        pass
    now = time.time()
    hit = _resolve_cache.get(host)
    if hit and hit[1] > now:
        return hit[0]
    try:
        ip = _s.gethostbyname(host)
        _resolve_cache[host] = (ip, now + _RESOLVE_TTL)
        return ip
    except OSError:
        # echec : on garde la derniere IP connue si on en a une
        return hit[0] if hit else None

def ddp_send(ip, buf):
    global _seq
    ip = ip.split(":")[0]          # le DDP a son propre port (4048)
    ip = _resolve(ip)              # accepte une IP ou un nom mDNS
    if not ip:
        return
    off, total = 0, len(buf)
    while off < total:
        chunk = buf[off:off + 1440]
        last = (off + len(chunk)) >= total
        _seq = (_seq % 15) + 1
        hdr = struct.pack(">BBBBIH", 0x40 | (0x01 if last else 0),
                          _seq, 0x0B, 0x01, off, len(chunk))
        try:
            _sock.sendto(hdr + chunk, (ip, DDP_PORT))
        except OSError:
            return
        off += len(chunk)


# ---------------------------------------------------------------- palettes
_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "palettes.json")) as f:
    _raw = json.load(f)

PALETTES = {k: np.array(v["lut"], dtype=np.float32) for k, v in _raw.items()}
PALETTE_NAMES = {k: v["name"] for k, v in _raw.items()}
_curpal = PALETTES["mono"]


def pal(v):
    """v: float ou ndarray 0..1 -> RGB float (…,3)"""
    idx = np.clip(np.asarray(v) * 255.0, 0, 255).astype(np.int32)
    return _curpal[idx]


# ---------------------------------------------------------------- buffer
PIX = np.zeros((H, W, 3), dtype=np.float32)

_yy, _xx = np.mgrid[0:H, 0:W].astype(np.float32)
_dx = _xx - CX
_dy = _yy - CY
_hyp = np.sqrt(_dx ** 2 + _dy ** 2)


def fade(amount):
    PIX[:] *= max(0.0, 1.0 - amount)


def clear():
    PIX[:] = 0


# ---------------------------------------------------------------- animations
def plasma(t, p, dt):
    sp, fr, mx = p.get("speed", .8), p.get("freq", 14), p.get("mix", .8)
    a = np.sin(_xx / fr + t * sp)
    b = np.sin(_yy / (fr * .8) + t * sp * 1.3)
    c = np.sin((_xx + _yy) / (fr * 1.2) + t * sp * .7)
    d = np.sin(_hyp / (fr * .6) - t * sp * 1.5)
    v = (a + b + c * mx + d * mx) / (2 + 2 * mx)
    PIX[:] = pal((v + 1) * .5)


def rings(t, p, dt):
    sp, n, sh = p.get("speed", .7), p.get("count", .4), p.get("sharp", 1.5)
    cx = CX + math.sin(t * .3) * (W / 5)
    cy = CY + math.cos(t * .4) * (H / 5)
    d = np.sqrt((_xx - cx) ** 2 + (_yy - cy) ** 2)
    raw = np.sin(d * n * .3 - t * sp * 3)
    v = np.power(np.maximum(0, raw), sh)
    PIX[:] = pal(v)


def moire(t, p, dt):
    f1, f2, sp = p.get("freq1", .25), p.get("freq2", .32), p.get("speed", .9)
    a1, a2 = t * sp * .3, -t * sp * .5
    u1 = (_dx * math.cos(a1) + _dy * math.sin(a1)) * f1
    u2 = (_dx * math.cos(a2) + _dy * math.sin(a2)) * f2
    v = (np.sin(u1) + np.sin(u2)) * .5
    PIX[:] = pal((v + 1) * .5)


def stripes(t, p, dt):
    sp, th = p.get("speed", 1), p.get("thick", 5)
    ang = math.radians(p.get("angle", 30))
    u = _dx * math.cos(ang) + _dy * math.sin(ang)
    v = (np.sin(u / th - t * sp * 2) + 1) * .5
    vv = np.where(v < .5, np.power(v * 2, 2) * .5, 1 - np.power((1 - v) * 2, 2) * .5)
    PIX[:] = pal(vv)


def truchet(t, p, dt):
    cell = max(1, int(p.get("scale", 8)))
    thick, sp = p.get("thick", 1.5), p.get("speed", .6)
    cxg = (_xx // cell)
    cyg = (_yy // cell)
    phase = np.sin(cxg * 1.7 + cyg * 2.3 + t * sp)
    lx = _xx % cell
    ly = _yy % cell
    d = np.where(phase > 0, np.abs(lx - ly), np.abs(lx - (cell - 1 - ly)))
    on = (d <= thick)
    v = np.where(on, np.maximum(.15, .6 + .4 * np.sin(t * sp * .5 + cxg * .3 + cyg * .4)), 0)
    PIX[:] = pal(v)


def glitch(t, p, dt):
    sp, tear, chro = p.get("speed", .8), p.get("tear", .35), p.get("chroma", 1)
    ys = np.arange(H, dtype=np.float32)
    seed = np.sin(ys * 1.7 + math.floor(t * sp * 4) * 7.3)
    xoff = np.where(seed > 1 - tear, np.floor(seed * 8), 0).astype(np.int32)
    boff = np.floor(xoff * chro).astype(np.int32)
    xr = _xx + xoff[:, None]
    xb = _xx - boff[:, None]
    common = np.sin(_yy * .5 - t * sp * 1.1)
    bR = (np.sin(xr * .4 + _yy * .3 + t * sp) + common) * .5
    bG = (np.sin(_xx * .4 + _yy * .3 + t * sp) + common) * .5
    bB = (np.sin(xb * .4 + _yy * .3 + t * sp) + common) * .5
    PIX[:, :, 0] = pal((bR + 1) * .5)[:, :, 0]
    PIX[:, :, 1] = pal((bG + 1) * .5)[:, :, 1]
    PIX[:, :, 2] = pal((bB + 1) * .5)[:, :, 2]


def bars(t, p, dt):
    sp, dens = p.get("speed", .5), p.get("density", .55)
    th = max(1, int(p.get("thick", 2)))
    band = (np.arange(H) // th).astype(np.float32)
    phase = np.sin(band * 1.7 + t * sp) + np.sin(band * .9 - t * sp * .7)
    on = (phase > (2 - dens * 4)).astype(np.float32)
    PIX[:] = pal(on)[:, None, :]


def lines(t, p, dt):
    sp, dens = p.get("speed", .4), p.get("density", .5)
    th = max(1, int(p.get("thick", 2)))
    band = (np.arange(W) // th).astype(np.float32)
    phase = np.sin(band * .45 + t * sp) + np.sin(band * .21 - t * sp * .5)
    on = (phase > (2 - dens * 4)).astype(np.float32)
    PIX[:] = pal(on)[None, :, :]


def static(t, p, dt):
    dens, sp = p.get("density", .18), p.get("speed", .7)
    blk = max(1, int(p.get("block", 1)))
    seed = int(t * sp * 12)
    bh, bw = (H + blk - 1) // blk, (W + blk - 1) // blk
    gy, gx = np.mgrid[0:bh, 0:bw]
    # calculs explicitement en int64 : sur un OS 32 bits, mgrid renvoie de
    # l'int32 par defaut, et numpy >=2.0 leve une OverflowError (au lieu de
    # boucler silencieusement comme avant) des que gx/gy/seed depassent
    # ~2.1 milliards une fois multiplies par les constantes de hachage.
    gx = gx.astype(np.int64)
    gy = gy.astype(np.int64)
    h = (gx * blk * 73856093) ^ (gy * blk * 19349663) ^ (seed * 83492791)
    h = h.astype(np.uint64).astype(np.uint32)
    h = ((h ^ (h >> 16)) * np.uint32(2246822519)).astype(np.uint32)
    h = ((h ^ (h >> 13)) * np.uint32(3266489917)).astype(np.uint32)
    h = (h ^ (h >> 16)).astype(np.uint32)
    v = h.astype(np.float64) / 4294967296.0
    on = (v < dens).astype(np.float32)
    big = np.kron(on, np.ones((blk, blk), dtype=np.float32))[:H, :W]
    PIX[:] = pal(big)


def mire(t, p, dt):
    sp, jit = p.get("speed", .5), p.get("jitter", .2)
    bands = max(2, int(p.get("bands", 8)))
    bw = max(1, W // bands)
    band = (np.arange(W) // bw).astype(np.float32)
    phase = band * 13 * (1 + jit * np.sin(band * 2.1 + t * .5))
    sv = 1 + (band % 4) * .4
    v = (((_yy + t * sp * sv[None, :] * 8 + phase[None, :]) % H) + H) % H / H
    PIX[:] = pal(v)


def sweep(t, p, dt):
    fade(1 - p.get("persist", .85))
    sp = p.get("speed", .6)
    horiz = p.get("axis", 0) < .5
    ln = H if horiz else W
    pos = int((t * sp * ln * .5) % ln)
    c = pal(1.0)
    if horiz:
        PIX[pos, :, :] = c
    else:
        PIX[:, pos, :] = c


def sine(t, p, dt):
    fade(1 - p.get("persist", .9))
    sp, fq, amp = p.get("speed", 1), p.get("freq", .2), p.get("amp", 12)
    c = pal(1.0)
    xs = np.arange(W)
    ys = np.round(CY + np.sin(xs * fq + t * sp) * amp).astype(np.int32)
    ok = (ys >= 0) & (ys < H)
    PIX[ys[ok], xs[ok], :] = c
    y2 = ys - 1
    ok2 = (y2 >= 0) & (y2 < H)
    PIX[y2[ok2], xs[ok2], :] = np.minimum(255, PIX[y2[ok2], xs[ok2], :] + c * .3)


def lissajous(t, p, dt):
    fade(1 - p.get("persist", .93))
    sp = p.get("speed", 1)
    A, B, ph = p.get("freqA", 3), p.get("freqB", 2), p.get("phase", 0)
    rx, ry = W / 2 - 2, H / 2 - 2
    c = pal(1.0)
    for k in range(10):
        tt = t * sp + k * .004
        x = int(round(CX + math.sin(tt * A) * rx))
        y = int(round(CY + math.sin(tt * B + ph) * ry))
        if 0 <= x < W and 0 <= y < H:
            PIX[y, x, :] = c


# --- hilbert ---
def _hilbert(order):
    def d2xy(n, d):
        rx = ry = x = y = 0
        tt, s = d, 1
        while s < n:
            rx = 1 & (tt // 2)
            ry = 1 & (tt ^ rx)
            if ry == 0:
                if rx == 1:
                    x, y = s - 1 - x, s - 1 - y
                x, y = y, x
            x += s * rx
            y += s * ry
            tt //= 4
            s *= 2
        return x, y
    n = 2 ** order
    return [d2xy(n, i) for i in range(n * n)]


_HIL = _hilbert(6)


def hilbert(t, p, dt):
    fade(1 - p.get("persist", .95))
    sp = p.get("speed", 1)
    tail = max(20, int(p.get("tail", 60)))
    ln = len(_HIL)
    cycle = ln * 2
    prog = (t * sp * ln * .5) % cycle
    cursor = int(cycle - prog if prog > ln else prog)
    for k in range(tail):
        i = cursor - k
        if i < 0:
            break
        x, y = _HIL[i]
        PIX[y, x, :] = pal(1 - k / tail)


# --- vie ---
class _Life:
    def __init__(self):
        self.reset()
        self.acc = 0.0

    def reset(self):
        self.g = (np.random.random((H, W)) < .3).astype(np.uint8)
        self.age = np.zeros((H, W), dtype=np.float32)

    def step(self):
        n = sum(np.roll(np.roll(self.g, dy, 0), dx, 1)
                for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx or dy))
        new = (((self.g == 1) & ((n == 2) | (n == 3))) |
               ((self.g == 0) & (n == 3))).astype(np.uint8)
        self.age = np.where(new == 1, self.age + 1, 0)
        self.g = new
        if self.g.sum() < 10:
            self.reset()


_life = _Life()


def life(t, p, dt):
    _life.acc += p.get("speed", .6) * 0.1
    steps = 0
    while _life.acc >= 1 and steps < 4:
        _life.step()
        _life.acc -= 1
        steps += 1
    v = np.where(_life.g == 1, np.minimum(1.0, _life.age / 20.0), 0.0)
    PIX[:] = pal(v)


# --- reaction-diffusion ---
class _RD:
    def __init__(self):
        self.reset()

    def reset(self):
        self.U = np.ones((H, W), dtype=np.float32)
        self.V = np.zeros((H, W), dtype=np.float32)
        s = slice(28, 36)
        self.V[s, s] = 1.0

    @staticmethod
    def _lap(X):
        return (np.roll(X, 1, 0) + np.roll(X, -1, 0) +
                np.roll(X, 1, 1) + np.roll(X, -1, 1) - 4 * X)


_rd = _RD()


def rd(t, p, dt):
    f, k = p.get("feed", .037), p.get("kill", .06)
    iters = max(1, int(p.get("iters", 6)))
    for _ in range(iters):
        U, V = _rd.U, _rd.V
        uvv = U * V * V
        _rd.U = U + .16 * _RD._lap(U) - uvv + f * (1 - U)
        _rd.V = V + .08 * _RD._lap(V) + uvv - (k + f) * V
    PIX[:] = pal(np.clip(_rd.V * 4, 0, 1))


# --- flot (particules) ---
class _Flow:
    def __init__(self):
        self.n = 0
        self.seed(80)

    def seed(self, n):
        self.n = n
        self.x = np.random.random(n).astype(np.float32) * W
        self.y = np.random.random(n).astype(np.float32) * H
        self.age = np.random.random(n).astype(np.float32) * 3


_flow = _Flow()


def flow(t, p, dt):
    n = max(1, int(p.get("count", 80)))
    sp, pers = p.get("speed", 1), p.get("persist", .92)
    if _flow.n != n:
        _flow.seed(n)
    fade(1 - pers)
    f = _flow
    ang = (np.sin(f.x * .3 + t * .2) + np.cos(f.y * .3 - t * .15)) * math.pi
    f.x += np.cos(ang) * sp * .4
    f.y += np.sin(ang) * sp * .4
    f.age += .05
    dead = (f.x < 0) | (f.x >= W) | (f.y < 0) | (f.y >= H) | (f.age > 6)
    k = int(dead.sum())
    if k:
        f.x[dead] = np.random.random(k) * W
        f.y[dead] = np.random.random(k) * H
        f.age[dead] = 0
    v = .4 + .5 * np.sin(f.age * 1.2)
    cols = pal(np.clip(v, 0, 1))
    ix = np.clip(f.x.astype(np.int32), 0, W - 1)
    iy = np.clip(f.y.astype(np.int32), 0, H - 1)
    np.add.at(PIX, (iy, ix), cols)
    np.clip(PIX, 0, 255, out=PIX)


# --- EKG ---
class _EKG:
    def __init__(self):
        self.buf = np.zeros(W, dtype=np.float32)
        self.last = 0


_ekg = _EKG()


def ekg(t, p, dt):
    fade(1 - p.get("persist", .88))
    bpm, amp = p.get("bpm", 60), p.get("amp", 1)
    rate = 20
    tick = int(t * rate)
    if tick != _ekg.last:
        steps = min(W, tick - _ekg.last)
        for s in range(steps):
            _ekg.buf[:-1] = _ekg.buf[1:]
            tt = (_ekg.last + s + 1) / rate
            bi = 60.0 / max(bpm, 1)
            ph = (tt % bi) / bi
            if ph < .04:
                v = ph * 25
            elif ph < .08:
                v = 1 - (ph - .04) * 30
            elif ph < .11:
                v = -.4 + (ph - .08) * 15
            else:
                v = 0.0
            _ekg.buf[-1] = v
        _ekg.last = tick
    c = pal(1.0)
    xs = np.arange(W)
    ys = np.round(CY - _ekg.buf * (H / 2.5) * amp).astype(np.int32)
    ok = (ys >= 0) & (ys < H)
    PIX[ys[ok], xs[ok], :] = c


# --- texte / horloges ---
_GLYPH_SRC = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "001", "001", "001"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    ":": ["000", "010", "000", "010", "000"], " ": ["000", "000", "000", "000", "000"],
    "A": ["111", "101", "111", "101", "101"], "B": ["110", "101", "110", "101", "110"],
    "C": ["111", "100", "100", "100", "111"], "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "111", "100", "111"], "F": ["111", "100", "111", "100", "100"],
    "G": ["111", "100", "101", "101", "111"], "H": ["101", "101", "111", "101", "101"],
    "I": ["111", "010", "010", "010", "111"], "J": ["001", "001", "001", "101", "111"],
    "K": ["101", "110", "100", "110", "101"], "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101"], "N": ["101", "111", "111", "111", "101"],
    "O": ["111", "101", "101", "101", "111"], "P": ["111", "101", "111", "100", "100"],
    "Q": ["111", "101", "101", "111", "001"], "R": ["111", "101", "110", "101", "101"],
    "S": ["111", "100", "111", "001", "111"], "T": ["111", "010", "010", "010", "010"],
    "U": ["101", "101", "101", "101", "111"], "V": ["101", "101", "101", "101", "010"],
    "W": ["101", "101", "111", "111", "101"], "X": ["101", "101", "010", "101", "101"],
    "Y": ["101", "101", "010", "010", "010"], "Z": ["111", "001", "010", "100", "111"],
    "-": ["000", "000", "111", "000", "000"], ".": ["000", "000", "000", "000", "010"],
    "!": ["010", "010", "010", "000", "010"], "?": ["111", "001", "011", "000", "010"],
    ",": ["000", "000", "000", "010", "100"], "'": ["010", "010", "000", "000", "000"],
    ";": ["000", "010", "000", "010", "100"], "\"": ["101", "101", "000", "000", "000"],
    "(": ["001", "010", "010", "010", "001"], ")": ["100", "010", "010", "010", "100"],
    "/": ["001", "001", "010", "100", "100"], "&": ["010", "101", "010", "101", "011"],
    "+": ["000", "010", "111", "010", "000"], "*": ["000", "101", "010", "101", "000"],
    "%": ["101", "001", "010", "100", "101"], "\\": ["100", "100", "010", "001", "001"],
    "\u00b0": ["010", "101", "010", "000", "000"], "\u20ac": ["011", "110", "011", "110", "011"],
}
_GLYPH = {k: np.array([[int(c) for c in r] for r in v], dtype=np.float32)
          for k, v in _GLYPH_SRC.items()}


def _blit(mask, s, x0, y0, scale):
    for ch in s:
        g = _GLYPH.get(ch)
        if g is not None and scale >= 1:
            big = np.kron(g, np.ones((scale, scale), dtype=np.float32))
            hh, ww = big.shape
            xs, xe = max(0, x0), min(W, x0 + ww)
            ys, ye = max(0, y0), min(H, y0 + hh)
            if xe > xs and ye > ys:
                seg = big[ys - y0:ye - y0, xs - x0:xe - x0]
                mask[ys:ye, xs:xe] = np.maximum(mask[ys:ye, xs:xe], seg)
        x0 += 4 * scale
    return mask


def clock(t, p, dt):
    """HH au-dessus de MM, separateur clignotant. Aligne sur renderClock (JS)."""
    lt = time.localtime()
    hh = f"{lt.tm_hour:02d}"
    mm = f"{lt.tm_min:02d}"
    scale = 4
    stride = 4 * scale
    w = len(hh) * stride - scale
    x = (W - w) // 2
    m = np.zeros((H, W), dtype=np.float32)
    _blit(m, hh, x, 4, scale)
    _blit(m, mm, x, 34, scale)
    if lt.tm_sec % 2 == 0:
        cx, cy = W // 2, H // 2
        for ddx in range(-2, 3):
            for ddy in range(-2, 3):
                px, py = cx + ddx, cy + ddy
                if 0 <= px < W and 0 <= py < H:
                    m[py, px] = max(m[py, px], 0.6)
    if p.get("seconds", 0) > .5:
        barw = int((lt.tm_sec / 60.0) * W)
        m[H - 2:H, :barw] = np.maximum(m[H - 2:H, :barw], 0.35)
    PIX[:] = pal(m)


_tc = {"sec": -1, "frame": 0}


def timecode(t, p, dt):
    """Une seule ligne HH:MM:SS:FF. Aligne sur renderTimecode (JS)."""
    lt = time.localtime()
    if lt.tm_sec != _tc["sec"]:
        _tc["sec"] = lt.tm_sec
        _tc["frame"] = 0
    else:
        _tc["frame"] = (_tc["frame"] + 1) % 25
    txt = f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}:{_tc['frame']:02d}"
    scale = 1
    stride = 4 * scale
    w = len(txt) * stride - scale
    x = (W - w) // 2
    y = (H - 5 * scale) // 2
    m = np.zeros((H, W), dtype=np.float32)
    _blit(m, txt, x, y, scale)
    if p.get("bar", 1) > .5:
        prog = int((_tc["frame"] / 25.0) * W)
        m[H - 2:H, :prog] = np.maximum(m[H - 2:H, :prog], 0.5)
    PIX[:] = pal(m)


def clock2(t, p, dt):
    clear()
    lt = time.localtime()
    m = np.zeros((H, W), dtype=np.float32)
    m[np.abs(_hyp - 29) < 1] = .3
    if p.get("ticks", 1) > .5:
        for i in range(12):
            a = i * math.tau / 12
            x = int(round(CX + math.sin(a) * 26))
            y = int(round(CY - math.cos(a) * 26))
            if 0 <= x < W and 0 <= y < H:
                m[y, x] = .9

    def hand(angle, length, val):
        steps = int(length * 3)
        for i in range(steps):
            f = i / max(steps - 1, 1)
            x = int(round(CX + math.sin(angle) * length * f))
            y = int(round(CY - math.cos(angle) * length * f))
            if 0 <= x < W and 0 <= y < H:
                m[y, x] = val

    hand((lt.tm_hour % 12 + lt.tm_min / 60) * math.tau / 12, 15, 1.0)
    hand((lt.tm_min + lt.tm_sec / 60) * math.tau / 60, 23, .85)
    if p.get("seconds", 1) > .5:
        hand(lt.tm_sec * math.tau / 60, 26, .5)
    PIX[:] = pal(m)


_PUNCT_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201b": "'", "\u02bc": "'",
    "\u00b4": "'", "\u0060": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...", "\u00a0": " ", "\u202f": " ", "\u2009": " ",
    "\u00e9": "E", "\u00e8": "E", "\u00ea": "E", "\u00eb": "E",
    "\u00e0": "A", "\u00e2": "A", "\u00e4": "A",
    "\u00ee": "I", "\u00ef": "I", "\u00f4": "O", "\u00f6": "O",
    "\u00f9": "U", "\u00fb": "U", "\u00fc": "U", "\u00e7": "C",
    "\u0153": "OE", "\u00e6": "AE",
}

def _norm_txt(s):
    out = []
    for ch in s:
        out.append(_PUNCT_MAP.get(ch, ch))
    return "".join(out).upper()

def text(t, p, dt):
    msg = _norm_txt(str(p.get("text", "LUMEN")))
    sc = max(1, int(p.get("scale", 3)))
    sp = p.get("speed", 1)
    bg = p.get("bg", 0)
    PIX[:] = pal(bg) if bg else 0
    gw = 4 * sc
    total = len(msg) * gw
    off = int((t * sp * 12) % (total + W))
    m = np.zeros((H, W), dtype=np.float32)
    _blit(m, msg, W - off, (H - 5 * sc) // 2, sc)
    col = pal(1.0)
    PIX[m > 0] = col


def checkers(t, p, dt):
    """Damier — grille rotative pulsante. Portage fidele de renderCheckers (JS)."""
    sp, cell = p.get("speed", .6), max(2, int(p.get("scale", 9)))
    ang = t * sp * 0.2
    ca, sa = math.cos(ang), math.sin(ang)
    dx, dy = _xx - CX, _yy - CY
    u = dx * ca - dy * sa
    v2 = dx * sa + dy * ca
    cxi = np.floor(u / cell)
    cyi = np.floor(v2 / cell)
    on = (np.mod(cxi + cyi, 2) == 0)
    pulse = 0.5 + 0.5 * np.sin(t * sp * 2 + cxi * 0.5 + cyi * 0.5)
    v = np.where(on, pulse, 0.06)
    PIX[:] = pal(v)


def spiral(t, p, dt):
    """Spirale — bras tournants, resserrement reglable. Portage de renderSpiral (JS)."""
    sp, arms, tight = p.get("speed", .8), p.get("arms", 4), p.get("tight", 2)
    dx, dy = _xx - CX, _yy - CY
    ang = np.arctan2(dy, dx)
    d = np.hypot(dx, dy)
    v = (np.sin(ang * arms + d * tight * 0.1 - t * sp * 3) + 1) * 0.5
    PIX[:] = pal(v)


def star(t, p, dt):
    """Etoile — rayons qui tournent depuis le centre. Portage de renderStar (JS)."""
    sp, rays, sharp = p.get("speed", .6), p.get("rays", 8), p.get("sharp", 2)
    dx, dy = _xx - CX, _yy - CY
    ang = np.arctan2(dy, dx) + t * sp * 0.5
    d = np.hypot(dx, dy)
    raw = np.power(np.abs(np.sin(ang * rays * 0.5)), sharp)
    fall = np.maximum(0, 1 - d / (W * 0.62))
    v = raw * fall
    PIX[:] = pal(v)


def kaleido(t, p, dt):
    """Kaleidoscope — segments radiaux en miroir. Portage de renderKaleido (JS)."""
    sp, seg, sc = p.get("speed", .7), max(2, int(p.get("segments", 6))), p.get("scale", 1)
    ang0 = t * sp * 0.3
    dx, dy = _xx - CX, _yy - CY
    a = np.arctan2(dy, dx) - ang0
    seg_a = (2 * np.pi) / seg
    a = np.abs(np.mod(np.mod(a, seg_a) + seg_a, seg_a) - seg_a / 2)
    r = np.hypot(dx, dy)
    v = (np.sin(r * sc * 0.25 - t * sp * 1.5) * np.cos(a * 3) + 1) * 0.5
    PIX[:] = pal(v)


_orbit_buf = np.zeros((H, W), dtype=np.float32)


def orbit(t, p, dt):
    """Orbite — points lumineux qui tournent avec remanence. Portage de renderOrbit (JS)."""
    global _orbit_buf
    sp = p.get("speed", .8)
    n = max(1, int(p.get("count", 5)))
    persist = p.get("persist", .88)
    _orbit_buf *= persist
    for i in range(n):
        a = t * sp * (0.4 + i * 0.15) + i * (2 * math.pi / n)
        rad = 8 + (i % 4) * 6
        x = int(round(CX + math.cos(a) * rad))
        y = int(round(CY + math.sin(a) * rad * 0.9))
        if 0 <= x < W and 0 <= y < H:
            _orbit_buf[y, x] = 1.0
    PIX[:] = pal(np.minimum(1.0, _orbit_buf))


PHOTO = np.zeros((H, W, 3), dtype=np.float32)


def photo(t, p, dt):
    """Affiche simplement la derniere photo envoyee par l'app — pas de calcul,
    pas de palette (les couleurs de la photo sont deja les bonnes)."""
    PIX[:] = PHOTO


# ---------------------------------------------------------------- meteo
# Donnees reelles Bruxelles via Open-Meteo (gratuit, sans cle).
# Un thread rafraichit toutes les 15 min ; le rendu lit juste l'etat courant.
_weather = {"code": None, "temp": None}


def _weather_fetch_loop():
    url = ("https://api.open-meteo.com/v1/forecast"
           "?latitude=50.85&longitude=4.35&current_weather=true")
    while True:
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.loads(r.read().decode())
            cw = d.get("current_weather", {})
            _weather["code"] = cw.get("weathercode")
            _weather["temp"] = int(round(cw.get("temperature")))
        except Exception:
            pass
        time.sleep(15 * 60)


# demarre le thread une seule fois, en arriere-plan
threading.Thread(target=_weather_fetch_loop, daemon=True).start()


def _weather_kind(code):
    if code is None:
        return "load"
    if code == 0:
        return "clear"
    if code <= 3:
        return "cloud"
    if 45 <= code <= 48:
        return "fog"
    if 51 <= code <= 67:
        return "rain"
    if 71 <= code <= 77:
        return "snow"
    if 80 <= code <= 82:
        return "rain"
    if code >= 95:
        return "storm"
    return "cloud"


def _disc(m, cx, cy, r, val):
    ys, ye = max(0, int(cy - r)), min(H, int(cy + r) + 1)
    xs, xe = max(0, int(cx - r)), min(W, int(cx + r) + 1)
    if xe <= xs or ye <= ys:
        return
    yy, xx = np.mgrid[ys:ye, xs:xe]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    m[ys:ye, xs:xe][mask] = np.maximum(m[ys:ye, xs:xe][mask], val)


def _cloud(m, cx, cy, top, base):
    """Nuage net : bosses arrondies + base plate."""
    R = W * 0.09
    _disc(m, cx - R * 1.1, cy + R * 0.5, R * 0.85, base)
    _disc(m, cx + R * 1.1, cy + R * 0.5, R * 0.9, base)
    _disc(m, cx - R * 0.3, cy + R * 0.2, R * 1.0, top)
    _disc(m, cx + R * 0.5, cy, R * 0.95, top)
    y0, y1 = int(cy + R * 0.5), int(cy + R * 1.1)
    x0, x1 = int(cx - R * 1.6), int(cx + R * 1.6)
    for y in range(max(0, y0), min(H, y1 + 1)):
        for x in range(max(0, x0), min(W, x1 + 1)):
            m[y, x] = max(m[y, x], base)


def weather(t, p, dt):
    """Pictogramme meteo reel de Bruxelles, colorise par la palette courante."""
    clear()
    m = np.zeros((H, W), dtype=np.float32)
    kind = _weather_kind(_weather["code"])
    scx, scy = W * 0.5, H * 0.5

    if kind == "clear":
        R = W * 0.11
        pulse = 1 + 0.08 * math.sin(t * 2)
        _disc(m, scx, scy, R * pulse, 1.0)
        for k in range(8):
            a = k * math.pi / 4 + t * 0.4
            r1, r2 = R * 1.5, R * 2.1 + math.sin(t * 3 + k)
            steps = int(r2 - r1) + 1
            for i in range(steps):
                f = r1 + i
                x = int(round(scx + math.cos(a) * f))
                y = int(round(scy + math.sin(a) * f))
                if 0 <= x < W and 0 <= y < H:
                    m[y, x] = 1.0
    elif kind in ("cloud", "fog"):
        _cloud(m, scx, scy - 2, 1.0, 0.6)
        if kind == "fog":
            for k in range(3):
                y = int(scy + 13 + k * 4)
                if 0 <= y < H:
                    off = (t * 8 + k * 10)
                    xs = np.arange(W)
                    line = 0.35 + 0.35 * np.sin((xs + off) * 0.25)
                    m[y, 6:W - 6] = np.maximum(m[y, 6:W - 6], line[6:W - 6])
    elif kind in ("rain", "storm"):
        _cloud(m, scx, scy - 4, 0.6, 0.4)
        for k in range(6):
            bx = int(scx - 16 + k * 6)
            phase = (t * 34 + k * 20) % 26
            y = int(scy + 8 + phase)
            for ddy in range(3):
                yy, xx = y + ddy, bx + (ddy >> 1)
                if 0 <= yy < H - 2 and 0 <= xx < W:
                    m[yy, xx] = 1.0
        if kind == "storm" and math.sin(t * 8) > 0.4:
            zx, zy = int(scx), int(scy + 6)
            for (x, y) in ((zx + 2, zy), (zx, zy + 3), (zx - 2, zy + 7),
                           (zx, zy + 7), (zx + 2, zy + 7), (zx, zy + 11), (zx - 1, zy + 16)):
                if 0 <= x < W and 0 <= y < H:
                    m[y, x] = 1.0
    elif kind == "snow":
        _cloud(m, scx, scy - 4, 1.0, 0.6)
        for k in range(6):
            bx = int(scx - 15 + k * 6 + math.sin(t * 1.5 + k) * 1.5)
            y = int(scy + 9 + (t * 14 + k * 15) % 24)
            if 0 <= y < H - 1:
                for (dx, dy) in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                    xx, yy = bx + dx, y + dy
                    if 0 <= xx < W and 0 <= yy < H:
                        m[yy, xx] = 1.0
    else:
        v = 0.4 + 0.4 * math.sin(t * 3)
        _disc(m, scx, scy, W * 0.05, v)

    # heure en haut, centree
    htxt = time.strftime("%H:%M")
    hscale = 2
    hstride = 4 * hscale
    hw = len(htxt) * hstride - hscale
    _blit(m, htxt, (W - hw) // 2, 3, hscale)

    if _weather["temp"] is not None:
        txt = f"{_weather['temp']}C"
        scale = 2
        stride = 4 * scale
        w = len(txt) * stride - scale
        _blit(m, txt, (W - w) // 2, H - 14, scale)

    PIX[:] = pal(m)


def orb(t, p, dt):
    """Disque chaud pose sur un degrade lent et large."""
    sp = p.get("speed", .25)
    size = p.get("size", .28)
    glow = p.get("glow", .5)
    warm = p.get("warm", 1.0)

    # fond : trois ondes tres larges qui derivent, cadrees sur la taille du panneau
    a = np.sin(_xx / (W * .40) + t * sp)
    b = np.sin(_yy / (H * .47) - t * sp * .8)
    c = np.sin((_xx + _yy) / (W * .62) + t * sp * .6)
    v = (a + b + c) / 3.0
    # etalement sur toute la palette : sans lui le fond n'utilise qu'une frange
    lo, hi = float(v.min()), float(v.max())
    bg = pal((v - lo) / (hi - lo) if hi - lo > 1e-3 else v * 0 + .5)

    # disque central, respiration lente, bord adouci
    r = W * size * (1 + .04 * math.sin(t * .8))
    edge = max(.8, W * .08 * glow)
    m = np.clip((r - _hyp) / edge, 0, 1)[..., None]

    amber = np.array([255., 150., 45.], dtype=np.float32)
    core = pal(.06) * (1.0 - warm) + amber * warm
    PIX[:] = bg * (1 - m) + core * m




# ─────────────────────────────────────────────── MODE JOURNEE
# L'objet vit une journee entiere sans qu'on y touche : l'ambiance suit l'heure.
# Chaque tranche propose des animations douces ou franches, une palette et une
# luminosite. On change d'animation de temps en temps a l'interieur d'une tranche.
DAY_SCENES = [
    # (heure_debut, nom, [animations], palette, luminosite 0..255)
    (0,  "nuit",   ["flow", "sine", "rings"],              "ink",      70),
    (6,  "aube",   ["plasma", "flow", "moire"],            "vapeur",  150),
    (9,  "matin",  ["plasma", "truchet", "spiral"],        "airdraw", 220),
    (12, "midi",   ["plasma", "kaleido", "orbit", "life"], "prisme",  255),
    (17, "soir",   ["flow", "plasma", "moire", "sine"],    "couchant",190),
    (21, "nuit",   ["flow", "sine", "rings", "star"],      "ecliptique",110),
]

def day_scene(now_local):
    """Renvoie (animations, palette, bri) pour l'heure donnee."""
    h = now_local.tm_hour
    chosen = DAY_SCENES[0]
    for sc in DAY_SCENES:
        if h >= sc[0]:
            chosen = sc
    return chosen[2], chosen[3], chosen[4]


# ─────────────────────────────────────────────── AUJOURD'HUI
# L'oeuvre du jour : une composition tiree de la date, unique et jamais rejouee.
# Elle choisit elle-meme sa forme, sa symetrie, son mouvement et sa palette.
# Le meme generateur tourne dans l'app (JS) : il doit rester identique au bit
# de logique pres, d'ou le generateur pseudo-aleatoire portable ci-dessous.

TODAY_PALETTES = ["airdraw","prisme","neonuit","vitrail","couchant","ecliptique",
                  "iris","ocean","forest","maree","recif","laser","lagon","solaire",
                  "magnetique","carnaval","givre","jade","pop","arcade"]

def _mulberry32(a):
    """Generateur deterministe, identique a sa version JavaScript."""
    a &= 0xffffffff
    state = [a]
    def nxt():
        state[0] = (state[0] + 0x6D2B79F5) & 0xffffffff
        t = state[0]
        t = ((t ^ (t >> 15)) * (1 | t)) & 0xffffffff
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & 0xffffffff)) & 0xffffffff) ^ t
        t &= 0xffffffff
        return ((t ^ (t >> 14)) & 0xffffffff) / 4294967296.0
    return nxt

_today_seed = None
_today = None

def _today_build(seed):
    r = _mulberry32(seed)
    nl = 3 + int(r() * 3)                     # 3 a 5 couches
    layers = []
    wsum = 0.0
    for _ in range(nl):
        fx = 0.05 + r() * 0.34
        fy = 0.05 + r() * 0.34
        ph = r() * 6.2832
        sp = (0.15 + r() * 0.7) * (1.0 if r() < 0.5 else -1.0)
        w = 0.5 + r() * 0.5
        wsum += w
        layers.append((fx, fy, ph, sp, w))
    sym = int(r() * 3)                        # 0 aucune, 1 miroir, 2 quadrant
    rot = r() * 6.2832
    warp = r() * 0.6                          # ondulation du repere
    palkey = TODAY_PALETTES[int(r() * len(TODAY_PALETTES))]
    return dict(layers=layers, wsum=wsum, sym=sym, rot=rot, warp=warp, palkey=palkey)

def aujourdhui(t, p, dt):
    global _today_seed, _today
    lt = time.localtime()
    seed = lt.tm_year * 10000 + (lt.tm_mon) * 100 + lt.tm_mday
    if seed != _today_seed or _today is None:
        _today_seed = seed
        _today = _today_build(seed)

    g = _today
    ca, sa = math.cos(g["rot"]), math.sin(g["rot"])
    xr = _dx * ca - _dy * sa
    yr = _dx * sa + _dy * ca
    if g["warp"] > 0.02:
        xr = xr + g["warp"] * 6.0 * np.sin(yr * 0.08 + t * 0.3)
        yr = yr + g["warp"] * 6.0 * np.sin(xr * 0.08 - t * 0.25)
    if g["sym"] >= 1:
        xr = np.abs(xr)
    if g["sym"] == 2:
        yr = np.abs(yr)

    acc = np.zeros((H, W), dtype=np.float32)
    for (fx, fy, ph, sp, w) in g["layers"]:
        acc += np.sin(xr * fx + yr * fy + ph + t * sp) * w
    v = acc / g["wsum"] * 0.5 + 0.5

    lut = PALETTES.get(g["palkey"], PALETTES["airdraw"])
    idx = np.clip(v * 255.0, 0, 255).astype(np.int32)
    PIX[:] = lut[idx]


# ─────────────────────────────────────────────── CAMERA
# Le flux arrive deja en 64x64 RGB depuis sensors.py : ici on ne fait
# qu'interpreter l'image, jamais de decodage lourd.

_cam_bg = None          # fond de reference, moyenne glissante
_cam_trail = None       # remanence des mouvements


def _cam_absent(msg_dark=(20, 6, 30)):
    """Camera indisponible : on affiche un fond sourd plutot qu'un ecran noir."""
    PIX[:] = np.array(msg_dark, dtype=np.float32)


def cam_mirror(t, p, dt):
    """Miroir : l'image telle quelle, contrastee, eventuellement teintee."""
    fr = sensors.CAM.frame()
    if fr is None:
        _cam_absent()
        return
    gain = p.get("gain", 1.0)
    tint = p.get("tint", 0.0)

    img = fr / 255.0
    m = float(img.mean())
    img = np.clip((img - m) * (0.6 + gain * 1.8) + m, 0.0, 1.0)

    if tint > 0.001:
        lum = img[:, :, 0] * .299 + img[:, :, 1] * .587 + img[:, :, 2] * .114
        col = pal(lum)
        img = img * 255.0 * (1.0 - tint) + col * tint
    else:
        img = img * 255.0
    PIX[:] = img


def cam_silhouette(t, p, dt):
    """Silhouette : ce qui bouge se detache du fond, en deux tons de palette."""
    global _cam_bg
    fr = sensors.CAM.frame()
    if fr is None:
        _cam_absent()
        return
    seuil = p.get("seuil", .5)
    memoire = p.get("memoire", .5)
    doux = p.get("doux", .3)

    lum = (fr[:, :, 0] * .299 + fr[:, :, 1] * .587 + fr[:, :, 2] * .114) / 255.0
    if _cam_bg is None or _cam_bg.shape != lum.shape:
        _cam_bg = lum.copy()
    # fond appris lentement : plus memoire est haut, plus il fige le decor
    k = 0.002 + (1.0 - memoire) * 0.12
    _cam_bg = _cam_bg * (1 - k) + lum * k

    d = np.abs(lum - _cam_bg)
    s = 0.02 + seuil * 0.28
    if doux < .02:
        m = (d > s).astype(np.float32)
    else:
        m = np.clip((d - s) / (doux * 0.25), 0.0, 1.0)

    PIX[:] = pal(m * 0.92 + 0.04)


def cam_trace(t, p, dt):
    """Traces : le mouvement laisse une remanence qui s'efface lentement."""
    global _cam_bg, _cam_trail
    fr = sensors.CAM.frame()
    if fr is None:
        _cam_absent()
        return
    persist = p.get("persist", .6)
    force = p.get("force", .6)

    lum = (fr[:, :, 0] * .299 + fr[:, :, 1] * .587 + fr[:, :, 2] * .114) / 255.0
    if _cam_bg is None or _cam_bg.shape != lum.shape:
        _cam_bg = lum.copy()
    if _cam_trail is None or _cam_trail.shape != lum.shape:
        _cam_trail = np.zeros_like(lum)

    _cam_bg = _cam_bg * .94 + lum * .06
    d = np.abs(lum - _cam_bg) * (1.0 + force * 6.0)

    decay = 0.90 + persist * 0.095          # 0.90 a 0.995
    _cam_trail = np.maximum(_cam_trail * decay, np.clip(d, 0, 1))
    PIX[:] = pal(np.clip(_cam_trail, 0, 1))



# ─────────────────────────────────────────────── SON
# Ces animations lisent le micro directement : elles le demarrent en arrivant
# et il s'arrete seul quand on passe a autre chose. Inutile d'activer le mode
# sonore de l'en-tete, elles sont sonores par nature.

_NB = sensors.NB_BANDS
_spec_peak = np.zeros(_NB, dtype=np.float32)
_vgrad = None            # degrade vertical de la palette, recalcule si besoin
_rings = []              # anneaux emis sur les attaques


def _grad_col():
    """Couleur de chaque ligne, du bas vers le haut de la palette."""
    global _vgrad
    _vgrad = pal(np.linspace(1.0, 0.0, H)).astype(np.float32)
    return _vgrad


def snd_spectre(t, p, dt):
    """Spectre : une barre par bande de frequences, du grave a gauche."""
    global _spec_peak
    b = sensors.MIC.bands()
    sens = STATE.sound_sens
    chute = p.get("chute", .5)
    miroir = p.get("miroir", 0.)

    v = np.clip(b * (0.45 + sens * 1.7), 0.0, 1.0)
    if miroir > .5:
        # graves au centre, aigus vers les bords
        half = v[:_NB // 2][::-1]
        v = np.concatenate([half, half[::-1]])
    _spec_peak = np.maximum(_spec_peak - dt * (0.12 + chute * 1.1), v)

    col = _grad_col()
    clear()
    w = max(1, W // _NB)
    for i in range(_NB):
        x0 = i * w
        x1 = x0 + w if i < _NB - 1 else W
        h = int(v[i] * H)
        if h > 0:
            PIX[H - h:H, x0:x1] = col[H - h:H, None, :]
        # trace du maximum recent, qui redescend lentement
        ph = int(_spec_peak[i] * H)
        if ph > 1:
            r = H - ph
            if 0 <= r < H:
                PIX[r, x0:x1] = col[r] * .55 + 110.0


def snd_pulse(t, p, dt):
    """Pulse : un disque qui respire au niveau, et des anneaux sur les attaques."""
    global _rings
    lvl, lo, mi, hi = sensors.MIC.read()
    sens = STATE.sound_sens
    taille = p.get("taille", .5)
    onde = p.get("onde", .6)

    v = min(1.0, lvl * (0.45 + sens * 1.7))

    # fond sourd, teinte par les graves
    PIX[:] = pal(np.full((H, W), 0.05 + lo * 0.18, dtype=np.float32))

    # anneaux : un par attaque, ils s'elargissent puis s'effacent
    if sensors.MIC.beat > .35 and onde > .05:
        _rings.append(0.0)
        if len(_rings) > 5:
            _rings.pop(0)
    alive = []
    for r in _rings:
        r += dt * (14.0 + onde * 34.0)
        if r < W * .78:
            alive.append(r)
            fade = max(0.0, 1.0 - r / (W * .78))
            m = np.clip(1.0 - np.abs(_hyp - r) / 1.8, 0.0, 1.0) * fade
            PIX[:] = PIX * (1 - m[..., None]) + pal(np.float32(.82)) * m[..., None]
    _rings = alive

    # le disque
    rad = W * (0.05 + taille * 0.42 * v)
    m = np.clip((rad - _hyp) / 1.6, 0.0, 1.0)[..., None]
    core = pal(np.float32(0.62 + v * 0.36))
    PIX[:] = PIX * (1 - m) + core * m


def snd_onde(t, p, dt):
    """Onde : une ligne qui ondule, chaque registre pilotant sa frequence."""
    lvl, lo, mi, hi = sensors.MIC.read()
    sens = STATE.sound_sens
    ampl = p.get("ampl", .6)
    trait = p.get("trait", .4)

    g = 0.45 + sens * 1.7
    a = (H * 0.42) * ampl
    xs = _xx[0]
    y = (CY
         + a * min(1.0, lo * g) * np.sin(xs / (W / 9.0) + t * 2.1)
         + a * .62 * min(1.0, mi * g) * np.sin(xs / (W / 19.0) - t * 3.4)
         + a * .34 * min(1.0, hi * g) * np.sin(xs / (W / 33.0) + t * 5.2))

    ep = 0.8 + trait * 3.4
    d = np.abs(_yy - y[None, :])
    m = np.clip(1.0 - d / ep, 0.0, 1.0)
    halo = np.clip(1.0 - d / (ep * 4.5), 0.0, 1.0) * 0.32
    PIX[:] = pal(np.clip(m * 0.92 + halo, 0.0, 1.0) * 0.94 + 0.04)


ANIMS = {
    "orb": orb,
    "plasma": plasma, "truchet": truchet, "moire": moire, "rings": rings,
    "stripes": stripes, "glitch": glitch, "flow": flow, "life": life,
    "checkers": checkers, "spiral": spiral, "star": star,
    "kaleido": kaleido, "orbit": orbit,
    "sweep": sweep, "bars": bars, "static": static, "lines": lines,
    "sine": sine, "lissajous": lissajous, "hilbert": hilbert, "rd": rd,
    "ekg": ekg, "mire": mire, "timecode": timecode, "clock": clock,
    "clock2": clock2, "text": text, "photo": photo, "weather": weather,
    "mirror": cam_mirror, "silhouette": cam_silhouette, "trace": cam_trace,
    "spectre": snd_spectre, "pulse": snd_pulse, "onde": snd_onde,
    "aujourdhui": aujourdhui,
}


# ---------------------------------------------------------------- etat
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.on = True
        self.paused = False
        self.anim = "plasma"
        self.palette = "airdraw"
        self.bri = 178
        self.params = {}
        self.ip = ""
        self.fps = 0.0
        self.audio_react = False
        self.audio = 0.0          # niveau sonore courant, 0..1, envoye par le telephone
        self.shuffle = False      # mode aleatoire : change d'animation tout seul
        self.shuffle_sec = 30     # duree d'affichage de chaque animation, en secondes
        self.sound = False        # mode sonore : le micro du Pi module l'animation
        self.sound_sens = 0.6     # sensibilite, 0..1
        self.day = False          # mode journee : l'ambiance suit l'heure reelle
        self.mood_on = False      # mode ambiance : l'humeur suit le bruit de la piece
        self.mood = 0.0           # humeur interne 0..1 (calme -> eveille), derive lente
        self.sleep_at = ""        # extinction programmee "HH:MM" ("" = desactive)
        self.timer_off = 0.0      # minuteur : instant d'extinction (epoch), 0 = aucun

    def snapshot(self):
        with self.lock:
            return dict(on=self.on, paused=self.paused, anim=self.anim, palette=self.palette,
                        bri=self.bri, params=dict(self.params), ip=self.ip,
                        fps=round(self.fps, 1), audio_react=self.audio_react, audio=self.audio,
                        shuffle=self.shuffle, shuffle_sec=self.shuffle_sec,
                        sound=self.sound, sound_sens=self.sound_sens, day=self.day,
                        mood_on=self.mood_on, mood=round(self.mood, 3),
                        sleep_at=self.sleep_at, timer_off=self.timer_off,
                        sound_level=round(sensors.MIC.level, 3) if self.sound else 0.0,
                        cam=sensors.CAM.active,
                        cam_err=sensors.CAM.error, mic_err=sensors.MIC.error)

    def update(self, d):
        with self.lock:
            for k in ("on", "paused", "anim", "palette", "bri", "ip", "audio_react", "audio",
                      "shuffle", "shuffle_sec", "sound", "sound_sens", "day", "mood_on",
                      "sleep_at", "timer_off"):
                if k in d:
                    setattr(self, k, d[k])
            if "params" in d and isinstance(d["params"], dict):
                self.params = dict(d["params"])


STATE = State()


# animations retenues pour le mode aleatoire : on ecarte celles qui dependent
# d'une saisie exterieure (photo envoyee depuis le telephone, texte a lire).
SHUFFLE_POOL = [k for k in ANIMS if k not in (
    "photo", "text", "timecode", "mirror", "silhouette", "trace",
    "spectre", "pulse", "onde", "aujourdhui")]


def render_loop(fps=25):
    global _curpal
    period = 1.0 / fps
    t0 = last = time.time()
    frames, fps_t = 0, t0
    prev_anim = None
    next_switch = 0.0
    t_anim = 0.0          # temps de l'animation, accelere par le son
    _last_sleep_stamp = [""]   # derniere extinction programmee honoree

    while True:
      try:
        now = time.time()
        dt = now - last
        last = now
        s = STATE.snapshot()

        if not s["on"] or not s["ip"]:
            time.sleep(.25)
            continue

        # mode aleatoire : on tire une nouvelle animation et une nouvelle palette
        if s["shuffle"]:
            if now >= next_switch:
                pick = random.choice(SHUFFLE_POOL)
                pal_pick = random.choice(list(PALETTES.keys()))
                STATE.update({"anim": pick, "palette": pal_pick, "params": {}})
                s = STATE.snapshot()
                next_switch = now + max(5, int(s["shuffle_sec"]))
        # mode journee : l'ambiance suit l'heure, on change d'animation par palier
        elif s["day"]:
            if now >= next_switch:
                anims, pal_pick, bri = day_scene(time.localtime())
                pick = random.choice(anims)
                STATE.update({"anim": pick, "palette": pal_pick, "bri": bri, "params": {}})
                s = STATE.snapshot()
                next_switch = now + 90    # une animation nouvelle toutes les 90 s
        else:
            next_switch = 0.0

        # ─ VEILLE cote Pi : marche meme telephone range ─
        # minuteur : eteindre a l'echeance
        if s["timer_off"] and now >= s["timer_off"]:
            STATE.update({"on": False, "timer_off": 0.0})
            s = STATE.snapshot()
        # extinction a heure fixe : des que l'horloge atteint ou depasse HH:MM
        # dans la meme minute. On garde en memoire la derniere extinction pour
        # ne pas re-eteindre en boucle si l'utilisateur rallume manuellement.
        if s["sleep_at"] and s["on"]:
            try:
                want_h, want_m = (int(x) for x in s["sleep_at"].split(":"))
                lt = time.localtime()
                now_min = lt.tm_hour * 60 + lt.tm_min
                want_min = want_h * 60 + want_m
                stamp = time.strftime("%Y-%m-%d ") + s["sleep_at"]
                # on eteint si on est dans la minute cible (ou juste apres, <2 min)
                # et qu'on ne l'a pas deja fait pour cette echeance aujourd'hui
                if 0 <= (now_min - want_min) < 2 and _last_sleep_stamp[0] != stamp:
                    _last_sleep_stamp[0] = stamp
                    STATE.update({"on": False})
                    s = STATE.snapshot()
            except (ValueError, AttributeError):
                pass

        _curpal = PALETTES.get(s["palette"], PALETTES["mono"])
        if s["anim"] != prev_anim:
            clear()
            prev_anim = s["anim"]

        # mode sonore : le micro du Pi accelere et anime le rendu au rythme du son.
        snd = 0.0
        if s["sound"]:
            try:
                lvl, lo, mi, hi = sensors.MIC.read()
                snd = lvl * (0.2 + s["sound_sens"] * 1.6)
            except Exception:
                snd = 0.0

        # mode ambiance : une HUMEUR interne suit le bruit de la piece et derive
        # lentement. Beaucoup de vie -> l'humeur monte vers l'eveil ; du calme ->
        # elle glisse vers le repos. Elle colore la vitesse et la luminosite,
        # sans jamais sauter : une atmosphere qui respire sur la duree.
        mood_bri = 1.0
        mood_speed = 1.0
        if s["mood_on"]:
            try:
                lvl, lo, mi, hi = sensors.MIC.read()
                # cible d'humeur = niveau sonore adouci ; monte vite, descend lentement
                target = min(1.0, lvl * 2.2)
                m = STATE.mood
                rate = 0.9 if target > m else 0.12   # montee douce, descente plus lente
                m += (target - m) * min(1.0, rate * dt)
                STATE.mood = m
                # l'humeur infléchit vitesse (x0.7 calme -> x1.8 eveille) et
                # luminosite (0.55 calme -> 1.0 eveille), en douceur
                mood_speed = 0.7 + m * 1.1
                mood_bri = 0.55 + m * 0.45
            except Exception:
                mood_speed = 1.0
                mood_bri = 1.0

        if not s["paused"]:
            fn = ANIMS.get(s["anim"], plasma)
            t_anim += dt * (1.0 + snd * 2.2) * mood_speed
            try:
                fn(t_anim, s["params"], dt)
            except Exception:
                pass
            # la luminosite d'ambiance module le rendu apres coup
            if s["mood_on"] and mood_bri < 0.999:
                PIX *= mood_bri
        # en pause : on ne rappelle pas fn(), PIX garde la derniere image
        # calculee, et on continue quand meme a l'envoyer (le panneau reste
        # allume sur cette image au lieu de s'eteindre).

        out = PIX
        k = s["bri"] / 255.0
        if s["sound"]:
            k *= 0.72 + 0.28 * min(1.0, snd)
        if s["audio_react"]:
            # pulse entre 30% et 100% du niveau regle, au rythme du son recu
            k *= (0.30 + 0.70 * max(0.0, min(1.0, s["audio"])))
        if k < .999:
            out = PIX * k
        ddp_send(s["ip"], np.clip(out, 0, 255).astype(np.uint8).tobytes())

        frames += 1
        if now - fps_t >= 1.0:
            with STATE.lock:
                STATE.fps = frames / (now - fps_t)
            frames, fps_t = 0, now

        sl = period - (time.time() - now)
        if sl > 0:
            time.sleep(sl)
      except Exception:
        # une erreur imprevue ne doit JAMAIS tuer le rendu : on saute la frame
        time.sleep(.05)


def start():
    th = threading.Thread(target=render_loop, daemon=True)
    th.start()
    return th


if __name__ == "__main__":
    print("Benchmark (une image = 4096 pixels)")
    slow = []
    for name, fn in ANIMS.items():
        _curpal = PALETTES["airdraw"]
        clear()
        n, t0 = 30, time.time()
        for i in range(n):
            fn(i * .04, {}, .04)
        el = time.time() - t0
        f = n / el
        flag = "  <-- lent" if f < 60 else ""
        if f < 60:
            slow.append(name)
        print(f"  {name:10s} {f:7.0f} img/s   {el/n*1000:5.2f} ms{flag}")
    print()
    print("Sur un Pi 3 A+ (~20x plus lent), il faut > 500 img/s ici pour tenir 25 img/s.")
    print("Animations a surveiller :", slow if slow else "aucune")
