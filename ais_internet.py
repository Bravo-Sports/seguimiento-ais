#!/usr/bin/env python3
"""Barcos del AIS por INTERNET (aisstream.io) -> ~/gribs/app/ais_internet.json, que lee la app.

Solo lectura: no escribe en SignalK ni en ningun bus. El JSON va con la forma de
/signalk/v1/api/vessels para que la app lo lea con el mismo parser que el AIS de a bordo
(el de a bordo manda: los de internet solo rellenan los que no ve la antena).

Caja de +-RADIO_MILLAS alrededor del barco (posicion de SignalK); si el barco se aleja
RECENTRA_MILLAS del centro, se vuelve a suscribir. Sin GPS, la ultima posicion o MALAGA.
La clave en ~/.aisstream (chmod 600): NUNCA en el repositorio ni en las copias.

  python3 ais_internet.py          servicio (ais-internet.service)
  python3 ais_internet.py --prueba 30   escucha 30 s y dice cuantos barcos
"""
import asyncio
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.request

import websockets

CLAVE = os.path.expanduser("~/.aisstream")
SALIDA = os.path.expanduser("~/gribs/app/ais_internet.json")
URL = "wss://stream.aisstream.io/v0/stream"
SIGNALK = "http://127.0.0.1:3000/signalk/v1/api/vessels/self/navigation/position"
MALAGA = (36.70, -4.40)
RADIO_MILLAS = 100        # lo que alcanza el radar de la app; medido: ~30 barcos y 9 kB, trafico despreciable
RECENTRA_MILLAS = 10
MAX_EDAD_S = 600
CADA_S = 10
TIPOS = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
         "ShipStaticData", "StaticDataReport"]


def posicion_barco(ultima):
    try:
        d = json.load(urllib.request.urlopen(SIGNALK, timeout=5))
        return d["value"]["latitude"], d["value"]["longitude"]
    except Exception:
        return ultima


def caja(centro, millas=RADIO_MILLAS):
    """[[lat_min, lon_min], [lat_max, lon_max]] de +-millas alrededor de 'centro'."""
    dla = millas / 60.0
    dlo = dla / max(0.1, math.cos(math.radians(centro[0])))
    return [[centro[0] - dla, centro[1] - dlo], [centro[0] + dla, centro[1] + dlo]]


def millas(a, b):
    dla = (b[0] - a[0]) * 60
    dlo = (b[1] - a[1]) * 60 * math.cos(math.radians(a[0]))
    return math.hypot(dla, dlo)


def anota(barcos, msg, ahora):
    """Mete un mensaje de aisstream en {mmsi: datos}. Ignora lo que no entiende."""
    meta = msg.get("MetaData") or {}
    mmsi = str(meta.get("MMSI") or "").strip()
    if not mmsi:
        return
    b = barcos.setdefault(mmsi, {})
    nombre = (meta.get("ShipName") or "").strip()
    if nombre:
        b["nombre"] = nombre
    tipo = msg.get("MessageType")
    cuerpo = (msg.get("Message") or {}).get(tipo) or {}
    if tipo in ("ShipStaticData", "StaticDataReport"):
        n = (cuerpo.get("Name") or (cuerpo.get("ReportA") or {}).get("Name") or "").strip()
        if n:
            b["nombre"] = n
        return
    la, lo = meta.get("latitude"), meta.get("longitude")
    if la is None or lo is None or abs(la) > 90 or abs(lo) > 180:
        return
    b["lat"], b["lon"], b["t"] = la, lo, ahora
    cog, sog = cuerpo.get("Cog"), cuerpo.get("Sog")
    b["cog"] = cog if cog is not None and 0 <= cog < 360 else None      # 360 = no disponible
    b["sog"] = sog if sog is not None and 0 <= sog < 102.2 else None    # 102.3 = no disponible


def a_signalk(barcos, ahora):
    """{urn: vessel} como /signalk/v1/api/vessels, solo con posicion de menos de MAX_EDAD_S."""
    out = {}
    for mmsi, b in barcos.items():
        if "t" not in b or ahora - b["t"] > MAX_EDAD_S:
            continue
        ts = dt.datetime.fromtimestamp(b["t"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nav = {"position": {"value": {"latitude": b["lat"], "longitude": b["lon"]}, "timestamp": ts}}
        if b.get("cog") is not None:
            nav["courseOverGroundTrue"] = {"value": math.radians(b["cog"])}
        if b.get("sog") is not None:
            nav["speedOverGround"] = {"value": b["sog"] * 0.514444}
        v = {"mmsi": mmsi, "navigation": nav}
        if b.get("nombre"):
            v["name"] = b["nombre"]
        out["urn:mrn:imo:mmsi:" + mmsi] = v
    return out


def escribe(barcos):
    ahora = time.time()
    for m in [m for m, b in barcos.items() if ahora - b.get("t", ahora) > 3 * MAX_EDAD_S]:
        del barcos[m]
    tmp = SALIDA + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a_signalk(barcos, ahora), f)
    os.replace(tmp, SALIDA)


async def escucha(clave, barcos, hasta=None):
    centro = posicion_barco(MALAGA)
    async with websockets.connect(URL, ping_interval=30) as ws:
        await ws.send(json.dumps({"APIKey": clave, "BoundingBoxes": [caja(centro)],
                                  "FilterMessageTypes": TIPOS}))
        escrito = mirado = time.time()
        while hasta is None or time.time() < hasta:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=CADA_S)
                msg = json.loads(raw)
                if "error" in msg:
                    raise RuntimeError("aisstream: %s" % msg["error"])
                anota(barcos, msg, time.time())
            except asyncio.TimeoutError:
                pass
            if time.time() - escrito >= CADA_S:
                escrito = time.time()
                escribe(barcos)
            if time.time() - mirado >= 60:
                mirado = time.time()
                if millas(centro, posicion_barco(centro)) > RECENTRA_MILLAS:
                    return                                   # se reconecta con la caja nueva


def main():
    with open(CLAVE) as f:
        clave = f.read().strip()
    barcos = {}
    if "--prueba" in sys.argv:
        s = int(sys.argv[sys.argv.index("--prueba") + 1]) if len(sys.argv) > 2 else 30
        asyncio.run(escucha(clave, barcos, time.time() + s))
        escribe(barcos)
        print("%d barcos con posicion en %d s -> %s" % (len(a_signalk(barcos, time.time())), s, SALIDA))
        return 0
    while True:
        try:
            asyncio.run(escucha(clave, barcos))
        except Exception as e:
            print("ais_internet: %s; reintento en 30 s" % e, flush=True)
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
