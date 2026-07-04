#!/usr/bin/env python3
"""
dashboard.py — Tableau de bord console MQTT/UPS pour Philoxenia
Zones : gauche (UPS L1/L2), droite-haut (Hardware : Alimentations + Températures),
droite-bas (GPIO), bas (flux MQTT).
Affichage sur écran physique via systemd (TTYPath=/dev/tty1).

Authentification Mosquitto :
  - identifiant dans MQTT_USER (par défaut "dashboard")
  - mot de passe lu depuis la variable d'environnement MQTT_PASS
    (fournie par systemd via EnvironmentFile=/etc/philoxenia/dashboard.env)
"""

from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.console import Console
from rich.text import Text
from rich import box
import paho.mqtt.client as mqtt
import threading
import time
import os
from datetime import datetime
from collections import deque

# ── Configuration ───────────────────────────────────────────────
MQTT_HOST = "localhost"        # broker Mosquitto local sur le CM4
MQTT_PORT = 1883
MQTT_USER = os.environ.get("MQTT_USER", "philoxenia")
MQTT_PASS = os.environ.get("MQTT_PASS", "")   # fourni par EnvironmentFile
MQTT_TOPICS = [
    ("ups/#", 0),              # adapte à tes vrais topics
    ("gpio/#", 0),
    ("alim/#", 0),
    ("hw/#", 0),
]
REFRESH_HZ = 2                 # rafraîchissements par seconde

# ── État partagé (rempli par MQTT, lu par l'affichage) ──────────
state = {
    "ups": {},                 # {"L1": {...}, "L2": {...}}
    "gpio": {},
    "alim": {},
    "hw": {},
}
events = deque(maxlen=50)      # derniers messages pour le panneau du bas
lock = threading.Lock()

# ── Callbacks MQTT (tournent dans le thread paho) ───────────────
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        client.subscribe(MQTT_TOPICS)
        with lock:
            events.append((datetime.now(), "MQTT", "connecté au broker"))
    else:
        with lock:
            events.append((datetime.now(), "MQTT", f"échec connexion rc={rc}"))

def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8", "replace")
    topic = msg.topic
    with lock:
        parts = topic.split("/")
        racine = parts[0]
        if racine == "ups" and len(parts) >= 3:
            # ups/L1/charge -> state["ups"]["L1"]["charge"]
            unite = parts[1]                       # "L1" ou "L2"
            cle = "/".join(parts[2:])
            state["ups"].setdefault(unite, {})[cle] = payload
        elif racine in state:
            sous_cle = "/".join(parts[1:]) or topic
            state[racine][sous_cle] = payload
        events.append((datetime.now(), topic, payload))

# ── Construction des panneaux ───────────────────────────────────
def panel_ups() -> Panel:
    with lock:
        ups = {k: dict(v) for k, v in state["ups"].items() if isinstance(v, dict)}

    def section(corps, titre, donnees):
        corps.append(f"── {titre} ──\n", style="bold yellow")
        if donnees:
            for cle, val in sorted(donnees.items()):
                style = "white"
                if cle.lower().endswith("status"):
                    style = "green" if "OL" in val else "bold red"
                corps.append(f"{cle:<18}", style="cyan")
                corps.append(f"{val}\n", style=style)
        else:
            corps.append("en attente…\n", style="dim")

    corps = Text()
    section(corps, "UPS L1", ups.get("L1", {}))
    corps.append("\n")
    section(corps, "UPS L2", ups.get("L2", {}))

    return Panel(corps, title="[bold]UPS / NUT[/bold]",
                 box=box.DOUBLE, border_style="green",
                 style="on dark_red")

def panel_hw() -> Panel:
    with lock:
        alim = dict(state["alim"])
        hw = dict(state["hw"])
    corps = Text()

    # ── Sous-menu Alimentations ──
    corps.append("── Alimentations ──\n", style="bold yellow")
    if alim:
        for cle, val in sorted(alim.items()):
            style = "green" if val.upper() == "OK" else "bold red"
            corps.append(f"{cle:<20}", style="cyan")
            corps.append(f"{val}\n", style=style)
    else:
        corps.append("en attente…\n", style="dim")

    # ── Sous-menu Températures ──
    corps.append("\n── Températures ──\n", style="bold yellow")
    if hw:
        for cle, val in sorted(hw.items()):
            # coloration selon le seuil de température
            try:
                t = float(val)
                if t >= 70:
                    style = "bold red"
                elif t >= 60:
                    style = "yellow"
                else:
                    style = "green"
                affichage = f"{t:.1f}°C"
            except ValueError:
                style = "white"
                affichage = val
            corps.append(f"{cle:<22}", style="cyan")
            corps.append(f"{affichage}\n", style=style)
    else:
        corps.append("en attente…\n", style="dim")

    return Panel(corps, title="[bold]Hardware[/bold]",
                 box=box.DOUBLE, border_style="magenta",
                 style="on dark_red")

def panel_gpio() -> Panel:
    with lock:
        gpio = dict(state["gpio"])
    corps = Text()
    corps.append("── GPIO ──\n", style="bold yellow")
    if gpio:
        for cle, val in sorted(gpio.items()):
            corps.append(f"{cle:<20}", style="cyan")
            corps.append(f"{val}\n")
    else:
        corps.append("en attente…\n", style="dim")
    return Panel(corps, title="[bold]GPIO[/bold]",
                 box=box.DOUBLE, border_style="yellow",
                 style="on dark_red")

def panel_flux() -> Panel:
    with lock:
        derniers = list(events)[-20:]   # ajuste selon la hauteur dispo
    corps = Text()
    for ts, topic, payload in derniers:
        corps.append(f"{ts:%H:%M:%S} ", style="dim")
        corps.append(f"{topic} ", style="cyan")
        corps.append(f"{payload}\n", style="white")
    if not derniers:
        corps = Text("aucun message reçu", style="dim")
    return Panel(corps, title="[bold]Flux MQTT[/bold]",
                 box=box.DOUBLE, border_style="blue",
                 style="on dark_red")

# ── Disposition (Layout) ────────────────────────────────────────
def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="haut", ratio=80),
        Layout(name="bas", ratio=20),
    )
    layout["haut"].split_row(
        Layout(name="gauche"),
        Layout(name="droite"),
    )
    # colonne gauche : UPS en haut (2/3), GPIO en bas (1/3)
    layout["gauche"].split_column(
        Layout(name="gauche_haut", ratio=2),
        Layout(name="gauche_bas", ratio=1),
    )
    # colonne droite : Hardware sur toute la hauteur
    return layout

def update_layout(layout: Layout):
    layout["gauche_haut"].update(panel_ups())
    layout["gauche_bas"].update(panel_gpio())
    layout["droite"].update(panel_hw())
    layout["bas"].update(panel_flux())

# ── Programme principal ─────────────────────────────────────────
def main():
    # Client MQTT dans son propre thread (boucle réseau non bloquante)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.username_pw_set(MQTT_USER, MQTT_PASS)   # authentification
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        with lock:
            events.append((datetime.now(), "MQTT", f"connexion impossible : {e}"))
    client.loop_start()        # thread réseau géré par paho

    console = Console(force_terminal=True)
    layout = build_layout()

    try:
        with Live(layout, console=console, screen=True,
                  refresh_per_second=REFRESH_HZ) as live:
            while True:
                update_layout(layout)
                time.sleep(1 / REFRESH_HZ)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
