"""
Inyección de fallas en registro-service para demostrar la compensación de la Saga.

Dos modos, configurables en caliente:
- Falla: POST /registro/donaciones responde 5xx sin escribir en la base. La Saga sabe
  que el registro no ocurrió y solo libera el cupo.
- Respuesta tardía: el registro SÍ se graba, pero la respuesta tarda N segundos. Si N
  supera el timeout de la Saga, el resultado queda incierto y la Saga compensa en orden
  inverso: primero anula el registro y después libera el cupo.

La anulación (compensación), las consultas y /health no se ven afectadas, para que la
Saga pueda compensar y el contenedor siga sano durante la demo.
"""

import logging
import math
import os
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("registro-service")

LATENCIA_MAXIMA_SEGUNDOS = 60.0


class FallaSimuladaError(Exception):
    """La simulación de falla está activa: el registro responde 5xx sin persistir."""

    def __init__(self, codigo_falla: int = 500):
        super().__init__(f"Servicio de registro no disponible (falla HTTP {codigo_falla} simulada).")
        self.codigo_falla = codigo_falla


def _parse_bool(valor: Optional[str]) -> bool:
    return (valor or "").strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


def _parse_codigo_falla(valor: Optional[str]) -> int:
    try:
        codigo = int((valor or "").strip())
    except ValueError:
        return 500
    return codigo if 500 <= codigo <= 599 else 500


def _parse_latencia(valor: Optional[str]) -> float:
    try:
        numero = float((valor or "").strip())
    except ValueError:
        return 0.0
    if not math.isfinite(numero):
        return 0.0
    return min(LATENCIA_MAXIMA_SEGUNDOS, max(0.0, numero))


class _ControladorFalla:
    """Estado de las fallas simuladas, protegido entre hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._simular_falla = _parse_bool(os.getenv("SIMULAR_FALLA"))
        self._codigo_falla = _parse_codigo_falla(os.getenv("CODIGO_FALLA_HTTP"))
        self._latencia_respuesta = _parse_latencia(os.getenv("LATENCIA_RESPUESTA_SEGUNDOS"))

    def estado(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "simular_falla": self._simular_falla,
                "codigo_falla": self._codigo_falla,
                "latencia_respuesta_segundos": self._latencia_respuesta,
            }

    def configurar(self, activa: bool, codigo_falla: int) -> Dict[str, Any]:
        with self._lock:
            self._simular_falla = activa
            self._codigo_falla = codigo_falla
        logger.warning(
            "[REGISTRO] [CAOS] Falla HTTP %d %s", codigo_falla, "ACTIVADA" if activa else "DESACTIVADA"
        )
        return self.estado()

    def configurar_latencia(self, segundos: float) -> Dict[str, Any]:
        with self._lock:
            self._latencia_respuesta = round(float(segundos), 3)
        logger.warning("[REGISTRO] [CAOS] Respuesta tardía tras grabar: %.2fs", segundos)
        return self.estado()

    def resetear(self) -> Dict[str, Any]:
        with self._lock:
            self._simular_falla = False
            self._codigo_falla = 500
            self._latencia_respuesta = 0.0
        logger.info("[REGISTRO] [CAOS] Operación normal restablecida")
        return self.estado()

    def aplicar(self, id_saga: Optional[str] = None) -> None:
        with self._lock:
            falla, codigo = self._simular_falla, self._codigo_falla
        if falla:
            logger.error("[REGISTRO] [CAOS] Forzando falla HTTP %d | saga=%s", codigo, id_saga or "N/A")
            raise FallaSimuladaError(codigo)

    def demorar_respuesta(self, id_saga: Optional[str] = None) -> None:
        with self._lock:
            latencia = self._latencia_respuesta
        if latencia > 0:
            logger.warning(
                "[REGISTRO] [CAOS] Registro grabado; demorando la respuesta %.2fs | saga=%s",
                latencia, id_saga or "N/A",
            )
            time.sleep(latencia)


_falla = _ControladorFalla()


def aplicar_falla(id_saga: Optional[str] = None) -> None:
    """Lanza FallaSimuladaError si la simulación de falla está activa (antes de grabar)."""
    _falla.aplicar(id_saga)


def demorar_respuesta(id_saga: Optional[str] = None) -> None:
    """Demora la respuesta después de grabar, si la respuesta tardía está activa."""
    _falla.demorar_respuesta(id_saga)


def resetear_falla() -> Dict[str, Any]:
    return _falla.resetear()


class ConfigurarFallaRequest(BaseModel):
    simular_falla: bool = Field(..., description="True para que el registro responda 5xx")
    codigo_falla: int = Field(default=500, ge=500, le=599, description="Código HTTP 5xx a simular")


class ConfigurarLatenciaRequest(BaseModel):
    latencia_segundos: float = Field(
        ..., ge=0.0, le=LATENCIA_MAXIMA_SEGUNDOS,
        description="Segundos que tarda la respuesta después de grabar el registro",
    )


class EstadoFallaResponse(BaseModel):
    simular_falla: bool
    codigo_falla: int
    latencia_respuesta_segundos: float


router = APIRouter(tags=["Inyección de fallas"])


@router.get("/config", response_model=EstadoFallaResponse)
async def endpoint_obtener_config() -> EstadoFallaResponse:
    """Consulta las fallas simuladas activas."""
    return EstadoFallaResponse(**_falla.estado())


@router.post("/config/falla", response_model=EstadoFallaResponse)
async def endpoint_configurar_falla(payload: ConfigurarFallaRequest) -> EstadoFallaResponse:
    """Activa o desactiva la falla 5xx en POST /registro/donaciones (no graba)."""
    return EstadoFallaResponse(**_falla.configurar(payload.simular_falla, payload.codigo_falla))


@router.post("/config/latencia", response_model=EstadoFallaResponse)
async def endpoint_configurar_latencia(payload: ConfigurarLatenciaRequest) -> EstadoFallaResponse:
    """POST /registro/donaciones graba y luego tarda N segundos en responder (0 lo desactiva)."""
    return EstadoFallaResponse(**_falla.configurar_latencia(payload.latencia_segundos))


@router.post("/config/reset", response_model=EstadoFallaResponse)
async def endpoint_resetear() -> EstadoFallaResponse:
    """Vuelve a la operación normal."""
    return EstadoFallaResponse(**_falla.resetear())
