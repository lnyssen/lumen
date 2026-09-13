#!/usr/bin/env python3
"""Commande vocale de LUMEN par Alexa, sans cloud ni compte a lier.

On emule des prises Belkin WeMo : l'Echo les decouvre seul sur le reseau
local et chaque prise declenche une action sur LUMEN via son API HTTP.

  « Alexa, allume Lumen »       -> panneau allume
  « Alexa, eteins Lumen »       -> panneau eteint
  « Alexa, allume Journee »     -> mode journee
  « Alexa, allume Aleatoire »   -> mode aleatoire
  « Alexa, allume Musique »     -> mode sonore
  « Alexa, allume Plasma »      -> animation plasma

Entierement local, aucune dependance a installer : uniquement la librairie
standard de Python. Emulation WeMo maison, testee sur Echo de 2e/3e gen ;
certains modeles recents demandent d'insister sur « decouvre les appareils ».
"""

import http.server
import json
import socket
import struct
import threading
import urllib.request
import uuid

LOCAL_API = "http://127.0.0.1:8000/api/state"

# ── chaque prise : nom prononce, action a l'allumage, action a l'extinction ──
DEVICES = [
    ("Lumen",     {"on": True},
                  {"on": False}),
    ("Journee",   {"on": True, "day": True, "shuffle": False, "sound": False},
                  {"day": False}),
    ("Aleatoire", {"on": True, "shuffle": True, "day": False, "sound": False},
                  {"shuffle": False}),
    ("Musique",   {"on": True, "sound": True, "day": False, "shuffle": False},
                  {"sound": False}),
    ("Plasma",    {"on": True, "anim": "plasma", "shuffle": False, "day": False},
                  {"on": False}),
]

BASE_PORT = 52000


def _post(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(LOCAL_API, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
        return True
    except Exception:
        return False


def _is_on():
    try:
        with urllib.request.urlopen(LOCAL_API, timeout=2) as r:
            return bool(json.loads(r.read()).get("on"))
    except Exception:
        return False


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ── serveur HTTP par prise : repond comme une WeMo a l'Echo ──────────────
class _Device:
    def __init__(self, name, on_act, off_act, port, ip):
        self.name = name
        self.on_act = on_act
        self.off_act = off_act
        self.port = port
        self.ip = ip
        self.uuid = "Socket-1_0-" + str(uuid.uuid4())[:12]
        self.serial = self.uuid


def _setup_xml(dev):
    return ('<?xml version="1.0"?>'
            '<root xmlns="urn:Belkin:device-1-0"><device>'
            '<deviceType>urn:Belkin:device:controllee:1</deviceType>'
            f'<friendlyName>{dev.name}</friendlyName>'
            '<manufacturer>Belkin International Inc.</manufacturer>'
            '<modelName>Emulated Socket</modelName><modelNumber>3.1415</modelNumber>'
            f'<UDN>uuid:{dev.uuid}</UDN><serialNumber>{dev.serial}</serialNumber>'
            '<serviceList><service>'
            '<serviceType>urn:Belkin:service:basicevent:1</serviceType>'
            '<serviceId>urn:Belkin:serviceId:basicevent1</serviceId>'
            '<controlURL>/upnp/control/basicevent1</controlURL>'
            '<eventSubURL>/upnp/event/basicevent1</eventSubURL>'
            '<SCPDURL>/eventservice.xml</SCPDURL>'
            '</service></serviceList></device></root>')


def _make_handler(dev):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="text/xml"):
            b = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.endswith("setup.xml"):
                self._send(_setup_xml(dev))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode(errors="ignore")
            if "GetBinaryState" in body:
                state = 1 if _is_on() else 0
                self._send(
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                    '<s:Body><u:GetBinaryStateResponse xmlns:u="urn:Belkin:service:basicevent:1">'
                    f'<BinaryState>{state}</BinaryState>'
                    '</u:GetBinaryStateResponse></s:Body></s:Envelope>')
                return
            if "SetBinaryState" in body:
                # <BinaryState>1</BinaryState> = allumer, 0 = eteindre
                turn_on = "<BinaryState>1" in body
                _post(dev.on_act if turn_on else dev.off_act)
                self._send(
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                    '<s:Body><u:SetBinaryStateResponse xmlns:u="urn:Belkin:service:basicevent:1">'
                    '<BinaryState>' + ("1" if turn_on else "0") + '</BinaryState>'
                    '</u:SetBinaryStateResponse></s:Body></s:Envelope>')
                return
            self.send_response(200)
            self.end_headers()

    return H


# ── SSDP : c'est ce qui rend les prises decouvrables par l'Echo ──────────
def _ssdp_loop(devices, ip):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 1900))
    mreq = struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except Exception:
            continue
        msg = data.decode(errors="ignore")
        if "M-SEARCH" not in msg or "ssdp:discover" not in msg:
            continue
        # une reponse par prise, chacune sur son port
        for dev in devices:
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=86400\r\n"
                "EXT:\r\n"
                f"LOCATION: http://{ip}:{dev.port}/setup.xml\r\n"
                "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                f"ST: urn:Belkin:device:**\r\n"
                f"USN: uuid:{dev.uuid}::urn:Belkin:device:**\r\n\r\n")
            try:
                sock.sendto(resp.encode(), addr)
            except Exception:
                pass


def main():
    ip = _local_ip()
    devices = []
    for i, (name, on_act, off_act) in enumerate(DEVICES):
        dev = _Device(name, on_act, off_act, BASE_PORT + i, ip)
        devices.append(dev)
        srv = http.server.ThreadingHTTPServer(("", dev.port), _make_handler(dev))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"prise virtuelle « {name} » sur {ip}:{dev.port}")

    threading.Thread(target=_ssdp_loop, args=(devices, ip), daemon=True).start()
    print("Alexa : prises pretes. Dis « Alexa, decouvre les appareils ».")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
