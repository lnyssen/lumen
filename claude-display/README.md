# Afficheur Claude pour LUMEN

Petit écran ESP32 (Waveshare ESP32-S3-Touch-LCD-1.54, 240 × 240) qui montre les limites
d'usage de Claude : session de 5 h et quota hebdomadaire, avec les comptes à rebours.

```
 Pi (LUMEN) ── HTTPS ──► api.anthropic.com   (claude_usage.py, jeton sur le Pi)
     │
     └──── HTTP, clé partagée ──► ESP32      (claude-display.ino, aucun jeton)
```

Le Pi interroge l'API toutes les 5 min et renvoie les valeurs à l'écran toutes les 2 min.
Sans nouvelle pendant 15 min, l'écran l'indique (« donnees perimees »).

## Avertissements

- L'endpoint d'usage de Claude **n'est pas documenté officiellement** : il peut changer ou
  refuser un jeton sans préavis. Les erreurs du serveur sont visibles sur `GET /api/claude`.
- Le jeton OAuth donne accès à ton compte. Il reste sur le Pi (`~/.config/lumen/`, hors du
  dossier servi par `server.py`), jamais sur l'écran. Sa durée de validité n'est pas documentée
  et le renouvellement n'est pas géré : s'il expire, remets un jeton.
- Le firmware n'est **pas testé sur le matériel**. Le brochage vient du code officiel Waveshare
  de la carte tactile (DC 45, CS 21, SCK 38, MOSI 39, RST 40, rétroéclairage 46).
- La clé partagée voyage en clair en HTTP sur ton réseau local : acceptable chez soi.

## Installation

### 1. L'écran
Arduino IDE : paquet « esp32 by Espressif » 3.2.0, carte « ESP32S3 Dev Module », Flash 16 Mo,
bibliothèque `GFX_Library_for_Arduino` 1.6.0. Copie `claude-display/secrets.h.example` en
`secrets.h`, renseigne le Wi-Fi 2,4 GHz et une clé (`openssl rand -hex 16`), puis téléverse.
Le moniteur série affiche l'adresse IP de la carte.

### 2. Le Pi
```bash
mkdir -p ~/.config/lumen && chmod 700 ~/.config/lumen
cat > ~/.config/lumen/claude.json <<'JSON'
{ "device_host": "claudeusage.local", "device_key": "LA-MEME-CLE" }
JSON
chmod 600 ~/.config/lumen/claude.json
```
Si `claudeusage.local` ne se résout pas depuis le Pi, mets l'IP de la carte dans `device_host`.

### 3. Le jeton (depuis ton Mac, sans jamais l'afficher)
```bash
security find-generic-password -s "Claude Code-credentials" -w \
 | python3 -c 'import sys,json; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])' \
 | ssh <utilisateur>@<ip-du-pi> 'umask 077; mkdir -p ~/.config/lumen; cat > ~/.config/lumen/claude-token'
```
Cet élément du Trousseau contient aussi les jetons d'autres services connectés : on n'extrait
que `claudeAiOauth.accessToken`. Format observé sur un Mac réel, non documenté.

### 4. Vérifier
```bash
sudo systemctl restart lumen
curl http://<ip-du-pi>:8000/api/claude
```
`error` doit être `null` et `device` valoir `"ok"`.

## Configuration (`~/.config/lumen/claude.json`)

| Clé | Défaut | Rôle |
|---|---|---|
| `device_host` | `claudeusage.local` | nom ou IP de l'écran |
| `device_key` | (vide : envoi désactivé) | clé partagée avec `secrets.h` |
| `token_file` | `~/.config/lumen/claude-token` | fichier du jeton |
| `interval` | `300` | secondes entre deux appels à l'API (60 minimum) |
| `user_agent` | (aucun) | en-tête `User-Agent` optionnel |

## Dépannage

| `/api/claude` indique | Signification probable |
|---|---|
| `error`: `jeton absent` | étape 3 non faite ou mauvais chemin |
| `error`: `HTTP 403 : …` | l'API refuse ce jeton ; le message du serveur est affiché |
| `error`: `HTTP 401 : …` | jeton expiré ou invalide |
| `device`: `ecran injoignable…` | Wi-Fi, nom `.local` non résolu, ou carte éteinte |
| `device`: `l'ecran a repondu 403` | `device_key` différente de `DEVICE_KEY` |
