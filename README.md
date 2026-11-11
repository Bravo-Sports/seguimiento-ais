# seguimiento-ais

Si el barco se queda sin internet, la web https://bravo-sports.github.io sigue al barco y a los
rivales por el AIS de internet (aisstream.io). Cada 10 minutos, GitHub Actions:

1. descifra `datos.json` de la web;
2. si la Pi del barco ha subido hace menos de 20 minutos, no hace nada;
3. si no, escucha el AIS 150 s y mueve el barco (por su MMSI) y los rivales (por su nombre), con
   su estela, marcados «por AIS»;
4. lo vuelve a cifrar y lo sube.

Cuando la Pi recupera internet, su subida sustituye a todo esto.

Original en el repositorio del proyecto: `raspberrypi/seguimiento_ais.py`. `seguimiento.py` y
`ais_internet.py` son COPIAS de `raspberrypi/`: si cambian allí, se vuelven a copiar aquí.

Configuración (Settings > Secrets and variables > Actions):

| Nombre | Tipo | Qué es |
|---|---|---|
| `CLAVE_WEB` | secreto | la contraseña de la web |
| `AISSTREAM` | secreto | la clave de aisstream.io (la de `~/.aisstream` en la Pi) |
| `DEPLOY_KEY` | secreto | clave privada SSH; su pública es una deploy key con escritura en bravo-sports.github.io |
| `MMSI` | variable | el MMSI del barco |

Prueba a mano: Actions > seguimiento por AIS > Run workflow.
