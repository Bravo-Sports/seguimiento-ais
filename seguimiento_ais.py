#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seguimiento_ais.py — con el barco SIN internet, la web sigue al barco y a los rivales por el AIS de internet.

NO corre en la Pi: lo lanza GitHub Actions cada 10 min (repositorio Bravo-Sports/seguimiento-ais,
ver seguimiento-ais/README.md). Cada pasada:
  1. descifra el datos.json del clon de la web (directorio del argumento);
  2. si la Pi ha subido hace menos de ESPERA_S, no hace nada (la Pi tiene internet y manda ella);
  3. si no, escucha aisstream ESCUCHA_S alrededor de la ultima posicion y, si el barco (por su MMSI)
     o un rival (por su nombre) se ha movido ~100 m, actualiza su posicion y su estela, marcados
     "fuente": "ais" (sin viento ni proa: eso solo lo da la Pi);
  4. lo vuelve a cifrar con la misma sal. El commit y el push los hace el workflow.
Cuando la Pi recupera internet, su subida (push --force) SUSTITUYE todo esto: manda la Pi.

Variables de entorno (secretos del repositorio): CLAVE_WEB (la contraseña de la página), AISSTREAM
(la clave de aisstream.io) y MMSI (el del barco).

  python3 seguimiento_ais.py <clon de la web>
"""
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import zlib

import ais_internet as ai
import seguimiento as sg

ESPERA_S = 20 * 60       # con internet la Pi sube en cuanto algo se mueve 100 m: mas que esto sin subir = sin internet
ESCUCHA_S = 150          # la clase B da posicion cada 30 s navegando y cada 3 min parada


def descifra(c, contrasena):
    """(datos, sal) del 'cifrado' de datos.json (lo contrario de seguimiento.cifra)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sal = base64.b64decode(c["sal"])
    k = hashlib.pbkdf2_hmac("sha256", contrasena.encode(), sal, sg.VUELTAS, 32)
    z = AESGCM(k).decrypt(base64.b64decode(c["iv"]), base64.b64decode(c["datos"]), None)
    return json.loads(zlib.decompress(z) if c.get("z") else z), sal


def nuevo(viejo, a):
    """El barco 'viejo' de datos.json movido a la posicion AIS 'a' ({lat, lon, sog, cog, t}), o None
    si el AIS no es mas nuevo o no se ha movido PASO_M."""
    if not a or "t" not in a:
        return None
    if viejo and (a["t"] <= viejo.get("t", 0) or sg.dist_m((viejo["lat"], viejo["lon"]), (a["lat"], a["lon"])) < sg.PASO_M):
        return None
    n = dict(viejo or {}, t=int(a["t"]), lat=round(a["lat"], 5), lon=round(a["lon"], 5),
             sog=a.get("sog") and round(a["sog"], 1), cog=a.get("cog") and round(a["cog"]),
             proa=None, tws=None, twd=None, fuente="ais")
    n.pop("estela", None)
    return n


def actualiza(d, barcos, mmsi, ahora):
    """Mete en d lo que el AIS (barcos: {mmsi: {nombre, lat, lon, sog, cog, t}}) tiene mas nuevo.
    True si ha cambiado algo."""
    cambia = False
    b = nuevo(d.get("barco"), barcos.get(str(mmsi)))
    if b:
        d["barco"] = b
        d["rastro"], _c = sg.apunta(d.get("rastro") or [], b, ahora)
        cambia = True
    por_nombre = {x["nombre"].strip().lower(): x for x in barcos.values() if x.get("nombre") and "t" in x}
    rivales = {r["nombre"].strip().lower(): r for r in d.get("rivales") or []}
    for n in set(rivales) | {x.lower() for x in d.get("buscar") or []}:
        r = nuevo(rivales.get(n), por_nombre.get(n))
        if r:
            r.setdefault("nombre", por_nombre[n]["nombre"])
            r["estela"], _c = sg.apunta((rivales.get(n) or {}).get("estela") or [], r, ahora, horas=6)
            rivales[n] = r
            cambia = True
    d["rivales"] = list(rivales.values())
    return cambia


async def oye(clave, centro, barcos, segundos):
    import websockets
    fin = time.time() + segundos
    async with websockets.connect(ai.URL, ping_interval=30) as ws:
        await ws.send(json.dumps({"APIKey": clave, "BoundingBoxes": [ai.caja(centro)], "FilterMessageTypes": ai.TIPOS}))
        while time.time() < fin:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, fin - time.time())))
            except asyncio.TimeoutError:
                break
            if "error" in msg:
                raise RuntimeError("aisstream: %s" % msg["error"])
            ai.anota(barcos, msg, time.time())


def main():
    f = os.path.join(sys.argv[1], "datos.json")
    env = json.load(open(f, encoding="utf-8"))
    contrasena = "".join(os.environ["CLAVE_WEB"].split()).lower()     # igual que la Pi y la pagina
    d, sal = descifra(env["cifrado"], contrasena)
    ahora = time.time()
    if ahora - d.get("generado", 0) < ESPERA_S:
        print("la Pi sube con normalidad: nada que hacer")
        return 0
    b, r = d.get("barco"), d.get("rastro") or []
    centro = (b["lat"], b["lon"]) if b else (r[-1][1], r[-1][2]) if r else None
    if not centro:
        print("sin posicion de partida")
        return 0
    barcos = {}
    asyncio.run(oye(os.environ["AISSTREAM"].strip(), centro, barcos, ESCUCHA_S))
    if not actualiza(d, barcos, os.environ["MMSI"].strip(), time.time()):
        print("sin novedades por AIS (%d barcos oidos)" % len(barcos))     # sin posiciones ni nombres: el log es publico
        return 0
    d["ais_t"] = int(time.time())
    env["cifrado"] = sg.cifra(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode(), contrasena, sal)
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(env, fh, ensure_ascii=False, separators=(",", ":"))
    print("actualizado por AIS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
