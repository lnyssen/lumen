# LUMEN

Panneau lumineux 64×64 piloté par un Raspberry Pi. Le Pi génère les animations
en continu et les envoie à WLED en DDP ; le téléphone n'est qu'une télécommande
web — on peut le fermer, le panneau continue de tourner.

## Architecture

```
┌──────────────┐   HTTP    ┌─────────────────────────┐   DDP/UDP   ┌────────────┐
│  téléphone   │ ────────► │  Raspberry Pi           │ ──────────► │   WLED     │
│  (index.html)│           │  server.py + engine.py  │   port 4048 │  64×64 LED │
└──────────────┘           └─────────────────────────┘             └────────────┘
                                   ▲        ▲
                            micro ─┘        └─ caméra  (sensors.py)
```

- **`engine.py`** — le moteur. Un buffer RGB persistant de 4096 pixels, rempli ou
  atténué par les animations (fade) pour les effets de rémanence. Tout est
  vectorisé numpy : une boucle pixel par pixel en Python plafonnerait à 2–3
  images/seconde sur un Pi 3 A+.
- **`server.py`** — serveur HTTP (port 8000) : sert l'app, expose l'API d'état,
  proxy WLED, et page de mise à jour depuis le téléphone.
- **`sensors.py`** — caméra (`v4l2-ctl`) et micro (`arecord`), démarrés à la
  demande et arrêtés seuls quand plus personne ne lit — pour libérer l'USB et
  éteindre le témoin de la caméra.
- **`alexa.py`** — commande vocale sans cloud ni compte à lier : émulation de
  prises Belkin WeMo que l'Echo découvre seul sur le réseau local.
- **`index.html`** — l'app (PWA autonome, aucune dépendance).
- **`palettes.json`** — les palettes de couleurs.

## Animations

36 animations réparties en familles :

| Famille | Animations |
|---|---|
| Génératif | `plasma` `truchet` `moire` `rings` `stripes` `glitch` `flow` `life` `checkers` `spiral` `star` `kaleido` `orbit` `orb` |
| Géométrique | `sweep` `bars` `static` `lines` `sine` `lissajous` `hilbert` `rd` `ekg` `mire` |
| Information | `clock` `clock2` `timecode` `text` `photo` `weather` `aujourdhui` |
| Caméra | `mirror` `silhouette` `trace` |
| Son | `spectre` `pulse` `onde` |

Modes transverses : **journée** (l'ambiance suit l'heure réelle), **aléatoire**
(change d'animation tout seul), **sonore** (le micro module l'animation),
**ambiance** (l'humeur dérive avec le bruit de la pièce).

## Matériel

- Raspberry Pi 3 A+ (512 Mo suffisent) ou mieux
- Panneau LED 64×64 sous [WLED](https://kno.wled.ge/), atteignable en DDP
- Optionnel : webcam USB (testé sur Logitech C270) pour les animations caméra
- Optionnel : micro USB pour le mode sonore
- Optionnel : Amazon Echo (2ᵉ/3ᵉ gén.) pour la commande vocale

## Installation

Sur le Pi, dans `/home/<utilisateur>/lumen` :

```bash
git clone https://github.com/<compte>/lumen.git ~/lumen
cd ~/lumen
./setup-service.sh
```

Le script installe numpy si besoin, crée deux services systemd (`lumen` et
`lumen-alexa`) et les démarre.

> **Note** — `setup-service.sh` suppose l'utilisateur `laurent` et le chemin
> `/home/laurent/lumen`. Adapte les deux si ton compte diffère.

L'app est ensuite sur `http://<ip-du-pi>:8000`. Au premier lancement, ouvre le
menu « Adresse du panneau » et saisis l'adresse WLED — une IP, ou un nom réseau
type `wled-xxxxxx.local`. Le nom survit aux changements de réseau : préférable
si l'objet se déplace.

## API

| Méthode | Route | Effet |
|---|---|---|
| `GET` | `/api/state` | état courant (anim, palette, params, bri, on, fps) |
| `POST` | `/api/state` | modifie l'état |
| `POST` | `/api/audio` | niveau sonore courant `{ level: 0..1 }` (haute fréquence, non persisté) |
| `POST` | `/api/image` | envoie une photo (64×64×3 octets RGB bruts), bascule sur `anim=photo` |
| `GET` | `/wled/info?ip=…` | proxy WLED (test de connexion) |

## Commande vocale

Une fois `lumen-alexa` lancé, dis à l'Echo « découvre les appareils ». Les
prises virtuelles apparaissent :

- « Alexa, allume Lumen » / « éteins Lumen »
- « Alexa, allume Journée » / « Aléatoire » / « Musique » / « Plasma »

Entièrement local, aucune dépendance hors bibliothèque standard Python.

## Licence

MIT — voir [LICENSE](LICENSE).
