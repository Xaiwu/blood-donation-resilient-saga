"""Eventos de resiliencia como líneas JSON en el log.

Cada cambio de estado del circuito y cada rechazo del bulkhead queda como una
línea ``{"evento": ..., "ts": ...}`` en la salida del contenedor, para usar
``docker compose logs`` como evidencia de los experimentos.
"""

import json
import logging
import time


def registrar_evento(logger: logging.Logger, evento: str, **campos) -> None:
    datos = {"evento": evento, "ts": round(time.time(), 3)}
    datos.update({k: v for k, v in campos.items() if v is not None})
    logger.info(json.dumps(datos, ensure_ascii=False))
