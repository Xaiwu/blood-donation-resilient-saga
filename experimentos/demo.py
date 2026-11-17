"""
Experimentos de resiliencia (ABET 6): un comando por escenario.

Uso (con Docker Desktop abierto, desde la raíz del repositorio):

    python experimentos/demo.py feliz
    python experimentos/demo.py compensacion
    python experimentos/demo.py circuito                 # con Circuit Breaker
    python experimentos/demo.py circuito --sin-patron    # mismo experimento sin CB
    python experimentos/demo.py bulkhead                 # con Bulkhead
    python experimentos/demo.py bulkhead --sin-patron    # mismo experimento sin Bulkhead
    python experimentos/demo.py todos                    # todo lo anterior + tabla comparativa

El script levanta el sistema con la configuración que necesita cada escenario,
activa las fallas con los endpoints de caos (por `docker compose exec`, sin
exponer puertos), manda las solicitudes por el gateway con JWT, verifica la
consistencia entre Cupos y Registro y guarda los resultados en
experimentos/resultados/*.csv. Solo usa la biblioteca estándar de Python.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RAIZ = Path(__file__).resolve().parent.parent
RESULTADOS = Path(__file__).resolve().parent / "resultados"
GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
USUARIO = os.getenv("USUARIO_ESCRITURA", "operador")
CLAVE = os.getenv("CLAVE_ESCRITURA", "operador123")

LATENCIA_CAOS_S = 5.0        # mayor que SAGA_STEP_TIMEOUT (3.5 s): la Saga corta por timeout
CB_RECUPERACION_S = 10       # valor por defecto en docker-compose.yml
RUT_NO_APTO = "00.000.000-0"  # historial clínico no apto en el mock de Elegibilidad

MARCA = time.strftime("%Y%m%d-%H%M%S")
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
        "print(urllib.request.urlopen(req, timeout=10).read().decode())"
    )
    return json.loads(python_en(servicio, codigo))


def levantar(cb: bool = True, bulkhead: bool = True, reiniciar_bff: bool = False) -> None:
    """Levanta el sistema con los patrones pedidos y sin rate limit que limite la carga."""
    print(f"  Configurando sistema: Circuit Breaker={'ON' if cb else 'OFF'}, "
          f"Bulkhead={'ON' if bulkhead else 'OFF'}...")
    env = {
        "CB_ACTIVO": str(cb).lower(),
        "BULKHEAD_ACTIVO": str(bulkhead).lower(),
        "RATE_LIMIT_RPM": "100000",
    }
    global _CONSTRUIDO
    compose("up", "-d", "--wait", *([] if _CONSTRUIDO else ["--build"]), env=env)
    _CONSTRUIDO = True
    if reiniciar_bff:  # contadores del CB y del Bulkhead en cero
        compose("up", "-d", "--wait", "--force-recreate", "bff", env=env)
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


def estado_resiliencia() -> Dict[str, Any]:
    return http_interno("bff", 5000, "GET", "/resiliencia/estado")


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


# ---------------------------------------------------------------- cliente del gateway

def _http(metodo: str, url: str, cuerpo: Optional[dict] = None, headers: Optional[dict] = None,
          timeout: float = 60) -> Tuple[int, Any]:
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        cuerpo_err = e.read().decode()
        try:
            return e.code, json.loads(cuerpo_err)
        except ValueError:
            return e.code, cuerpo_err


_TOKEN: Optional[str] = None


def token() -> str:
    global _TOKEN
    if _TOKEN is None:
        codigo, datos = _http("POST", f"{GATEWAY}/auth/token", {"usuario": USUARIO, "clave": CLAVE})
        if codigo != 200:
            raise RuntimeError(f"No se pudo obtener el JWT ({codigo}): {datos}")
        _TOKEN = datos["access_token"]
    return _TOKEN


def agendar(rut: str, id_jornada: str, clave: str) -> Dict[str, Any]:
    t0 = time.perf_counter()
    codigo, datos = _http(
        "POST", f"{GATEWAY}/api/v1/donaciones/agendar",
        {"rut_donante": rut, "nombre_donante": "Donante Experimento", "id_jornada": id_jornada,
         "horario": "10:00", "edad": 30, "peso_kg": 70.0},
        {"Authorization": f"Bearer {token()}", "Idempotency-Key": clave},
    )
    segundos = time.perf_counter() - t0
    if isinstance(datos, dict) and "estado" in datos:
        resultado = datos["estado"]
    else:
        detalle = datos.get("detail") if isinstance(datos, dict) else datos
        resultado = str(detalle)
    return {"http": codigo, "segundos": round(segundos, 3), "resultado": resultado,
            "id_saga": datos.get("id_saga") if isinstance(datos, dict) else None}


def resumen_donante(rut: str) -> Tuple[int, float]:
    t0 = time.perf_counter()
    codigo, _ = _http("GET", f"{GATEWAY}/api/v1/donantes/{rut}/resumen",
                      headers={"Authorization": f"Bearer {token()}"})
    return codigo, round(time.perf_counter() - t0, 3)


# ---------------------------------------------------------------- utilidades de salida

def guardar_csv(nombre: str, filas: List[Dict[str, Any]]) -> Path:
    RESULTADOS.mkdir(exist_ok=True)
    ruta = RESULTADOS / f"{nombre}_{MARCA}.csv"
    with ruta.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        escritor.writeheader()
        escritor.writerows(filas)
    print(f"  Resultados guardados en {ruta.relative_to(RAIZ)}")
    return ruta


def percentil(valores: List[float], p: float) -> float:
    ordenados = sorted(valores)
    indice = min(len(ordenados) - 1, max(0, round(p / 100 * len(ordenados)) - 1))
    return ordenados[indice]


def titulo(texto: str) -> None:
    print(f"\n{'=' * 72}\n{texto}\n{'=' * 72}")


def corto(texto: str) -> str:
    """Deja visible el motivo y el resultado de la compensación en una línea."""
    if "Transacción compensada" in texto:
        motivo = texto.split(":")[0].split(". ")[0][:60]
        return f"{motivo} -> compensada, cupo liberado"
    if "Atención" in texto:
        return f"{texto.split(':')[0][:40]} -> COMPENSACIÓN FALLIDA"
    return texto[:70]


def fila(i: int, r: Dict[str, Any], extra: str = "") -> None:
    print(f"  #{i:<3} HTTP {r['http']}  {r['segundos']:>6.2f} s  {corto(r['resultado'])}{extra}")


# ---------------------------------------------------------------- escenarios

def escenario_feliz(n: int = 5) -> Dict[str, Any]:
    titulo("Escenario 1: Camino feliz")
    levantar()
    jornada = f"EXP-FELIZ-{MARCA}"
    crear_jornada(jornada, n)
    filas = []
    for i in range(n):
        r = agendar(f"FELIZ-{MARCA}-{i}", jornada, f"feliz-{MARCA}-{i}")
        fila(i + 1, r)
        filas.append({"n": i + 1, **r})

    reintento = agendar(f"FELIZ-{MARCA}-0", jornada, f"feliz-{MARCA}-0")
    misma = reintento["id_saga"] == filas[0]["id_saga"]
    print(f"  Reintento con la misma Idempotency-Key: HTTP {reintento['http']} "
          f"{'-> misma cita, sin duplicar' if misma else '-> [X] distinta'}")

    consistencia = verificar_consistencia(jornada)
    guardar_csv("feliz", filas)
    ok = all(f["resultado"] == "CONFIRMADA" for f in filas) and misma and consistencia["consistente"]
    return {"escenario": "1. Camino feliz", "solicitudes": n,
            "resultado": f"{sum(f['resultado'] == 'CONFIRMADA' for f in filas)}/{n} CONFIRMADA",
            "latencia_prom_s": round(statistics.mean(f["segundos"] for f in filas), 3),
            "consistente": consistencia["consistente"], "ok": ok}


def escenario_compensacion(n: int = 3) -> Dict[str, Any]:
    titulo("Escenario 2: Falla en Registro -> compensación inversa (se libera el cupo)")
    levantar()
    jornada = f"EXP-COMP-{MARCA}"
    crear_jornada(jornada, n)
    filas = []

    print("  >> Inyectando falla HTTP 500 en registro-service")
    falla_registro(True)
    for i in range(n):
        r = agendar(f"COMP-{MARCA}-{i}", jornada, f"comp-{MARCA}-{i}")
        fila(i + 1, r)
        filas.append({"n": i + 1, "caso": "falla_registro", **r})

    print(f"  >> Donante no apto ({RUT_NO_APTO}): Elegibilidad responde 422")
    r = agendar(RUT_NO_APTO, jornada, f"comp-noapto-{MARCA}")
    fila(n + 1, r)
    filas.append({"n": n + 1, "caso": "no_apto", **r})

    consistencia = verificar_consistencia(jornada)
    print("  >> Quitando la falla y agendando de nuevo")
    falla_registro(False)
    r = agendar(f"COMP-{MARCA}-ok", jornada, f"comp-{MARCA}-ok")
    fila(n + 2, r)
    filas.append({"n": n + 2, "caso": "recuperado", **r})
    final = verificar_consistencia(jornada)

    guardar_csv("compensacion", filas)
    compensadas = sum("compensada" in f["resultado"] for f in filas[:-1])
    ok = (consistencia["disponibles"] == consistencia["capacidad"] and consistencia["consistente"]
          and final["consistente"] and filas[-1]["resultado"] == "CONFIRMADA")
    return {"escenario": "2. Compensación", "solicitudes": n + 2,
            "resultado": f"{compensadas}/{n + 1} compensadas, stock restituido",
            "latencia_prom_s": round(statistics.mean(f["segundos"] for f in filas), 3),
            "consistente": consistencia["consistente"] and final["consistente"], "ok": ok}


def escenario_circuito(n: int = 10, sin_patron: bool = False, modo: str = "latencia") -> Dict[str, Any]:
    nombre = "Circuit Breaker " + ("DESACTIVADO" if sin_patron else "ACTIVO")
    titulo(f"Escenario 3: Elegibilidad cae ({modo}) -> {nombre}")
    levantar(cb=not sin_patron, bulkhead=True, reiniciar_bff=True)
    jornada = f"EXP-CB-{'SIN' if sin_patron else 'CON'}-{MARCA}"
    crear_jornada(jornada, n + 1)

    if modo == "latencia":
        print(f"  >> Inyectando latencia de {LATENCIA_CAOS_S:.0f} s en elegibilidad-service "
              "(timeout de la Saga: 3.5 s)")
        latencia_elegibilidad(LATENCIA_CAOS_S)
    else:
        print("  >> Inyectando falla HTTP 500 en elegibilidad-service")
        falla_elegibilidad(True)

    filas = []
    for i in range(n):
        r = agendar(f"CB-{MARCA}-{int(sin_patron)}-{i}", jornada, f"cb-{MARCA}-{int(sin_patron)}-{i}")
        cb = estado_resiliencia()["circuit_breaker"]
        estado_cb = cb["estado"] if cb["activo"] else "DESACTIVADO"
        fila(i + 1, r, f"  | circuito: {estado_cb}")
        filas.append({"n": i + 1, **r, "circuito": estado_cb})

    metricas = estado_resiliencia()["circuit_breaker"]
    resetear_caos()
    recuperacion = None
    if not sin_patron:
        print(f"  >> Falla reparada. Esperando {CB_RECUPERACION_S} s de recuperación del circuito...")
        time.sleep(CB_RECUPERACION_S + 1)
        r = agendar(f"CB-{MARCA}-rec", jornada, f"cb-{MARCA}-rec")
        recuperacion = estado_resiliencia()["circuit_breaker"]["estado"]
        fila(n + 1, r, f"  | circuito: {recuperacion} (llamada de prueba OK)")
        filas.append({"n": n + 1, **r, "circuito": recuperacion})

    latencias = [f["segundos"] for f in filas[:n]]
    total = sum(latencias)
    print(f"  Tiempo total de {n} solicitudes: {total:.2f} s | promedio: {statistics.mean(latencias):.2f} s")
    if not sin_patron:
        print(f"  Llamadas reales a Elegibilidad: {metricas['llamadas']} | "
              f"rechazadas al instante por el circuito: {metricas['rechazos_fail_fast']}")
    consistencia = verificar_consistencia(jornada)
    guardar_csv(f"circuito_{'sin' if sin_patron else 'con'}-patron", filas)
    fallback = sum(f["resultado"] == "PENDIENTE_VALIDACION" for f in filas[:n])
    return {"escenario": f"3. {nombre}", "solicitudes": n,
            "resultado": f"{fallback}/{n} fallback, total {total:.1f} s",
            "latencia_prom_s": round(statistics.mean(latencias), 3),
            "consistente": consistencia["consistente"],
            "ok": fallback == n and consistencia["consistente"]
                  and (sin_patron or recuperacion == "CERRADO")}


def escenario_bulkhead(n: int = 40, sin_patron: bool = False) -> Dict[str, Any]:
    nombre = "Bulkhead " + ("DESACTIVADO" if sin_patron else "ACTIVO")
    titulo(f"Escenario 3b: {n} solicitudes simultáneas con Elegibilidad lenta -> {nombre}")
    # El CB se apaga para aislar el efecto del Bulkhead.
    levantar(cb=False, bulkhead=not sin_patron, reiniciar_bff=True)
    jornada = f"EXP-BH-{'SIN' if sin_patron else 'CON'}-{MARCA}"
    crear_jornada(jornada, n)
    print(f"  >> Inyectando latencia de {LATENCIA_CAOS_S:.0f} s en elegibilidad-service")
    latencia_elegibilidad(LATENCIA_CAOS_S)
    token()

    print(f"  >> Lanzando {n} agendamientos a la vez y midiendo otra función (resumen de donante)")
    with ThreadPoolExecutor(max_workers=n + 1) as pool:
        futuros = [pool.submit(agendar, f"BH-{MARCA}-{int(sin_patron)}-{i}", jornada,
                               f"bh-{MARCA}-{int(sin_patron)}-{i}") for i in range(n)]
        time.sleep(0.5)
        resumenes = [resumen_donante("11.111.111-1") for _ in range(3)]
        resultados = [f.result() for f in futuros]

    metricas = estado_resiliencia()["bulkhead"]
    resetear_caos()
    latencias = [r["segundos"] for r in resultados]
    lat_resumen = [s for _, s in resumenes]
    print(f"  Agendamientos: p50 {percentil(latencias, 50):.2f} s | p95 {percentil(latencias, 95):.2f} s | "
          f"máx {max(latencias):.2f} s | HTTP: "
          + ", ".join(f"{c}x{sum(r['http'] == c for r in resultados)}" for c in sorted({r['http'] for r in resultados})))
    print(f"  Resumen de donante durante la carga: {', '.join(f'{s:.2f} s' for s in lat_resumen)}")
    if not sin_patron:
        print(f"  Bulkhead: máximo {metricas['max_concurrentes']} en curso | admitidas {metricas['admitidas']} | "
              f"rechazadas al instante {metricas['rechazadas']}")
    consistencia = verificar_consistencia(jornada)
    filas = [{"n": i + 1, **r} for i, r in enumerate(resultados)]
    filas += [{"n": f"resumen-{i + 1}", "http": c, "segundos": s, "resultado": "resumen", "id_saga": ""}
              for i, (c, s) in enumerate(resumenes)]
    guardar_csv(f"bulkhead_{'sin' if sin_patron else 'con'}-patron", filas)
    return {"escenario": f"3b. {nombre}", "solicitudes": n,
            "resultado": f"p95 {percentil(latencias, 95):.2f} s, resumen máx {max(lat_resumen):.2f} s",
            "latencia_prom_s": round(statistics.mean(latencias), 3),
            "consistente": consistencia["consistente"],
            "ok": consistencia["consistente"] and all(r["http"] == 201 for r in resultados)}


# ---------------------------------------------------------------- main

def imprimir_tabla(resumenes: List[Dict[str, Any]]) -> None:
    titulo("Resumen comparativo")
    print(f"  {'Escenario':<32} {'Prom. (s)':>9}  {'Consistente':<11}  Resultado")
    for r in resumenes:
        print(f"  {r['escenario']:<32} {r['latencia_prom_s']:>9.3f}  "
              f"{'sí' if r['consistente'] else 'NO':<11}  {r['resultado']}")
    guardar_csv("resumen", resumenes)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Experimentos de resiliencia ABET 6")
    parser.add_argument("escenario", choices=["feliz", "compensacion", "circuito", "bulkhead", "todos"])
    parser.add_argument("--sin-patron", action="store_true", help="Desactiva el patrón evaluado (CB o Bulkhead)")
    parser.add_argument("--modo", choices=["latencia", "error"], default="latencia",
                        help="Tipo de falla en Elegibilidad para el escenario circuito")
    parser.add_argument("-n", type=int, help="Cantidad de solicitudes")
    args = parser.parse_args()
    kw = {"n": args.n} if args.n else {}

    resumenes = []
    try:
        if args.escenario == "feliz":
            resumenes.append(escenario_feliz(**kw))
        elif args.escenario == "compensacion":
            resumenes.append(escenario_compensacion(**kw))
        elif args.escenario == "circuito":
            resumenes.append(escenario_circuito(sin_patron=args.sin_patron, modo=args.modo, **kw))
        elif args.escenario == "bulkhead":
            resumenes.append(escenario_bulkhead(sin_patron=args.sin_patron, **kw))
        else:
            resumenes += [
                escenario_feliz(),
                escenario_compensacion(),
                escenario_circuito(sin_patron=False),
                escenario_circuito(sin_patron=True),
                escenario_bulkhead(sin_patron=False),
                escenario_bulkhead(sin_patron=True),
            ]
            imprimir_tabla(resumenes)
    finally:
        restaurar()
    return 0 if all(r["consistente"] for r in resumenes) else 1


if __name__ == "__main__":
    sys.exit(main())
