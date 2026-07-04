#!/usr/bin/env python3
"""
fake_publisher.py — Publie de faux messages MQTT pour tester dashboard.py
Simule deux UPS (L1/L2), des GPIO, quatre alimentations et les températures.

Authentification Mosquitto :
  - identifiant dans MQTT_USER (par défaut "dashboard")
  - mot de passe lu depuis la variable d'environnement MQTT_PASS

Lancement (dans le venv, en fournissant le mot de passe) :
  MQTT_PASS='ton_mot_de_passe' ~/dashboard-venv/bin/python ~/fake_publisher.py
"""

import paho.mqtt.client as mqtt
import time
import random
import os

MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_USER = os.environ.get("MQTT_USER", "philoxenia")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set(MQTT_USER, MQTT_PASS)   # authentification
client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
client.loop_start()

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
            client.publish(f"ups/{unite}/charge", str(c))
            client.publish(f"ups/{unite}/runtime", str(c * 30))
            client.publish(f"ups/{unite}/status", status)
            client.publish(f"ups/{unite}/voltage", str(random.randint(228, 235)))

        # ── GPIO : états on/off des entrées
        client.publish("gpio/bouton_21", random.choice(["0", "1"]))
        client.publish("gpio/led_16", random.choice(["ON", "OFF"]))
        client.publish("gpio/led_26", random.choice(["ON", "OFF"]))

        # ── Alimentations : quatre sources (19V et 48V, L1/L2)
        client.publish("alim/19V-L1", random.choice(["OK", "FAULT"]))
        client.publish("alim/19V-L2", "OK")
        client.publish("alim/48V-L1", random.choice(["OK", "FAULT"]))
        client.publish("alim/48V-L2", "OK")

        # ── Température des équipements
        client.publish("hw/cpu_proxmox_master", str(round(random.uniform(45, 72), 1)))
        client.publish("hw/cpu_proxmox_slave",  str(round(random.uniform(45, 72), 1)))
        client.publish("hw/cpu_pi_master",       str(round(random.uniform(38, 58), 1)))
        client.publish("hw/cpu_pi_slave",        str(round(random.uniform(38, 58), 1)))
        client.publish("hw/cpu_router_master",   str(round(random.uniform(40, 65), 1)))
        client.publish("hw/cpu_router_slave",    str(round(random.uniform(40, 65), 1)))
        client.publish("hw/rack",                str(round(random.uniform(22, 38), 1)))

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
