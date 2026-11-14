#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seguimiento.py — página de seguimiento para la familia: posición, rastro de 48 h, velocidad y viento,
la previsión de la Pi y los avisos de AEMET, el viento (barbas), la corriente y la ola de las próximas 24 h, la ruta sugerida que ha calculado la app y, en regata, la línea, las boyas y los rivales (los que
estén en la lista de rivales de la app, por su nombre en el AIS) con su estela de 6 h.

La Pi no abre ningún puerto: SUBE con git dos ficheros a un repositorio de GitHub con GitHub Pages
(index.html, la página, y datos.json, lo que cambia). Lo lanza seguimiento.timer cada 2 min;
solo sube si el barco o un rival se ha movido unos 100 m, si cambia la regata o la previsión, o
cada hora. Cada subida SUSTITUYE a la anterior (un solo commit, push --force): el repositorio es
público (GitHub Pages gratis lo exige) y así no guarda el historial de posiciones.

Acceso: una CLAVE DE DESPLIEGUE de GitHub (deploy key) con escritura SOLO en ese repositorio:
  ~/.ssh/seguimiento          la clave privada (ssh-keygen en la Pi; no sale de ahí ni va a backups)
  ~/.seguimiento-repo         una línea: git@github.com:Bravo-Sports/bravo-sports.github.io.git
                              (https://bravo-sports.github.io; hasta el 10-nov-2026, ManuelBravo01/bravosports)
Sin ~/.seguimiento-repo el script no sube nada (sale sin error).

  seguimiento.py            una pasada (lee SignalK, apunta el rastro, sube si toca)
  seguimiento.py --prueba   imprime datos.json sin subir nada
"""
import base64
import zlib
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.request

SIGNALK = "http://127.0.0.1:3000/signalk/v1/api/vessels/self"
DIR = os.path.expanduser("~/seguimiento")
REPO = os.path.expanduser("~/.seguimiento-repo")
CLAVE = os.path.expanduser("~/.ssh/seguimiento")
CONTRASENA = os.path.expanduser("~/.seguimiento-clave")   # si existe, datos.json va cifrado con ella (la pide la pagina)
VUELTAS = 200000                                         # PBKDF2: lo mismo en seguimiento.html
DIR_APP = os.path.expanduser("~/gribs/app")             # prevision.json, avisos.json y ais_internet.json (publica_app.py)
SIGNALK_BARCOS = "http://127.0.0.1:3000/signalk/v1/api/vessels"
REGATA = "http://127.0.0.1:8091/regata"
SUGERIDA = "http://127.0.0.1:8091/sugerida"               # la ruta que ha calculado la app
GRIB_CORR = os.path.expanduser("~/gribs/corrientes_estrecho.grb")   # IBI, ~2,8 km (corrientes_estrecho.sh)
PAGINA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seguimiento.html")
ICONO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seguimiento_icono.png")   # el de la pantalla de inicio del movil
HORAS = 48                      # rastro que se enseña
PASO_M, PASO_S = 100, 3600      # se apunta y se sube con 100 m de movimiento o una hora
KN = 1.943844                   # m/s -> nudos


def dist_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def valor(d, ruta):
    """value de un camino de SignalK ('navigation.position'), o None."""
    for k in ruta.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d.get("value") if isinstance(d, dict) else None


def lee_barco(sk, ahora):
    """{t, lat, lon, sog, cog, proa, tws, twd} del árbol de SignalK; None sin posición fresca (10 min)."""
    p = sk.get("navigation", {}).get("position", {})
    v = p.get("value") or {}
    if not isinstance(v.get("latitude"), (int, float)):
        return None
    ts = p.get("timestamp")
    if ts:
        try:
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            if ahora - t > 600:
                return None
        except ValueError:
            pass

    def num(ruta, f):
        x = valor(sk, ruta)
        return round(f(x), 1) if isinstance(x, (int, float)) else None
    sog = num("navigation.speedOverGround", lambda x: x * KN)
    tws = num("environment.wind.speedTrue", lambda x: x * KN)
    if tws is None and (sog is None or sog < 0.5):     # parado (en puerto) SignalK no da el real: es el aparente
        tws = num("environment.wind.speedApparent", lambda x: x * KN)
    twd = num("environment.wind.directionTrue", lambda x: math.degrees(x) % 360)
    if twd is None:                                   # la magnética mas la declinación (si se conoce)
        var = valor(sk, "navigation.magneticVariation")
        twd = num("environment.wind.directionMagnetic",
                  lambda x: math.degrees(x + (var if isinstance(var, (int, float)) else 0)) % 360)
    # proa (compas): con el barco parado el rumbo GPS no vale; la verdadera o la magnetica mas la declinacion
    proa = num("navigation.headingTrue", lambda x: math.degrees(x) % 360)
    if proa is None:
        var = valor(sk, "navigation.magneticVariation")
        proa = num("navigation.headingMagnetic", lambda x: math.degrees(x + (var if isinstance(var, (int, float)) else 0)) % 360)
    return {"t": int(ahora), "lat": round(v["latitude"], 5), "lon": round(v["longitude"], 5), "sog": sog,
            "cog": num("navigation.courseOverGroundTrue", lambda x: math.degrees(x) % 360), "proa": proa, "tws": tws, "twd": twd}


def apunta(rastro, b, ahora, horas=HORAS):
    """Añade b al rastro si toca (100 m o una hora desde el último) y quita lo de más de 'horas'.
    Devuelve (rastro, True si ha cambiado)."""
    rastro = [q for q in rastro if ahora - q[0] <= horas * 3600]
    if rastro:
        u = rastro[-1]
        if dist_m((u[1], u[2]), (b["lat"], b["lon"])) < PASO_M and ahora - u[0] < PASO_S:
            return rastro, False
    rastro.append([b["t"], b["lat"], b["lon"], b["sog"]])
    return rastro, True


def rivales_ais(fuentes, nombres, ahora):
    """Los rivales (nombres del AIS en minúsculas) con posición de menos de 10 min, de los barcos de
    SignalK y de ais_internet.json (mismo formato); manda el primero que lo tenga (el AIS de a bordo)."""
    out = {}
    for vs in fuentes:
        for v in (vs or {}).values():
            if not isinstance(v, dict):
                continue
            n = v.get("name")
            n = n.get("value") if isinstance(n, dict) else n
            if not isinstance(n, str) or n.strip().lower() not in nombres or n.strip().lower() in out:
                continue
            b = lee_barco(v, ahora)
            if b:
                b["nombre"] = n.strip()
                out[n.strip().lower()] = b
    return out


def ais_todos(fuentes, ahora, centro, excluir, radio_nm=60, maximo=300):
    """Todos los barcos del AIS (el de a bordo manda) con posicion de menos de 10 min a menos de
    'radio_nm' del barco: [nombre, lat, lon, sog, rumbo]. Sin los de 'excluir' (rivales y el propio)."""
    out, vistos = [], set()
    for vs in fuentes:
        for k, v in (vs or {}).items():
            if not isinstance(v, dict) or k in excluir or k in vistos:
                continue
            n = v.get("name")
            n = (n.get("value") if isinstance(n, dict) else n) or v.get("mmsi") or k.split(":")[-1]
            if str(n).strip().lower() in excluir:
                continue
            b = lee_barco(v, ahora)                    # None sin posicion de menos de 10 min
            if not b:
                continue
            if centro and dist_m(centro, (b["lat"], b["lon"])) > radio_nm * 1852:
                continue
            vistos.add(k)
            out.append([str(n).strip()[:30], round(b["lat"], 4), round(b["lon"], 4), b["sog"],
                        b["proa"] if b["proa"] is not None and (b["sog"] or 0) < 1 else b["cog"]])
    out.sort(key=lambda x: dist_m(centro, (x[1], x[2])) if centro else 0)
    return out[:maximo]


def viaje(sug, b):
    """Millas que faltan por la ruta sugerida (desde su punto mas cercano al barco) y llegada a la
    velocidad de ahora (None si va a menos de 1 nudo: entonces vale la llegada prevista de la ruta)."""
    pts = sug.get("puntos") if sug.get("activa") else None
    if not pts or not b:
        return None
    i = min(range(len(pts)), key=lambda k: dist_m(pts[k][:2], (b["lat"], b["lon"])))
    m = (dist_m((b["lat"], b["lon"]), pts[i][:2]) + sum(dist_m(pts[k][:2], pts[k + 1][:2]) for k in range(i, len(pts) - 1))) / 1852
    v = b.get("sog") or 0
    return {"destino": sug.get("destino"), "millas": round(m, 1), "eta": int(b["t"] + m / v * 3600) if v >= 1 else None}


def dia(d, b, ahora):
    """Resumen de hoy (hora local), sumado en cada pasada (5 min): millas, velocidad maxima y
    segundos navegando (a 1 nudo o mas). Pasos de menos de 37 m no cuentan: es el GPS que baila."""
    hoy = time.strftime("%Y-%m-%d", time.localtime(ahora))
    if d.get("fecha") != hoy:
        d = {"fecha": hoy, "millas": 0.0, "max": 0.0, "seg": 0}
    if b:
        u = d.get("u")
        if u and 0 < ahora - u[0] <= 900:
            m = dist_m((u[1], u[2]), (b["lat"], b["lon"])) / 1852
            if m > .02:
                d["millas"] = round(d["millas"] + m, 2)
            if (b["sog"] or 0) >= 1:
                d["seg"] = int(d["seg"] + ahora - u[0])
        d["max"] = max(d["max"], b["sog"] or 0)
        d["u"] = [int(ahora), b["lat"], b["lon"]]
    return d


def presion_grib(dir_app, b, ahora):
    """(hPa, cambio previsto en 3 h) de la prevision en el punto de la rejilla mas cercano al barco."""
    pasos = []
    for p in lee_json(os.path.join(dir_app, "capa_grib.json"), {}).get("pasos", []):
        try:
            pasos.append((dt.datetime.fromisoformat(p["t"]).timestamp(), p["puntos"]))
        except (KeyError, TypeError, ValueError):
            pass
    if not pasos or not b:
        return None, None

    def en(t):
        _t, q = min(pasos, key=lambda x: abs(x[0] - t))
        x = min(q, key=lambda x: dist_m((x[0], x[1]), (b["lat"], b["lon"])))
        return x[8] if len(x) > 8 else None
    a, c = en(ahora), en(ahora + 3 * 3600)
    return a, (round(c - a, 1) if a is not None and c is not None else None)


def a_bordo(sk, b, ahora, hist, dir_app):
    """Presion (barometro de SignalK si lo hay, y su tendencia en 3 h; si no, la de la prevision),
    temperatura del agua, profundidad y bateria de servicio. Devuelve (datos, historial de presion)."""
    out = {}
    pr = valor(sk, "environment.outside.pressure")
    if isinstance(pr, (int, float)):
        hpa = round(pr / 100, 1)
        hist = [h for h in hist if ahora - h[0] <= 4 * 3600] + [[int(ahora), hpa]]
        viejos = [h for h in hist if ahora - h[0] >= 3 * 3600 - 600]
        out.update(presion=hpa, tend3h=round(hpa - viejos[-1][1], 1) if viejos else None, fuente="barómetro")
    else:
        hpa, t3 = presion_grib(dir_app, b, ahora)
        if hpa is not None:
            out.update(presion=round(hpa, 1), tend3h=t3, fuente="previsión")
    agua = valor(sk, "environment.water.temperature")
    if isinstance(agua, (int, float)):
        out["agua"] = round(agua - 273.15, 1)
    fondo = valor(sk, "environment.depth.belowSurface")
    fondo = fondo if isinstance(fondo, (int, float)) else valor(sk, "environment.depth.belowTransducer")
    if isinstance(fondo, (int, float)):
        out["fondo"] = round(fondo, 1)
    soc, v = valor(sk, "electrical.batteries.house.capacity.stateOfCharge"), valor(sk, "electrical.batteries.house.voltage")
    if isinstance(soc, (int, float)):
        out["bateria"] = round(soc * 100)
    if isinstance(v, (int, float)):
        out["voltios"] = round(v, 2)
    return out, hist


def garreo(f, b):
    """El fondeo de la app [lat, lon, radio, (radio corto, sectores)] con la distancia del barco y si
    esta fuera. Sectores (como tactica/Fondeo.java): 8 de 45 grados vistos desde el ancla (bit 0 = N,
    1 = NE...); los marcados usan el radio corto."""
    if not f:
        return None
    corto, sect = (f[3], int(f[4])) if len(f) >= 5 else (f[2], 0)
    out = {"lat": f[0], "lon": f[1], "radio": f[2], "dist": None, "fuera": False}
    if sect:
        out.update(corto=corto, sectores=sect)
    if b:
        d = dist_m((f[0], f[1]), (b["lat"], b["lon"]))
        rumbo = math.degrees(math.atan2((b["lon"] - f[1]) * math.cos(math.radians(f[0])), b["lat"] - f[0])) % 360
        radio = corto if sect >> int((rumbo + 22.5) % 360 // 45) & 1 else f[2]
        out.update(dist=round(d), fuera=d > radio, limite=radio)
    return out


def serie(s, b, bordo, ahora, horas=24):
    """Historial para la grafica: [t, sog, tws, hPa] de cada pasada, las ultimas 'horas'."""
    s = [x for x in s if ahora - x[0] <= horas * 3600]
    if b:
        s.append([int(ahora), b["sog"], b["tws"], bordo.get("presion")])
    return s


def resume(s, cada_s=600):
    """La serie en tramos de 'cada_s' (el ultimo valor de cada uno): ~144 puntos por dia."""
    out = {}
    for x in s:
        out[x[0] // cada_s] = x
    return [out[k] for k in sorted(out)]


def mareas(b, ahora, fichero=os.path.expanduser("~/gribs/mareas_estrecho.grb"), horas=24):
    """Proximas pleamares y bajamares (IBI) en el barco o, si su punto del modelo no tiene dato
    (pegado a tierra), en el puerto de estrecho.PUERTOS mas cercano: {lugar, ex: [[t, m, pleamar]]}."""
    if not b or not os.path.exists(fichero):
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import estrecho as es
        desde = dt.datetime.fromtimestamp(ahora, dt.timezone.utc)
        sitios = [("tu posición", b["lat"], b["lon"])] + sorted(es.PUERTOS, key=lambda p: dist_m((p[1], p[2]), (b["lat"], b["lon"])))[:1]
        for nombre, la, lo in sitios:
            ex = [e for e in es.extremos(es.altura_mar(fichero, la, lo)) if desde <= e[0] <= desde + dt.timedelta(hours=horas)]
            if ex:
                return {"lugar": nombre, "ex": [[int(h.timestamp()), round(m, 2), bool(p)] for h, m, p in ex]}
    except Exception:
        pass
    return None


def mareas_cache(b, ahora):
    """mareas() solo si cambia el GRIB, el barco se mueve ~5 km o pasa una hora (lee el GRIB, ~1 s)."""
    f = os.path.join(DIR, "mareas.json")
    c = lee_json(f, {})
    try:
        mt = os.path.getmtime(os.path.expanduser("~/gribs/mareas_estrecho.grb"))
    except OSError:
        return None
    clave = [mt, b and round(b["lat"], 2), b and round(b["lon"], 2)]
    if c.get("clave") != clave or ahora - c.get("t", 0) > 3600:
        c = {"clave": clave, "t": ahora, "m": mareas(b, ahora)}
        guarda_json(f, c)
    m = c.get("m")
    if m:
        m = dict(m, ex=[e for e in m["ex"] if e[0] >= ahora - 1800])
    return m


def meteo(dir_app):
    """Previsión de la Pi (prevision.json: 36 h en bloques de 6 h y el aviso de temporal) y avisos de AEMET."""
    m = {}
    try:
        p = json.load(open(os.path.join(dir_app, "prevision.json"), encoding="utf-8"))
        m = {"modelo": p.get("modelo"), "lugar": p.get("lugar"), "aviso": p.get("aviso"), "generado": p.get("generado"),
             "bloques": [{k: b.get(k) for k in ("etiqueta", "cardinal", "kn", "racha", "hs", "tp")} for b in p.get("bloques", [])[:6]]}
    except (OSError, ValueError):
        pass
    try:
        m["avisos"] = json.load(open(os.path.join(dir_app, "avisos.json"), encoding="utf-8")).get("lineas", [])
    except (OSError, ValueError):
        pass
    return m


def capa(dir_app, ahora, horas=24):
    """Viento, corriente y ola para el mapa, de capa_grib.json (publica_app.py): los pasos de 3 h de
    ahora a 'horas' y uno de cada dos puntos (~14 km). Por punto:
    [lat, lon, viento_kn, viento_de, corriente_kn, corriente_hacia, hs, periodo_s, ola_de] (None si no hay)."""
    pasos = []
    for p in lee_json(os.path.join(dir_app, "capa_grib.json"), {}).get("pasos", []):
        try:
            t = dt.datetime.fromisoformat(p["t"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if not ahora - 5400 <= t <= ahora + horas * 3600:
            continue
        q = p.get("puntos") or []
        las, los = sorted({x[0] for x in q}), sorted({x[1] for x in q})
        pares = {x for x in las[::2]}, {x for x in los[::2]}
        pasos.append({"t": int(t), "puntos": [x[:7] + (x[11:13] if len(x) >= 13 else [None, None])
                                              for x in q if x[0] in pares[0] and x[1] in pares[1]]})
    return {"pasos": pasos} if pasos else None


def cifra(texto, contrasena, sal):
    """AES-256-GCM con la clave PBKDF2-SHA256 de la contraseña; la página lo descifra con WebCrypto."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k = hashlib.pbkdf2_hmac("sha256", contrasena.encode(), sal, VUELTAS, 32)
    iv = os.urandom(12)
    b = lambda x: base64.b64encode(x).decode()
    return {"sal": b(sal), "iv": b(iv), "z": 1, "datos": b(AESGCM(k).encrypt(iv, zlib.compress(texto, 9), None))}   # comprimido y cifrado


def corriente_fina(grib, ahora, horas=30):
    """La corriente IBI a su resolucion (~2,8 km) para la animacion de la web: la costa sale bien y
    el paso del Estrecho no se corta. Pasos de 3 h de ahora a 'horas'. Formato compacto: la rejilla
    (origen, paso y tamano), 'mar' (una cifra por celda: 1 mar, 0 tierra, fila a fila de sur a norte)
    y por paso 'uv': este y norte en decimas de nudo de las celdas de mar, en ese orden."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ruta                                        # el lector de GRIB de la Pi (/opt/h2000-bridge)
    c = ruta.Campo(grib, "indicatorOfParameter")
    las, los = list(c.lats), list(c.lons)
    mar, pasos = None, []
    for t in c.horas:
        if t.hour % 3 or not ahora - 5400 <= t.timestamp() <= ahora + horas * 3600:
            continue
        m, uv = [], []
        for la in las:
            for lo in los:
                u, v = c.valor("49", t, la, lo), c.valor("50", t, la, lo)
                ok = u is not None and v is not None
                m.append("1" if ok else "0")
                if ok:
                    uv += [round(u * KN * 10), round(v * KN * 10)]
        m = "".join(m)
        if mar is None:
            mar = m
        if m == mar:                                   # la mascara de tierra es la misma en todos los pasos
            pasos.append({"t": int(t.timestamp()), "uv": uv})
    if not pasos:
        return None
    return {"la0": las[0], "lo0": los[0], "dla": (las[-1] - las[0]) / (len(las) - 1), "dlo": (los[-1] - los[0]) / (len(los) - 1),
            "ni": len(las), "nj": len(los), "mar": mar, "pasos": pasos}


def corriente_web(ahora, contrasena, sal, clave):
    """(hash, bytes de corriente.json) ya cifrados si hay contrasena. Se rehace (~40 s) solo si cambia
    el GRIB, la contrasena o cada 6 h; si no, los mismos bytes (git no vuelve a subirlos)."""
    fc, fm = os.path.join(DIR, "corriente.json"), os.path.join(DIR, "corriente.meta")
    try:
        mt = os.path.getmtime(GRIB_CORR)
    except OSError:
        return None, None
    meta = lee_json(fm, {})
    if meta.get("mtime") != mt or meta.get("clave") != clave or ahora - meta.get("t", 0) > 6 * 3600 or not os.path.exists(fc):
        cf = corriente_fina(GRIB_CORR, ahora)
        if not cf:
            return None, None
        plano = json.dumps(cf, separators=(",", ":")).encode()
        h = hashlib.md5(plano).hexdigest()[:8]
        sub = json.dumps({"cifrado": cifra(plano, contrasena, sal)}).encode() if contrasena else plano
        with open(fc + ".tmp", "wb") as fh:
            fh.write(sub)
        os.replace(fc + ".tmp", fc)
        meta = {"mtime": mt, "clave": clave, "t": ahora, "h": h}
        guarda_json(fm, meta)
    return meta["h"], open(fc, "rb").read()


def lee_json(ruta, defecto):
    try:
        return json.load(open(ruta, encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


def guarda_json(ruta, d):
    with open(ruta + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(ruta + ".tmp", ruta)


def url_json(url):
    try:
        return json.load(urllib.request.urlopen(url, timeout=5))
    except Exception:
        return None


def sube_git(dir_web, url, ficheros, clave=None):
    """Deja 'ficheros' {nombre: bytes} en el repositorio 'url' como UN solo commit (push --force):
    cada subida sustituye a la anterior y no queda historial. 'clave': deploy key SSH."""
    env = dict(os.environ)
    if clave:
        env["GIT_SSH_COMMAND"] = "ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" % clave

    def git(*a):
        subprocess.run(["git", "-C", dir_web] + list(a), check=True, env=env, capture_output=True, timeout=120)
    if not os.path.isdir(os.path.join(dir_web, ".git")):
        os.makedirs(dir_web, exist_ok=True)
        git("init", "-q", "-b", "main")
        git("remote", "add", "origin", url)
        git("config", "user.name", "Barco")
        git("config", "user.email", "barco@localhost")
    git("remote", "set-url", "origin", url)              # si ~/.seguimiento-repo cambia, sube al nuevo
    for n, d in ficheros.items():
        with open(os.path.join(dir_web, n), "wb") as fh:
            fh.write(d)
    git("add", "-A")
    git("checkout", "-q", "--orphan", "nuevo")            # rama sin padres: un solo commit
    git("commit", "-q", "-m", "seguimiento")
    git("branch", "-M", "main")
    git("push", "-q", "--force", "origin", "main")


def main():
    ahora = time.time()
    os.makedirs(DIR, exist_ok=True)
    fr, fv, fs = (os.path.join(DIR, x) for x in ("rastro.json", "rivales.json", "subido.json"))
    rastro, estelas = lee_json(fr, []), lee_json(fv, {})
    sk = url_json(SIGNALK)
    b = lee_barco(sk, ahora) if sk else None
    if b:
        b["nombre"] = (sk.get("name") or "").strip() or None     # el nombre del barco en SignalK
    cambia = False
    if b:
        rastro, cambia = apunta(rastro, b, ahora)
        guarda_json(fr, rastro)
    # regata (linea, boyas y rivales, los manda la app a salida_regata.py) y rivales por el AIS
    reg = url_json(REGATA) or {}
    nombres = set(reg.get("rivales") or [])
    barcos = url_json(SIGNALK_BARCOS)
    riv = rivales_ais([barcos, lee_json(os.path.join(DIR_APP, "ais_internet.json"), {})], nombres, ahora) if nombres else {}
    estelas = {n: e for n, e in estelas.items() if n in nombres}
    for n, r in riv.items():
        estelas[n], _c = apunta(estelas.get(n, []), r, ahora, horas=6)
    guarda_json(fv, estelas)
    rivales = [dict(r, estela=estelas.get(n, [])) for n, r in riv.items()]
    regata = {k: reg.get(k) for k in ("comite", "boya", "marcas")} if reg.get("comite") or reg.get("marcas") else None
    nogo = reg.get("nogo") or None                 # zonas prohibidas de la app
    # AIS: todos los barcos cerca (menos el propio y los rivales, que ya salen); no fuerza la subida
    yo = url_json("http://127.0.0.1:3000/signalk/v1/api/self")
    yo = yo.split(".", 1)[-1] if isinstance(yo, str) else ""
    ais = ais_todos([barcos, lee_json(os.path.join(DIR_APP, "ais_internet.json"), {})], ahora,
                    b and (b["lat"], b["lon"]), set(nombres) | {yo, "self"})
    met = meteo(DIR_APP)
    fd = os.path.join(DIR, "dia.json")
    hoy = dia(lee_json(fd, {}), b, ahora)
    guarda_json(fd, hoy)
    fp = os.path.join(DIR, "presion.json")
    bordo, hist = a_bordo(sk or {}, b, ahora, lee_json(fp, []), DIR_APP)
    guarda_json(fp, hist)
    fondeo = garreo(reg.get("fondeo"), b)
    fse = os.path.join(DIR, "serie.json")
    ser = serie(lee_json(fse, []), b, bordo, ahora)
    guarda_json(fse, ser)
    mar = mareas_cache(b, ahora)
    cg = capa(DIR_APP, ahora)
    sug = url_json(SUGERIDA) or {}
    ruta = {k: sug.get(k) for k in ("destino", "salida", "llegada", "puntos")} if sug.get("activa") else None
    via = viaje(sug, b)
    pagina = open(PAGINA, "rb").read()
    version = hashlib.md5(pagina).hexdigest()[:8]
    pagina = pagina.replace(b"__VERSION__", version.encode())      # la pagina sabe su version
    # sin espacios y en minusculas (igual en la pagina): el teclado del movil no la estropea
    contrasena = "".join(open(CONTRASENA, encoding="utf-8").read().split()).lower() if os.path.exists(CONTRASENA) else ""
    sal, cambio_clave = b"", ""
    if contrasena:
        fsal = os.path.join(DIR, "sal")
        if not os.path.exists(fsal):
            with open(fsal, "wb") as fh:
                fh.write(os.urandom(16))
        sal = open(fsal, "rb").read()
        cambio_clave = hashlib.sha256(sal + contrasena.encode()).hexdigest()
    corr_h, corr_bytes = corriente_web(ahora, contrasena, sal, cambio_clave) if "--prueba" not in sys.argv else (None, None)
    datos = json.dumps({"generado": int(ahora), "barco": b, "rastro": rastro, "regata": regata, "rivales": rivales, "meteo": met,
                        "ruta": ruta, "capa": cg, "corriente": corr_h, "ais": ais, "nogo": nogo,
                        "viaje": via, "hoy": {k: hoy[k] for k in ("millas", "max", "seg")}, "bordo": bordo, "fondeo": fondeo,
                        "serie": resume(ser), "mareas": mar, "buscar": sorted(nombres)},   # buscar: rivales para seguimiento_ais.py
                       ensure_ascii=False, separators=(",", ":")).encode()
    envio = {"pagina": version}
    if contrasena:
        envio["cifrado"] = cifra(datos, contrasena, sal)
    else:
        envio.update(json.loads(datos))
    if "--prueba" in sys.argv:
        print(datos.decode())
        return
    if not os.path.exists(REPO):
        return                                       # sin repositorio: seguimiento web apagado
    # se sube si el barco o un rival se ha movido ~100 m, si cambia la regata o la prevision, o cada hora
    firma = json.dumps([b and (round(b["lat"], 3), round(b["lon"], 3)), [(r["nombre"], round(r["lat"], 3), round(r["lon"], 3)) for r in rivales],
                        regata, nogo, fondeo and (fondeo["lat"], fondeo["radio"], fondeo.get("corto"), fondeo.get("sectores"), fondeo["fuera"]), met.get("generado"), met.get("avisos"), sug.get("seq"),
                        cg and [x["t"] for x in cg["pasos"]], cg and cg["pasos"][0]["puntos"][:3],
                        hashlib.md5(pagina).hexdigest(), cambio_clave, corr_h], ensure_ascii=False)
    antes = lee_json(fs, {})
    if not cambia and firma == antes.get("firma") and ahora - antes.get("t", 0) < PASO_S:
        return
    sube_git(os.path.join(DIR, "web"), open(REPO, encoding="utf-8").read().strip(),
             dict({"index.html": pagina, "datos.json": json.dumps(envio, ensure_ascii=False, separators=(",", ":")).encode()},
                  **({"corriente.json": corr_bytes} if corr_bytes else {}),
                  **({"icono.png": open(ICONO, "rb").read()} if os.path.exists(ICONO) else {})), CLAVE if os.path.exists(CLAVE) else None)
    guarda_json(fs, {"firma": firma, "t": ahora})
    print("subido: %s, %d rivales, %d puntos" % ("sin GPS" if not b else "%.5f, %.5f" % (b["lat"], b["lon"]), len(rivales), len(rastro)))


if __name__ == "__main__":
    main()
