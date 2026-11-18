"""
Experimentos de resiliencia (ABET 6): un comando por escenario.

Uso (con Docker Desktop abierto, desde la raíz del repositorio):

    python experimentos/demo.py feliz
    python experimentos/demo.py compensacion
    python experimentos/demo.py circuito                 # Elegibilidad lenta y luego caída, con CB
    python experimentos/demo.py circuito --sin-patron    # mismo experimento sin CB
    python experimentos/demo.py bulkhead                 # carga simultánea con Elegibilidad lenta
    python experimentos/demo.py bulkhead --sin-patron    # mismo experimento sin Bulkhead
    python experimentos/demo.py ratelimit                # ráfaga contra el gateway (HTTP 429)
    python experimentos/demo.py todos                    # todo lo anterior + tabla comparativa

El script levanta el sistema con la configuración que necesita cada escenario,
activa las fallas con los endpoints de caos (por `docker compose exec`, sin
exponer puertos), manda las solicitudes por el gateway con JWT, verifica la
consistencia entre Cupos y Registro y guarda la salida y los CSV en
experimentos/resultados/<fecha>/. Solo usa la biblioteca estándar de Python.
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RAIZ = Path(__file__).resolve().parent.parent
GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
USUARIO = os.getenv("USUARIO_ESCRITURA", "operador")
CLAVE = os.getenv("CLAVE_ESCRITURA", "operador123")

LATENCIA_CAOS_S = 5.0         # mayor que SAGA_STEP_TIMEOUT (3.5 s): la Saga corta por timeout
CB_RECUPERACION_S = 10        # valor por defecto en docker-compose.yml
RATE_LIMIT_POR_DEFECTO = 15   # valor por defecto en docker-compose.yml
RUT_NO_APTO = "00.000.000-0"  # historial clínico no apto en el mock de Elegibilidad

MARCA = time.strftime("%Y%m%d-%H%M%S")
RESULTADOS = Path(__file__).resolve().parent / "resultados" / MARCA
_CONSTRUIDO = False


# ---------------------------------------------------------------- infraestructura

def compose(*args: str, env: Optional[Dict[str, str]] = None) -> str:
    resultado = subprocess.run(
        ["docker", "compose", *args],
        cwd=RAIZ, env={**os.environ, **(env or {})},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} falló:\n{resultado.stderr}")
    return resultado.stdout


def python_en(servicio: str, codigo: str) -> str:
    """Ejecuta código Python dentro de un contenedor de la red interna."""
    return compose("exec", "-T", servicio, "python", "-c", codigo)


def http_interno(servicio: str, puerto: int, metodo: str, ruta: str, cuerpo: Optional[dict] = None) -> Any:
    datos = json.dumps(cuerpo) if cuerpo is not None else ""
    codigo = (
        "import json, urllib.request\n"
        f"d = {datos!r}\n"
        f"req = urllib.request.Request('http://localhost:{puerto}{ruta}', data=d.encode() if d else None, "
        f"method={metodo!r}, headers={{'Content-Type': 'application/json'}})\n"
        "print(urllib.request.urlopen(req, timeout=30).read().decode())"
    )
    return json.loads(python_en(servicio, codigo))


def levantar(cb: bool = True, bulkhead: bool = True, rate_limit: int = 100000,
             recrear: Tuple[str, ...] = ()) -> None:
    """Levanta el sistema con los patrones pedidos. `recrear` reinicia contadores en memoria."""
    limite = "normal" if rate_limit == RATE_LIMIT_POR_DEFECTO else "sin límite"
    print(f"  Configurando sistema: Circuit Breaker={'ON' if cb else 'OFF'}, "
          f"Bulkhead={'ON' if bulkhead else 'OFF'}, rate limit {limite}...")
    env = {
        "CB_ACTIVO": str(cb).lower(),
        "BULKHEAD_ACTIVO": str(bulkhead).lower(),
        "RATE_LIMIT_RPM": str(rate_limit),
    }
    global _CONSTRUIDO
    compose("up", "-d", "--wait", *([] if _CONSTRUIDO else ["--build"]), env=env)
    _CONSTRUIDO = True
    if recrear:
        compose("up", "-d", "--wait", "--force-recreate", *recrear, env=env)
    resetear_caos()


def restaurar() -> None:
    print("\nRestaurando configuración por defecto (patrones ON, rate limit normal)...")
    compose("up", "-d", "--wait")
    resetear_caos()


def resetear_caos() -> None:
    http_interno("elegibilidad-service", 5002, "POST", "/config/reset")
    http_interno("registro-service", 5003, "POST", "/config/reset")


def latencia_elegibilidad(segundos: float) -> None:
    http_interno("elegibilidad-service", 5002, "POST", "/config/latencia", {"latencia_segundos": segundos})


def falla_elegibilidad(activa: bool) -> None:
    http_interno("elegibilidad-service", 5002, "POST", "/config/falla", {"simular_falla": activa})


def falla_registro(activa: bool) -> None:
    http_interno("registro-service", 5003, "POST", "/config/falla", {"simular_falla": activa})


def respuesta_tardia_registro(segundos: float) -> None:
    http_interno("registro-service", 5003, "POST", "/config/latencia", {"latencia_segundos": segundos})


def estado_resiliencia() -> Dict[str, Any]:
    return http_interno("bff", 5000, "GET", "/resiliencia/estado")


def resumen_directo_bff(rut: str) -> float:
    """Mide el resumen llamando al BFF desde dentro de la red, sin pasar por el gateway."""
    salida = python_en("bff", (
        "import time, urllib.request\n"
        "t = time.perf_counter()\n"
        f"urllib.request.urlopen('http://localhost:5000/api/v1/donantes/{rut}/resumen', timeout=60).read()\n"
        "print(round(time.perf_counter() - t, 3))"
    ))
    return float(salida.strip())


def crear_jornada(id_jornada: str, capacidad: int) -> None:
    """Crea una jornada propia del experimento para no depender de los datos semilla."""
    python_en("cupos-service", (
        "import os, sqlite3\n"
        "c = sqlite3.connect(os.environ['DATABASE_PATH'])\n"
        "c.execute('INSERT INTO jornadas (id_jornada, nombre, fecha, capacidad_total, cupos_disponibles) "
        "VALUES (?, ?, ?, ?, ?)', "
        f"({id_jornada!r}, 'Experimento ABET 6', '2026-12-01', {capacidad}, {capacidad}))\n"
        "c.commit()"
    ))


def estado_en_bases(id_saga: str) -> Tuple[Optional[str], Optional[str]]:
    """Devuelve (estado del cupo en Cupos, estado de la cita en Registro) para un id_saga."""
    consulta = (
        "import os, sqlite3\n"
        "c = sqlite3.connect(os.environ['DATABASE_PATH'])\n"
        "r = c.execute('SELECT estado FROM {tabla} WHERE id_saga = ?', ({id_saga!r},)).fetchone()\n"
        "print(r[0] if r else '-')"
    )
    cupo = python_en("cupos-service", consulta.format(tabla="cupos", id_saga=id_saga)).strip()
    cita = python_en("registro-service", consulta.format(tabla="citas_donacion", id_saga=id_saga)).strip()
    return (None if cupo == "-" else cupo), (None if cita == "-" else cita)


def verificar_consistencia(id_jornada: str) -> Dict[str, Any]:
    """Compara Cupos y Registro: cada cupo reservado debe tener su cita activa y viceversa."""
    cupos = json.loads(python_en("cupos-service", (
        "import json, os, sqlite3\n"
        "c = sqlite3.connect(os.environ['DATABASE_PATH'])\n"
        f"j = c.execute('SELECT capacidad_total, cupos_disponibles FROM jornadas WHERE id_jornada = ?', ({id_jornada!r},)).fetchone()\n"
        f"r = [x[0] for x in c.execute(\"SELECT id_saga FROM cupos WHERE id_jornada = ? AND estado = 'RESERVADO'\", ({id_jornada!r},))]\n"
        "print(json.dumps({'capacidad': j[0], 'disponibles': j[1], 'reservados': r}))"
    )))
    citas = json.loads(python_en("registro-service", (
        "import json, os, sqlite3\n"
        "c = sqlite3.connect(os.environ['DATABASE_PATH'])\n"
        f"print(json.dumps([x[0] for x in c.execute(\"SELECT id_saga FROM citas_donacion WHERE id_jornada = ? AND estado != 'ANULADA'\", ({id_jornada!r},))]))"
    )))
    reservados = set(cupos["reservados"])
    activas = set(citas)
    ok = (cupos["capacidad"] - cupos["disponibles"] == len(reservados)) and reservados == activas
    print(f"  Consistencia: stock {cupos['disponibles']}/{cupos['capacidad']} disponible | "
          f"cupos reservados: {len(reservados)} | citas activas: {len(activas)} | "
          f"{'[OK] Cupos y Registro coinciden' if ok else '[X] INCONSISTENTE'}")
    return {"consistente": ok, "disponibles": cupos["disponibles"], "capacidad": cupos["capacidad"],
            "reservados": len(reservados), "citas_activas": len(activas)}


def orden_compensaciones(id_saga: str) -> List[str]:
    """Lee en el log del BFF las compensaciones que ejecutó la Saga, en el orden en que corrieron."""
    marca = f"[SAGA {id_saga}] Ejecutando compensación: "
    lineas = compose("logs", "--no-log-prefix", "--since", "10m", "bff").splitlines()
    return [linea.split(marca, 1)[1].strip() for linea in lineas if marca in linea]


def marca_tiempo_docker() -> str:
    """Instante actual en UTC, para pedir a docker compose logs solo lo ocurrido desde ahí."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))


def eventos_bff(desde: str) -> List[Dict[str, Any]]:
    """Eventos JSON del BFF (transiciones del circuito y rechazos del bulkhead) desde un instante."""
    # Con muchas solicitudes simultáneas, dos líneas de log pueden quedar pegadas en una:
    # se buscan todos los objetos JSON de cada línea, no solo el primero.
    decodificador = json.JSONDecoder()
    eventos = []
    for linea in compose("logs", "--no-log-prefix", "--since", desde, "bff").splitlines():
        inicio = linea.find('{"evento"')
        while inicio >= 0:
            try:
                evento, fin = decodificador.raw_decode(linea, inicio)
                eventos.append(evento)
            except ValueError:
                fin = inicio + 1
            inicio = linea.find('{"evento"', fin)
    return eventos


def guardar_evidencia_resiliencia(nombre: str, antes: Dict[str, Any], desde: str) -> List[Dict[str, Any]]:
    """Guarda el estado de /resiliencia/estado antes y después, y los eventos del BFF del escenario."""
    despues = estado_resiliencia()
    eventos = eventos_bff(desde)
    RESULTADOS.mkdir(parents=True, exist_ok=True)
    (RESULTADOS / f"{nombre}_estado.json").write_text(
        json.dumps({"antes": antes, "despues": despues}, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTADOS / f"{nombre}_eventos.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eventos), encoding="utf-8")
    transiciones = [f"{e['desde']}->{e['hacia']}" for e in eventos if e["evento"] == "circuito_transicion"]
    rechazos = sum(e["evento"] == "bulkhead_rechazo" for e in eventos)
    print(f"  Eventos del BFF: transiciones del circuito: {', '.join(transiciones) or 'ninguna'} | "
          f"rechazos del bulkhead: {rechazos}")
    print(f"  Evidencia guardada en {nombre}_estado.json y {nombre}_eventos.jsonl")
    return eventos


def id_saga_de(clave: str) -> str:
    """Misma derivación que el BFF (UUIDv5 de la Idempotency-Key)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, clave))


# ---------------------------------------------------------------- cliente del gateway

def _http(metodo: str, url: str, cuerpo: Optional[dict] = None, headers: Optional[dict] = None,
          timeout: float = 120) -> Tuple[int, Any, Dict[str, str]]:
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        cuerpo_err = e.read().decode()
        try:
            return e.code, json.loads(cuerpo_err), dict(e.headers)
        except ValueError:
            return e.code, cuerpo_err, dict(e.headers)


_TOKEN: Optional[str] = None


def token(nuevo: bool = False) -> str:
    global _TOKEN
    if _TOKEN is None or nuevo:
        codigo, datos, _ = _http("POST", f"{GATEWAY}/auth/token", {"usuario": USUARIO, "clave": CLAVE})
        if codigo != 200:
            raise RuntimeError(f"No se pudo obtener el JWT ({codigo}): {datos}")
        _TOKEN = datos["access_token"]
    return _TOKEN


def agendar(rut: str, id_jornada: str, clave: str) -> Dict[str, Any]:
    t0 = time.perf_counter()
    codigo, datos, _ = _http(
        "POST", f"{GATEWAY}/api/v1/donaciones/agendar",
        {"rut_donante": rut, "nombre_donante": "Donante Experimento", "id_jornada": id_jornada,
         "horario": "10:00", "edad": 30, "peso_kg": 70.0},
        {"Authorization": f"Bearer {token()}", "Idempotency-Key": clave},
    )
    segundos = time.perf_counter() - t0
    registro = datos.get("registro") if isinstance(datos, dict) else None
    if isinstance(datos, dict) and "estado" in datos:
        resultado = datos["estado"]
    else:
        detalle = datos.get("detail") if isinstance(datos, dict) else datos
        resultado = str(detalle)
    return {"http": codigo, "segundos": round(segundos, 3), "resultado": resultado,
            "id_saga": id_saga_de(clave),
            "cita_id": (registro or {}).get("cita_id"),
            "idempotente": (registro or {}).get("idempotente")}


def resumen_donante(rut: str) -> Tuple[int, float]:
    t0 = time.perf_counter()
    codigo, _, _ = _http("GET", f"{GATEWAY}/api/v1/donantes/{rut}/resumen",
                         headers={"Authorization": f"Bearer {token()}"})
    return codigo, round(time.perf_counter() - t0, 3)


# ---------------------------------------------------------------- utilidades de salida

class _Duplicador:
    """Escribe la salida en la consola y en experimentos/resultados/<fecha>/salida.txt."""

    def __init__(self, consola, archivo) -> None:
        self.consola, self.archivo = consola, archivo

    def write(self, texto: str) -> int:
        self.consola.write(texto)
        self.archivo.write(texto)
        return len(texto)

    def flush(self) -> None:
        self.consola.flush()
        self.archivo.flush()


def guardar_csv(nombre: str, filas: List[Dict[str, Any]]) -> Path:
    RESULTADOS.mkdir(parents=True, exist_ok=True)
    ruta = RESULTADOS / f"{nombre}.csv"
    campos = list(dict.fromkeys(k for f in filas for k in f))
    with ruta.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=campos)
        escritor.writeheader()
        escritor.writerows(filas)
    print(f"  Resultados guardados en {ruta.name}")
    return ruta


def percentil(valores: List[float], p: float) -> float:
    ordenados = sorted(valores)
    indice = min(len(ordenados) - 1, max(0, round(p / 100 * len(ordenados)) - 1))
    return ordenados[indice]


def titulo(texto: str) -> None:
    print(f"\n{'=' * 78}\n{texto}\n{'=' * 78}")


def corto(texto: str) -> str:
    """Deja visible el motivo y el resultado de la compensación en una línea."""
    if "Transacción compensada" in texto:
        motivo = texto.split(":")[0].split(". ")[0][:60]
        return f"{motivo} -> compensada, cupo liberado"
    if "Atención" in texto:
        return f"{texto.split(':')[0][:40]} -> COMPENSACIÓN FALLIDA"
    return texto[:70]


def fila(i: Any, r: Dict[str, Any], extra: str = "") -> None:
    print(f"  #{str(i):<3} HTTP {r['http']}  {r['segundos']:>6.2f} s  {corto(r['resultado'])}{extra}")


# ---------------------------------------------------------------- escenarios

def escenario_feliz(n: int = 5) -> Dict[str, Any]:
    titulo("Escenario 1: Camino feliz e idempotencia")
    levantar()
    jornada = f"EXP-FELIZ-{MARCA}"
    crear_jornada(jornada, n)
    filas = []
    for i in range(n):
        r = agendar(f"FELIZ-{MARCA}-{i}", jornada, f"feliz-{MARCA}-{i}")
        fila(i + 1, r, f"  | cita {r['cita_id']}")
        filas.append({"n": i + 1, **r})

    print("  >> Reintento de la solicitud #1 con la misma Idempotency-Key")
    reintento = agendar(f"FELIZ-{MARCA}-0", jornada, f"feliz-{MARCA}-0")
    misma_cita = reintento["cita_id"] is not None and reintento["cita_id"] == filas[0]["cita_id"]
    print(f"  #R   HTTP {reintento['http']}  {reintento['segundos']:>6.2f} s  {reintento['resultado']}  "
          f"| cita {reintento['cita_id']} | Registro informa idempotente={reintento['idempotente']}")
    sin_duplicar = misma_cita and reintento["idempotente"] is True
    print(f"  Idempotencia: {'[OK] misma cita y Registro la reconoce como reintento' if sin_duplicar else '[X] se creó otra cita'}")
    filas.append({"n": "reintento", **reintento})

    consistencia = verificar_consistencia(jornada)
    guardar_csv("1_feliz", filas)
    ok = (all(f["resultado"] == "CONFIRMADA" for f in filas[:n]) and sin_duplicar
          and consistencia["consistente"] and consistencia["disponibles"] == 0)
    return {"escenario": "1. Camino feliz", "solicitudes": n + 1,
            "resultado": f"{sum(f['resultado'] == 'CONFIRMADA' for f in filas[:n])}/{n} CONFIRMADA, "
                         f"reintento sin duplicar: {'sí' if sin_duplicar else 'NO'}",
            "latencia_prom_s": round(statistics.mean(f["segundos"] for f in filas[:n]), 3),
            "consistente": consistencia["consistente"], "ok": ok}


def escenario_compensacion() -> Dict[str, Any]:
    titulo("Escenario 2: Fallas en la Saga -> compensación en orden inverso")
    levantar()
    jornada = f"EXP-COMP-{MARCA}"
    crear_jornada(jornada, 2)
    filas: List[Dict[str, Any]] = []
    casos_ok = []

    def caso(nombre: str, rut: str, clave: str, esperado: List[str], estados: Tuple[Any, Any]) -> None:
        r = agendar(rut, jornada, clave)
        orden = orden_compensaciones(r["id_saga"])
        cupo, cita = estado_en_bases(r["id_saga"])
        fila(len(filas) + 1, r)
        print(f"       compensaciones ejecutadas: {' -> '.join(orden) or 'ninguna'} | "
              f"cupo: {cupo or 'no existe'} | cita: {cita or 'no existe'}")
        casos_ok.append(orden == esperado and (cupo, cita) == estados)
        filas.append({"n": len(filas) + 1, "caso": nombre, **r, "compensaciones": " -> ".join(orden),
                      "estado_cupo": cupo, "estado_cita": cita})

    print("  >> A) Registro cae (HTTP 500, no graba): la Saga sabe que no se registró y solo libera el cupo")
    falla_registro(True)
    caso("registro_500", f"COMP-{MARCA}-A", f"comp-{MARCA}-A", ["liberar_cupo"], ("LIBERADO", None))
    resetear_caos()

    print(f"  >> B) Registro graba pero responde en {LATENCIA_CAOS_S:.0f} s (timeout de la Saga: 3.5 s): "
          "resultado incierto")
    print("     La Saga compensa en orden inverso: primero anula el registro, luego libera el cupo")
    respuesta_tardia_registro(LATENCIA_CAOS_S)
    caso("registro_incierto", f"COMP-{MARCA}-B", f"comp-{MARCA}-B",
         ["anular_registro", "liberar_cupo"], ("LIBERADO", "ANULADA"))
    resetear_caos()

    print(f"  >> C) Donante no apto ({RUT_NO_APTO}): Elegibilidad responde 422 y se libera el cupo")
    caso("no_apto", RUT_NO_APTO, f"comp-{MARCA}-C", ["liberar_cupo"], ("LIBERADO", None))

    consistencia = verificar_consistencia(jornada)
    print("  >> Sin fallas: el sistema vuelve a agendar normalmente")
    r = agendar(f"COMP-{MARCA}-ok", jornada, f"comp-{MARCA}-ok")
    fila(len(filas) + 1, r)
    filas.append({"n": len(filas) + 1, "caso": "recuperado", **r})
    final = verificar_consistencia(jornada)

    guardar_csv("2_compensacion", filas)
    ok = (all(casos_ok) and consistencia["disponibles"] == consistencia["capacidad"]
          and consistencia["consistente"] and final["consistente"] and r["resultado"] == "CONFIRMADA")
    return {"escenario": "2. Compensación", "solicitudes": len(filas),
            "resultado": f"{sum(casos_ok)}/3 compensadas como se esperaba (incluye anular -> liberar)",
            "latencia_prom_s": round(statistics.mean(f["segundos"] for f in filas), 3),
            "consistente": consistencia["consistente"] and final["consistente"], "ok": ok}


def escenario_circuito(n: int = 10, sin_patron: bool = False, modo: str = "lenta-caida") -> Dict[str, Any]:
    """Mismo protocolo con y sin patrón: mismas fases, mismas pausas y la misma solicitud final."""
    nombre = "Circuit Breaker " + ("DESACTIVADO" if sin_patron else "ACTIVO")
    falla = {"lenta-caida": "lenta y luego caída", "latencia": "lenta", "error": "caída"}[modo]
    titulo(f"Escenario 3: Elegibilidad {falla} -> {nombre}")
    levantar(cb=not sin_patron, bulkhead=True, recrear=("bff",))
    jornada = f"EXP-CB-{'SIN' if sin_patron else 'CON'}-{MARCA}"
    crear_jornada(jornada, n + 1)
    antes, desde = estado_resiliencia(), marca_tiempo_docker()

    fases = {"latencia": [("lenta", n)], "error": [("caida", n)],
             "lenta-caida": [("lenta", n // 2), ("caida", n - n // 2)]}[modo]
    pausa = CB_RECUPERACION_S + 1
    filas: List[Dict[str, Any]] = []
    por_fase: List[Dict[str, Any]] = []
    aperturas = 0
    for numero_fase, (fase, cantidad) in enumerate(fases):
        resetear_caos()
        if fase == "lenta":
            print(f"  >> Fase LENTA: Elegibilidad tarda {LATENCIA_CAOS_S:.0f} s (timeout de la Saga: 3.5 s)")
            latencia_elegibilidad(LATENCIA_CAOS_S)
        else:
            print("  >> Fase CAÍDA: Elegibilidad responde HTTP 500")
            falla_elegibilidad(True)
        if numero_fase > 0:
            print(f"     Pausa de {pausa} s (igual con y sin patrón)"
                  + ("" if sin_patron else ": el circuito pasa a SEMIABIERTO y prueba una llamada"))
            time.sleep(pausa)
        llamadas_antes = estado_resiliencia()["bulkhead"]["admitidas"]
        inicio_fase = len(filas)
        for _ in range(cantidad):
            i = len(filas) + 1
            r = agendar(f"CB-{MARCA}-{int(sin_patron)}-{i}", jornada, f"cb-{MARCA}-{int(sin_patron)}-{i}")
            cb = estado_resiliencia()["circuit_breaker"]
            estado_cb = cb["estado"] if cb["activo"] else "DESACTIVADO"
            evento = ""
            if cb["activo"] and cb["aperturas"] > aperturas:
                evento = " (se abre)" if aperturas == 0 else " (la llamada de prueba falló: se reabre)"
                aperturas = cb["aperturas"]
            fila(i, r, f"  | circuito: {estado_cb}{evento}")
            filas.append({"n": i, "fase": fase, **r, "circuito": estado_cb})
        llamadas = estado_resiliencia()["bulkhead"]["admitidas"] - llamadas_antes
        segundos = sum(f["segundos"] for f in filas[inicio_fase:])
        por_fase.append({"fase": "caída" if fase == "caida" else fase, "solicitudes": cantidad, "segundos": round(segundos, 2),
                         "llamadas_elegibilidad": llamadas})

    resetear_caos()
    print(f"  >> Elegibilidad reparada. Pausa de {pausa} s (igual con y sin patrón) y una solicitud más")
    time.sleep(pausa)
    r = agendar(f"CB-{MARCA}-{int(sin_patron)}-rec", jornada, f"cb-{MARCA}-{int(sin_patron)}-rec")
    cb = estado_resiliencia()["circuit_breaker"]
    recuperacion = cb["estado"] if cb["activo"] else "DESACTIVADO"
    fila(n + 1, r, f"  | circuito: {recuperacion}" + ("" if sin_patron else " (llamada de prueba OK)"))
    filas.append({"n": n + 1, "fase": "reparada", **r, "circuito": recuperacion})

    for f in por_fase:
        print(f"  Fase {f['fase']:<6}: {f['solicitudes']} solicitudes en {f['segundos']:.2f} s | "
              f"llamadas que llegaron a Elegibilidad: {f['llamadas_elegibilidad']} de {f['solicitudes']}")
    latencias = [f["segundos"] for f in filas[:n]]
    total = sum(latencias)
    llamadas_total = sum(f["llamadas_elegibilidad"] for f in por_fase)
    print(f"  Total: {n} solicitudes en {total:.2f} s | promedio {statistics.mean(latencias):.2f} s | "
          f"{llamadas_total} llamadas a Elegibilidad")
    guardar_evidencia_resiliencia(f"3_circuito_{'sin' if sin_patron else 'con'}-patron", antes, desde)
    consistencia = verificar_consistencia(jornada)
    guardar_csv(f"3_circuito_{'sin' if sin_patron else 'con'}-patron", filas)
    fallback = sum(f["resultado"] == "PENDIENTE_VALIDACION" for f in filas[:n])
    detalle_fases = "; ".join(f"{f['fase']} {f['segundos']:.1f} s, {f['llamadas_elegibilidad']}/{f['solicitudes']} a Elegibilidad"
                              for f in por_fase)
    return {"escenario": f"3. {nombre}", "solicitudes": n,
            "resultado": f"{fallback}/{n} fallback | {detalle_fases}",
            "latencia_prom_s": round(statistics.mean(latencias), 3),
            "consistente": consistencia["consistente"],
            "ok": (fallback == n and consistencia["consistente"] and r["resultado"] == "CONFIRMADA"
                   and (sin_patron or recuperacion == "CERRADO"))}


def escenario_bulkhead(n: int = 60, sin_patron: bool = False, consultas: int = 5) -> Dict[str, Any]:
    nombre = "Bulkhead " + ("DESACTIVADO" if sin_patron else "ACTIVO")
    titulo(f"Escenario 3b: {n} solicitudes simultáneas con Elegibilidad lenta -> {nombre}")
    print("  (Circuit Breaker apagado en ambas corridas para aislar el efecto del Bulkhead)")
    levantar(cb=False, bulkhead=not sin_patron, recrear=("bff",))
    jornada = f"EXP-BH-{'SIN' if sin_patron else 'CON'}-{MARCA}"
    crear_jornada(jornada, n)
    antes, desde = estado_resiliencia(), marca_tiempo_docker()
    print(f"  >> Elegibilidad LENTA: {LATENCIA_CAOS_S:.0f} s por respuesta")
    latencia_elegibilidad(LATENCIA_CAOS_S)
    token()

    print(f"  >> Lanzando {n} agendamientos a la vez y, 0,5 s después, {consultas} consultas simultáneas")
    print("     a otra función (resumen de donante) por el gateway, más una directa en el BFF")
    with ThreadPoolExecutor(max_workers=n + consultas + 2) as pool:
        futuros = [pool.submit(agendar, f"BH-{MARCA}-{int(sin_patron)}-{i}", jornada,
                               f"bh-{MARCA}-{int(sin_patron)}-{i}") for i in range(n)]
        time.sleep(0.5)
        directo = pool.submit(resumen_directo_bff, "11.111.111-1")
        futuros_resumen = [pool.submit(resumen_donante, "11.111.111-1") for _ in range(consultas)]
        resultados = [f.result() for f in futuros]
        por_gateway = [f.result() for f in futuros_resumen]
        lat_bff = directo.result()

    metricas = estado_resiliencia()["bulkhead"]
    resetear_caos()
    latencias = [r["segundos"] for r in resultados]
    lat_gateway = [s for _, s in por_gateway]
    print(f"  Agendamientos: p50 {percentil(latencias, 50):.2f} s | p95 {percentil(latencias, 95):.2f} s | "
          f"máx {max(latencias):.2f} s | HTTP: "
          + ", ".join(f"{c}x{sum(r['http'] == c for r in resultados)}" for c in sorted({r['http'] for r in resultados})))
    print(f"  Resumen de donante durante la carga: por gateway {', '.join(f'{s:.2f} s' for s in lat_gateway)} "
          f"(promedio {statistics.mean(lat_gateway):.2f} s) | directo en el BFF {lat_bff:.2f} s")
    print(f"  Llamadas simultáneas a Elegibilidad (máximo observado): {metricas['max_en_curso_observado']}"
          + (f" (límite del Bulkhead: {metricas['max_concurrentes']}) | rechazadas al instante: {metricas['rechazadas']}"
             if not sin_patron else " (sin límite)"))
    guardar_evidencia_resiliencia(f"3b_bulkhead_{'sin' if sin_patron else 'con'}-patron", antes, desde)
    consistencia = verificar_consistencia(jornada)
    filas = [{"n": i + 1, **r} for i, r in enumerate(resultados)]
    filas += [{"n": f"resumen-gateway-{i + 1}", "http": c, "segundos": s, "resultado": "resumen"}
              for i, (c, s) in enumerate(por_gateway)]
    filas.append({"n": "resumen-bff-directo", "http": 200, "segundos": lat_bff, "resultado": "resumen"})
    guardar_csv(f"3b_bulkhead_{'sin' if sin_patron else 'con'}-patron", filas)
    contencion_ok = sin_patron or (metricas["max_en_curso_observado"] <= metricas["max_concurrentes"]
                                   and metricas["rechazadas"] > 0)
    return {"escenario": f"3b. {nombre}", "solicitudes": n,
            "resultado": f"p50 {percentil(latencias, 50):.2f} s, máx simultáneas {metricas['max_en_curso_observado']}, "
                         f"resumen prom. {statistics.mean(lat_gateway):.2f} s",
            "latencia_prom_s": round(statistics.mean(latencias), 3),
            "consistente": consistencia["consistente"],
            "ok": consistencia["consistente"] and contencion_ok and all(r["http"] == 201 for r in resultados)}


def escenario_ratelimit(n: int = 20) -> Dict[str, Any]:
    titulo(f"Escenario extra: ráfaga de {n} consultas contra el gateway (rate limit de "
           f"{RATE_LIMIT_POR_DEFECTO} por minuto)")
    levantar(rate_limit=RATE_LIMIT_POR_DEFECTO, recrear=("api-gateway",))
    token(nuevo=True)  # también cuenta como petición
    filas = []
    for i in range(n):
        t0 = time.perf_counter()
        codigo, datos, headers = _http("GET", f"{GATEWAY}/api/v1/donantes/11.111.111-1/resumen",
                                       headers={"Authorization": f"Bearer {token()}"})
        segundos = round(time.perf_counter() - t0, 3)
        reintentar = headers.get("Retry-After") or headers.get("retry-after") or ""
        filas.append({"n": i + 1, "http": codigo, "segundos": segundos, "retry_after": reintentar})
    aceptadas = sum(f["http"] == 200 for f in filas)
    rechazadas = sum(f["http"] == 429 for f in filas)
    primera = next((f for f in filas if f["http"] == 429), None)
    print(f"  Aceptadas (200): {aceptadas} | rechazadas (429): {rechazadas}"
          + (f" | primera 429 en la petición #{primera['n']}, Retry-After {primera['retry_after']} s" if primera else ""))
    guardar_csv("4_ratelimit", filas)
    return {"escenario": "Rate limit (gateway)", "solicitudes": n,
            "resultado": f"{aceptadas} aceptadas, {rechazadas} con 429",
            "latencia_prom_s": round(statistics.mean(f["segundos"] for f in filas), 3),
            "consistente": True, "ok": rechazadas > 0}


# ---------------------------------------------------------------- main

def imprimir_tabla(resumenes: List[Dict[str, Any]]) -> None:
    titulo("Resumen comparativo")
    print(f"  {'Escenario':<30} {'Prom. (s)':>9}  {'Consist.':<8}  {'OK':<3}  Resultado")
    for r in resumenes:
        print(f"  {r['escenario']:<30} {r['latencia_prom_s']:>9.3f}  "
              f"{'sí' if r['consistente'] else 'NO':<8}  {'sí' if r['ok'] else 'NO':<3}  {r['resultado']}")
    guardar_csv("resumen", resumenes)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Experimentos de resiliencia ABET 6")
    parser.add_argument("escenario",
                        choices=["feliz", "compensacion", "circuito", "bulkhead", "ratelimit", "todos"])
    parser.add_argument("--sin-patron", action="store_true", help="Desactiva el patrón evaluado (CB o Bulkhead)")
    parser.add_argument("--modo", choices=["lenta-caida", "latencia", "error"], default="lenta-caida",
                        help="Falla de Elegibilidad en el escenario circuito (por defecto: lenta y luego caída)")
    parser.add_argument("-n", type=int, help="Cantidad de solicitudes")
    args = parser.parse_args()
    kw = {"n": args.n} if args.n else {}

    RESULTADOS.mkdir(parents=True, exist_ok=True)
    salida = (RESULTADOS / "salida.txt").open("w", encoding="utf-8")
    sys.stdout = _Duplicador(sys.stdout, salida)
    print(f"Experimentos ABET 6 | {time.strftime('%Y-%m-%d %H:%M:%S')} | escenario: {args.escenario}"
          f"{' --sin-patron' if args.sin_patron else ''}")

    resumenes = []
    try:
        if args.escenario == "feliz":
            resumenes.append(escenario_feliz(**kw))
        elif args.escenario == "compensacion":
            resumenes.append(escenario_compensacion())
        elif args.escenario == "circuito":
            resumenes.append(escenario_circuito(sin_patron=args.sin_patron, modo=args.modo, **kw))
        elif args.escenario == "bulkhead":
            resumenes.append(escenario_bulkhead(sin_patron=args.sin_patron, **kw))
        elif args.escenario == "ratelimit":
            resumenes.append(escenario_ratelimit(**kw))
        else:
            resumenes += [
                escenario_feliz(),
                escenario_compensacion(),
                escenario_circuito(sin_patron=False),
                escenario_circuito(sin_patron=True),
                escenario_bulkhead(sin_patron=False),
                escenario_bulkhead(sin_patron=True),
                escenario_ratelimit(),
            ]
            imprimir_tabla(resumenes)
    finally:
        restaurar()
        print(f"Salida completa y archivos de la corrida en experimentos/resultados/{MARCA}/")
        sys.stdout = sys.stdout.consola
        salida.close()
    return 0 if resumenes and all(r["ok"] for r in resumenes) else 1


if __name__ == "__main__":
    sys.exit(main())
