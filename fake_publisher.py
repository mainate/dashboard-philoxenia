#!/usr/bin/env python3
"""
fake_publisher.py — Publie de faux messages MQTT pour tester dashboard.py
Simule deux UPS (L1/L2), des GPIO, quatre alimentations et les températures.

Authentification Mosquitto :
  - identifiant dans MQTT_USER (par défaut "philoxenia")
  - mot de passe lu depuis la variable d'environnement MQTT_PASS

Lancement (dans le venv, en fournissant le mot de passe) :
  MQTT_PASS='ton_mot_de_passe' ~/dashboard-venv/bin/python ~/fake_publisher.py
"""

import paho.mqtt.client as mqtt
import time
import random
import os

MQTT_HOST = "raspi-vrrp.philoxenia"
MQTT_PORT = 1883
MQTT_USER = os.environ.get("MQTT_USER", "philoxenia")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
# Racine commune des topics (doit correspondre à TOPIC_ROOT du dashboard)
TOPIC_ROOT = "infrastructure"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set(MQTT_USER, MQTT_PASS)   # authentification
client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
client.loop_start()

def pub(topic, payload):
    """Publie en préfixant automatiquement par la racine commune."""
    client.publish(f"{TOPIC_ROOT}/{topic}", payload)

print("Publication de faux messages — Ctrl+C pour arrêter")

etats = {
    "L1": {"charge": 100, "ob": False},
    "L2": {"charge": 100, "ob": False},
}
compteur = 0

try:
    while True:
        compteur += 1

        # ── UPS L1 (Back-UPS CS350) et L2 (RM900)
        for unite in ("L1", "L2"):
            if random.random() < 0.05:
                etats[unite]["ob"] = not etats[unite]["ob"]
            if etats[unite]["ob"]:
                etats[unite]["charge"] = max(0, etats[unite]["charge"] - random.randint(1, 3))
                status = "OB"
            else:
                etats[unite]["charge"] = min(100, etats[unite]["charge"] + random.randint(0, 2))
                status = "OL"
            c = etats[unite]["charge"]
            pub(f"ups/{unite}/charge", str(c))
            pub(f"ups/{unite}/runtime", str(c * 30))
            pub(f"ups/{unite}/status", status)
            pub(f"ups/{unite}/voltage", str(random.randint(228, 235)))

        # ── État des devices : down / starting / up / failed
        for dev in ("pve-domo-master", "pve-domo-slave"):
            pub(f"etat/{dev}",
                           random.choice(["down", "starting", "up", "up", "up", "failed"]))

        # ── Alimentations : tension + puissance par rail
        #    (tension à 0 = FAULT ; sinon surtout nominal, parfois hors plage)
        alims = {
            "19V-L1": 19.0, "19V-L2": 19.0,
            "48V-L1": 48.0, "48V-L2": 48.0,
        }
        for rail, nominal in alims.items():
            r = random.random()
            if r < 0.02:                          # 2% : panne (FAULT)
                pub(f"alim/{rail}/voltage", "0")
                pub(f"alim/{rail}/power", "0")
                continue
            elif r < 0.10:                        # 8% : excursion (jaune/rouge)
                v = round(nominal + random.uniform(-2.0, 2.0), 1)
            else:                                 # 90% : nominal (vert), faible bruit
                v = round(nominal + random.uniform(-0.3, 0.3), 1)
            p = round(random.uniform(12, 30), 1)
            pub(f"alim/{rail}/voltage", str(v))
            pub(f"alim/{rail}/power", str(p))

        # ── Température des équipements
        pub("hw/cpu_proxmox_master", str(round(random.uniform(45, 72), 1)))
        pub("hw/cpu_proxmox_slave",  str(round(random.uniform(45, 72), 1)))
        pub("hw/cpu_raspi_master",   str(round(random.uniform(38, 58), 1)))
        pub("hw/cpu_raspi_slave",    str(round(random.uniform(38, 58), 1)))
        pub("hw/cpu_router_master",   str(round(random.uniform(40, 65), 1)))
        pub("hw/cpu_router_slave",    str(round(random.uniform(40, 65), 1)))
        pub("hw/rack_temp",           str(round(random.uniform(22, 38), 1)))

        resume = " | ".join(
            f"{u}:{etats[u]['charge']}%{'⚡' if etats[u]['ob'] else '🔌'}"
            for u in ("L1", "L2")
        )
        print(f"[{compteur}] {resume}")

        time.sleep(2)

except KeyboardInterrupt:
    print("\nArrêt.")
finally:
    client.loop_stop()
    client.disconnect()
