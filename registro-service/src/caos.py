"""
Inyección de fallas en registro-service para demostrar la compensación de la Saga.

Con la falla activa, POST /registro/donaciones responde 5xx sin escribir en la base.
La anulación (compensación), las consultas y /health no se ven afectadas, para que
la Saga pueda compensar y el contenedor siga sano durante la demo.
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("registro-service")


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


class _ControladorFalla:
    """Estado de la falla simulada, protegido entre hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._simular_falla = _parse_bool(os.getenv("SIMULAR_FALLA"))
        self._codigo_falla = _parse_codigo_falla(os.getenv("CODIGO_FALLA_HTTP"))

    def estado(self) -> Dict[str, Any]:
        with self._lock:
            return {"simular_falla": self._simular_falla, "codigo_falla": self._codigo_falla}

    def configurar(self, activa: bool, codigo_falla: int) -> Dict[str, Any]:
        with self._lock:
            self._simular_falla = activa
            self._codigo_falla = codigo_falla
        logger.warning(
            "[REGISTRO] [CAOS] Falla HTTP %d %s", codigo_falla, "ACTIVADA" if activa else "DESACTIVADA"
        )
        return self.estado()

    def resetear(self) -> Dict[str, Any]:
        return self.configurar(False, 500)

    def aplicar(self, id_saga: Optional[str] = None) -> None:
        with self._lock:
            falla, codigo = self._simular_falla, self._codigo_falla
        if falla:
            logger.error("[REGISTRO] [CAOS] Forzando falla HTTP %d | saga=%s", codigo, id_saga or "N/A")
            raise FallaSimuladaError(codigo)


_falla = _ControladorFalla()


def aplicar_falla(id_saga: Optional[str] = None) -> None:
    """Lanza FallaSimuladaError si la simulación está activa."""
    _falla.aplicar(id_saga)


def resetear_falla() -> Dict[str, Any]:
    return _falla.resetear()


class ConfigurarFallaRequest(BaseModel):
    simular_falla: bool = Field(..., description="True para que el registro responda 5xx")
    codigo_falla: int = Field(default=500, ge=500, le=599, description="Código HTTP 5xx a simular")


class EstadoFallaResponse(BaseModel):
    simular_falla: bool
    codigo_falla: int


router = APIRouter(tags=["Inyección de fallas"])


@router.get("/config", response_model=EstadoFallaResponse)
async def endpoint_obtener_config() -> EstadoFallaResponse:
    """Consulta si la falla simulada está activa."""
    return EstadoFallaResponse(**_falla.estado())


@router.post("/config/falla", response_model=EstadoFallaResponse)
async def endpoint_configurar_falla(payload: ConfigurarFallaRequest) -> EstadoFallaResponse:
    """Activa o desactiva la falla 5xx en POST /registro/donaciones."""
    return EstadoFallaResponse(**_falla.configurar(payload.simular_falla, payload.codigo_falla))


@router.post("/config/reset", response_model=EstadoFallaResponse)
async def endpoint_resetear() -> EstadoFallaResponse:
    """Vuelve a la operación normal."""
    return EstadoFallaResponse(**_falla.resetear())
