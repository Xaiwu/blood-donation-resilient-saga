import asyncio
import logging
import math
import os
import threading
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, status

from src.models import ConfigurarFallaRequest, ConfigurarLatenciaRequest, EstadoCaosResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("elegibilidad-service")

LATENCIA_MAXIMA_SEGUNDOS = 60.0


class FallaSimuladaError(Exception):
    """Excepción lanzada cuando el simulador de caos tiene activa la caída del servicio (HTTP 5xx)."""

    def __init__(self, detalle: str = "Falla simulada en el servicio externo de elegibilidad.", codigo_falla: int = 500):
        super().__init__(detalle)
        self.codigo_falla = codigo_falla


def _parse_bool(valor: Optional[str], defecto: bool = False) -> bool:
    if valor is None:
        return defecto
    return valor.strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


def _parse_float(valor: Optional[str], defecto: float = 0.0) -> float:
    if valor is None:
        return defecto
    try:
        numero = float(valor.strip())
        if not math.isfinite(numero):
            return defecto
        return min(LATENCIA_MAXIMA_SEGUNDOS, max(0.0, numero))
    except ValueError:
        return defecto


def _parse_codigo_falla(valor: Optional[str], defecto: int = 500) -> int:
    if valor is None:
        return defecto
    try:
        codigo = int(valor.strip())
        return codigo if 500 <= codigo <= 599 else defecto
    except ValueError:
        return defecto


class _ControladorCaos:
    """Mantiene en memoria y de forma segura entre hilos los parámetros de inyección de fallas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._simular_falla: bool = _parse_bool(os.getenv("SIMULAR_FALLA"), False)
        self._latencia_segundos: float = _parse_float(os.getenv("LATENCIA_SEGUNDOS"), 0.0)
        self._codigo_falla: int = _parse_codigo_falla(os.getenv("CODIGO_FALLA_HTTP"), 500)

    def estado(self, mensaje: str = "Estado actual del simulador de caos.") -> Dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "simular_falla": self._simular_falla,
                "latencia_segundos": self._latencia_segundos,
                "codigo_falla": self._codigo_falla,
                "mensaje": mensaje,
            }

    def configurar_latencia(self, segundos: float) -> Dict[str, Any]:
        if not math.isfinite(segundos) or segundos < 0.0 or segundos > LATENCIA_MAXIMA_SEGUNDOS:
            raise ValueError(f"La latencia en segundos debe estar entre 0.0 y {LATENCIA_MAXIMA_SEGUNDOS}.")
        with self._lock:
            self._latencia_segundos = round(float(segundos), 3)
            actual = self._latencia_segundos
        logger.warning("[ELEGIBILIDAD] [CAOS] Latencia artificial actualizada a %.2fs", actual)
        return self.estado(f"Latencia configurada en {actual:.2f} segundos.")

    def configurar_falla(self, activa: bool = True, codigo_falla: int = 500) -> Dict[str, Any]:
        if not 500 <= codigo_falla <= 599:
            raise ValueError("El código de falla HTTP debe estar entre 500 y 599.")
        with self._lock:
            self._simular_falla = bool(activa)
            self._codigo_falla = int(codigo_falla)
        estado_str = "ACTIVADA" if activa else "DESACTIVADA"
        logger.warning("[ELEGIBILIDAD] [CAOS] Simulación de falla HTTP %d %s", codigo_falla, estado_str)
        return self.estado(f"Simulación de falla HTTP {codigo_falla} {estado_str.lower()}.")

    def resetear(self) -> Dict[str, Any]:
        with self._lock:
            self._simular_falla = False
            self._latencia_segundos = 0.0
            self._codigo_falla = 500
        logger.info("[ELEGIBILIDAD] [CAOS] Parámetros de caos restablecidos a operación normal")
        return self.estado("Configuración de caos restablecida a valores normales.")

    async def aplicar(self, id_saga: Optional[str] = None) -> None:
        with self._lock:
            latencia = self._latencia_segundos
            falla = self._simular_falla
            codigo = self._codigo_falla

        if latencia > 0:
            logger.warning("[ELEGIBILIDAD] [CAOS] Demorando respuesta %.2fs | saga=%s", latencia, id_saga or "N/A")
            await asyncio.sleep(latencia)

        if falla:
            logger.error("[ELEGIBILIDAD] [CAOS] Forzando falla HTTP %d | saga=%s", codigo, id_saga or "N/A")
            raise FallaSimuladaError(
                detalle=f"Servicio externo de elegibilidad no disponible (falla HTTP {codigo} simulada).",
                codigo_falla=codigo,
            )


_caos = _ControladorCaos()


def obtener_estado_caos() -> Dict[str, Any]:
    """Retorna la configuración vigente de latencia y falla simulada."""
    return _caos.estado()


def configurar_latencia(segundos: float) -> Dict[str, Any]:
    """Actualiza los segundos de demora artificial antes de responder."""
    return _caos.configurar_latencia(segundos)


def configurar_falla(activa: bool = True, codigo_falla: int = 500) -> Dict[str, Any]:
    """Activa o desactiva la simulación de caída HTTP 5xx."""
    return _caos.configurar_falla(activa=activa, codigo_falla=codigo_falla)


def resetear_caos() -> Dict[str, Any]:
    """Restablece latencia a 0s y desactiva fallas simuladas."""
    return _caos.resetear()


async def aplicar_caos(id_saga: Optional[str] = None) -> None:
    """Ejecuta la demora artificial asíncrona y/o lanza FallaSimuladaError según la configuración activa."""
    await _caos.aplicar(id_saga=id_saga)


router = APIRouter(tags=["Inyección de Caos"])


@router.get("/config", response_model=EstadoCaosResponse, status_code=status.HTTP_200_OK)
async def endpoint_obtener_config() -> EstadoCaosResponse:
    """Consulta el estado actual de latencia e inyección de fallas."""
    return EstadoCaosResponse(**obtener_estado_caos())


@router.post("/config/latencia", response_model=EstadoCaosResponse, status_code=status.HTTP_200_OK)
async def endpoint_configurar_latencia(payload: ConfigurarLatenciaRequest) -> EstadoCaosResponse:
    """Configura la demora artificial (en segundos) para probar Timeouts y Bulkhead."""
    try:
        return EstadoCaosResponse(**configurar_latencia(payload.latencia_segundos))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/config/falla", response_model=EstadoCaosResponse, status_code=status.HTTP_200_OK)
async def endpoint_configurar_falla(
    payload: Optional[ConfigurarFallaRequest] = None,
) -> EstadoCaosResponse:
    """Activa o desactiva la simulación de caída (HTTP 500/503) para probar Circuit Breaker y Fallback."""
    activa = payload.simular_falla if payload else True
    codigo = payload.codigo_falla if payload else 500
    try:
        return EstadoCaosResponse(**configurar_falla(activa=activa, codigo_falla=codigo))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/config/reset", response_model=EstadoCaosResponse, status_code=status.HTTP_200_OK)
async def endpoint_resetear_caos() -> EstadoCaosResponse:
    """Restablece el servicio a su operación normal sin latencia ni fallas."""
    return EstadoCaosResponse(**resetear_caos())
