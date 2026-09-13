#!/bin/bash
set -e

echo "Installation de LUMEN..."

# numpy est indispensable au moteur d'animations
if ! python3 -c "import numpy" 2>/dev/null; then
  echo "  installation de numpy (une seule fois, ~1 min)..."
  sudo apt-get update -qq
  sudo apt-get install -y python3-numpy
fi

sudo tee /etc/systemd/system/lumen.service >/dev/null <<'UNIT'
[Unit]
Description=LUMEN — moteur d'animations et serveur
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/laurent/lumen
ExecStart=/usr/bin/python3 /home/laurent/lumen/server.py
Restart=always
RestartSec=3
User=laurent

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable lumen >/dev/null 2>&1
sudo systemctl restart lumen
sleep 2

# permet le redemarrage du service sans mot de passe (pour le bouton "Mettre a jour" dans l'app)
sudo tee /etc/sudoers.d/lumen-update >/dev/null <<'SUDOERS'
laurent ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart lumen, /bin/systemctl restart lumen
SUDOERS
sudo chmod 440 /etc/sudoers.d/lumen-update

if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ | grep -q 200; then
  echo "OK — LUMEN tourne."
  echo "   Le Pi genere les animations : le panneau continue meme app fermee."
else
  echo "ATTENTION — verifie : sudo systemctl status lumen"
fi

# ─ service Alexa : prises virtuelles decouvrables par l'Echo (optionnel) ─
sudo tee /etc/systemd/system/lumen-alexa.service >/dev/null <<'UNIT'
[Unit]
Description=LUMEN — commande vocale Alexa (emulation WeMo locale)
After=lumen.service
Wants=lumen.service

[Service]
WorkingDirectory=/home/laurent/lumen
ExecStart=/usr/bin/python3 /home/laurent/lumen/alexa.py
Restart=always
RestartSec=5
User=laurent

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable lumen-alexa >/dev/null 2>&1
sudo systemctl restart lumen-alexa
echo "  Alexa : prises pretes. Dis a l'Echo « decouvre les appareils »."
