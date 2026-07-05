#!/usr/bin/env python3
"""
dashboard.py — Tableau de bord console MQTT/UPS pour Philoxenia
Zones : gauche (UPS L1/L2), droite-haut (Hardware : Alimentations + Températures),
droite-bas (GPIO), bas (flux MQTT).
Affichage sur écran physique via systemd (TTYPath=/dev/tty1).

Authentification Mosquitto :
  - identifiant dans MQTT_USER (par défaut "philoxenia")
  - mot de passe lu depuis la variable d'environnement MQTT_PASS
    (fournie par systemd via EnvironmentFile=/etc/philoxenia/dashboard.env)
"""

from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.console import Console, Group
from rich.text import Text
from rich.segment import Segment
from rich.style import Style
from rich import box
import paho.mqtt.client as mqtt
import threading
import time
import os
from datetime import datetime
from collections import deque

# ── Configuration ───────────────────────────────────────────────
MQTT_HOST = "raspi-vrrp.philoxenia"        # broker Mosquitto local sur le CM4
MQTT_PORT = 1883
MQTT_USER = os.environ.get("MQTT_USER", "philoxenia")
MQTT_PASS = os.environ.get("MQTT_PASS", "")   # fourni par EnvironmentFile
# Racine commune des topics de l'infrastructure (regroupe tout sous un domaine)
TOPIC_ROOT = "infrastructure"
MQTT_TOPICS = [
    (f"{TOPIC_ROOT}/ups/#", 0),    # adapte à tes vrais topics
    (f"{TOPIC_ROOT}/etat/#", 0),
    (f"{TOPIC_ROOT}/alim/#", 0),
    (f"{TOPIC_ROOT}/hw/#", 0),
]
REFRESH_HZ = 2                 # rafraîchissements par seconde

# ── Seuils d'alerte des alimentations (À CALIBRER) ──────────────
# La couleur est pilotée par la TENSION et s'applique aux deux valeurs
# (tension + puissance) de la ligne.
#   vert   : tension dans [vert_bas, vert_haut]        → nominal
#   jaune  : tension dans [jaune_bas, vert_bas[ ou ]vert_haut, jaune_haut]
#   rouge  : tension < jaune_bas ou > jaune_haut       → hors limites
#   FAULT  : tension == 0 (affiché en rouge, sans la puissance)
# Un seuil par type de rail ; le mapping se fait sur le préfixe du nom
# (ex. "48V-L1" → seuils "48V"). Ajuste ces valeurs lors de la calibration.
SEUILS_ALIM = {
    "48V": {"jaune_bas": 45.0, "vert_bas": 47.0, "vert_haut": 49.0, "jaune_haut": 51.0},
    "19V": {"jaune_bas": 17.5, "vert_bas": 18.5, "vert_haut": 19.5, "jaune_haut": 20.5},
}
# Seuil par défaut si le préfixe n'est pas reconnu (tolérance ±10 % autour du nominal)
SEUILS_ALIM_DEFAUT = {"jaune_bas": 0.1, "vert_bas": 0.2, "vert_haut": 999.0, "jaune_haut": 999.0}

# ── Seuils d'alerte des températures (À CALIBRER) ───────────────
#   vert  : t < temp_jaune
#   jaune : temp_jaune <= t < temp_rouge
#   rouge : t >= temp_rouge
TEMP_JAUNE = 60.0
TEMP_ROUGE = 70.0

# ── État partagé (rempli par MQTT, lu par l'affichage) ──────────
state = {
    "ups": {},                 # {"L1": {...}, "L2": {...}}
    "etat": {},                # état des devices : down/starting/up/failed
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
        # retire la racine commune "infrastructure/" si présente
        if parts and parts[0] == TOPIC_ROOT:
            parts = parts[1:]
        if not parts:
            events.append((datetime.now(), topic, payload))
            return
        racine = parts[0]
        if racine == "ups" and len(parts) >= 3:
            # ups/L1/charge -> state["ups"]["L1"]["charge"]
            unite = parts[1]                       # "L1" ou "L2"
            cle = "/".join(parts[2:])
            state["ups"].setdefault(unite, {})[cle] = payload
        elif racine == "alim" and len(parts) >= 3:
            # alim/48V-L1/voltage -> state["alim"]["48V-L1"]["voltage"]
            rail = parts[1]
            cle = "/".join(parts[2:])
            state["alim"].setdefault(rail, {})[cle] = payload
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

class BandeAlerte:
    """Aplat de couleur qui remplit toute la largeur et toute la hauteur
    disponibles (utilisé en bas du panneau Hardware pour signaler le niveau
    d'alerte global). La hauteur s'adapte à l'espace restant."""
    def __init__(self, couleur: str):
        self.couleur = couleur

    def __rich_console__(self, console, options):
        largeur = options.max_width
        hauteur = options.height or 1
        style = Style(color=self.couleur, bold=True)
        ligne = "█" * largeur
        for _ in range(hauteur):
            yield Segment(ligne, style)
            yield Segment.line()

def couleur_tension(rail: str, tension: float) -> str:
    """Retourne le style Rich selon la tension et les seuils du rail."""
    # sélection des seuils : préfixe avant le tiret (48V-L1 -> 48V)
    prefixe = rail.split("-")[0]
    s = SEUILS_ALIM.get(prefixe, SEUILS_ALIM_DEFAUT)
    if s["vert_bas"] <= tension <= s["vert_haut"]:
        return "green"
    if s["jaune_bas"] <= tension <= s["jaune_haut"]:
        return "bold yellow"
    return "bold red"

def niveau_alerte_global() -> str:
    """Agrège le pire niveau (vert/jaune/rouge) sur les rubriques
    Alimentations, Températures et État. rouge > jaune > vert."""
    rang = {"green": 0, "yellow": 1, "red": 2}
    pire = 0  # vert par défaut

    with lock:
        alim = {k: dict(v) for k, v in state["alim"].items() if isinstance(v, dict)}
        hw = dict(state["hw"])
        etat = dict(state["etat"])

    # ── Alimentations : niveau piloté par la tension ──
    for rail, donnees in alim.items():
        try:
            v = float(donnees.get("voltage", ""))
        except (ValueError, TypeError):
            continue
        if v == 0:                      # FAULT
            pire = max(pire, rang["red"])
            continue
        st = couleur_tension(rail, v)   # "green" / "bold yellow" / "bold red"
        if "red" in st:
            pire = max(pire, rang["red"])
        elif "yellow" in st:
            pire = max(pire, rang["yellow"])

    # ── Températures ──
    for val in hw.values():
        try:
            t = float(val)
        except (ValueError, TypeError):
            continue
        if t >= TEMP_ROUGE:
            pire = max(pire, rang["red"])
        elif t >= TEMP_JAUNE:
            pire = max(pire, rang["yellow"])

    # ── État des devices ──
    for val in etat.values():
        v = val.lower()
        if v in ("down", "failed"):
            pire = max(pire, rang["red"])
        elif v == "starting":
            pire = max(pire, rang["yellow"])

    return {0: "green", 1: "yellow", 2: "red"}[pire]

def panel_hw() -> Panel:
    with lock:
        alim = {k: dict(v) for k, v in state["alim"].items() if isinstance(v, dict)}
        hw = dict(state["hw"])
    corps = Text()

    # ── Sous-menu Alimentations ──
    corps.append("── Alimentations ──\n", style="bold yellow")
    # ordre d'affichage fixe des quatre rails
    rails = ["19V-L1", "19V-L2", "48V-L1", "48V-L2"]
    puissance_totale = 0.0
    if alim:
        for rail in rails:
            donnees = alim.get(rail, {})
            corps.append(f"{rail:<12}", style="cyan")
            # tension
            try:
                v = float(donnees.get("voltage", ""))
            except (ValueError, TypeError):
                v = None
            if v is None:
                corps.append("—\n", style="dim")
                continue
            if v == 0:
                # défaut : "--- FAULT ---" centré sous les deux colonnes
                # (zone tension+puissance = 16 caractères), rouge, sans puissance
                corps.append(f"{'--- FAULT ---':^17}\n", style="bold red")
                continue
            style = couleur_tension(rail, v)
            # puissance
            try:
                p = float(donnees.get("power", ""))
            except (ValueError, TypeError):
                p = None
            corps.append(f"{v:>6.1f}V  ", style=style)
            if p is not None:
                corps.append(f"{p:>6.1f}W\n", style=style)
                puissance_totale += p
            else:
                corps.append("\n")
        # ligne vide + puissance totale
        # Alignement : la puissance des alims commence à la colonne 21
        # (12 pour le rail + 7 pour "xxxx.xV" + 2 espaces). On reproduit
        # ce gabarit pour que le "W" du total tombe sous ceux des alims.
        corps.append("\n")
        label = "Puissance totale"
        pad = 21 - len(label)                 # espaces pour atteindre la colonne 21
        corps.append(label, style="bold cyan")
        corps.append(f"{' ' * pad}{puissance_totale:>6.1f}W\n", style="bold white")
    else:
        corps.append("en attente…\n", style="dim")

    # ── Sous-menu Températures ──
    corps.append("\n── Températures ──\n", style="bold yellow")
    if hw:
        for cle, val in sorted(hw.items()):
            # coloration selon le seuil de température
            try:
                t = float(val)
                if t >= TEMP_ROUGE:
                    style = "bold red"
                elif t >= TEMP_JAUNE:
                    style = "bold yellow"
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

    # ── Bande d'alerte globale (Alim + Temp + État) ──
    # Aplat de couleur qui remplit tout l'espace libre sous les températures :
    # rouge si erreur active, jaune si warning, vert sinon. rouge > jaune > vert.
    niveau = niveau_alerte_global()
    couleur = {"green": "green", "yellow": "yellow", "red": "red"}[niveau]

    # compte des lignes du contenu pour dimensionner la zone haute
    nb_lignes = len(corps.plain.rstrip("\n").split("\n"))
    interne = Layout()
    interne.split_column(
        Layout(corps, name="contenu", size=nb_lignes + 1),
        Layout(BandeAlerte(couleur), name="bande"),
    )

    return Panel(interne, title="[bold]Hardware[/bold]",
                 box=box.DOUBLE, border_style="magenta",
                 style="on dark_red")

def panel_etat() -> Panel:
    # devices attendus, dans l'ordre d'affichage
    devices = ["pve-domo-master", "pve-domo-slave", "raspi-master"]
    # couleur selon l'état
    couleurs = {
        "up":       "bold green",
        "starting": "bold yellow",
        "down":     "bold red",
        "failed":   "bold red",
    }
    with lock:
        etat = dict(state["etat"])
    corps = Text()
    corps.append("── État ──\n", style="bold yellow")
    for dev in devices:
        val = etat.get(dev, "—")
        style = couleurs.get(val.lower(), "dim" if val == "—" else "white")
        corps.append(f"{dev:<20}", style="cyan")
        corps.append(f"{val}\n", style=style)
    return Panel(corps, title="[bold]État[/bold]",
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
    layout["gauche_bas"].update(panel_etat())
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
