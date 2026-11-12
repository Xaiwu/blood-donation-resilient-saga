"""
Orquestador de la SAGA para la operación central 'Agendar una Donación'.
Coordina Cupos, Elegibilidad y Registro aplicando compensación inversa (LIFO).
"""
import logging
import os
import uuid
from typing import Any, Callable, Dict, List, Tuple

import requests

try:
    from fastapi import HTTPException, status
except ImportError:
    # Fallback para permitir pruebas unitarias locales independientes del framework web
    class HTTPException(Exception):  # type: ignore
        """Excepción HTTP simulada."""
        def __init__(self, status_code: int, detail: str = ""):
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    class status:  # type: ignore  # pylint: disable=invalid-name,too-few-public-methods
        """Códigos de estado HTTP simulados."""
        HTTP_200_OK = 200
        HTTP_201_CREATED = 201
        HTTP_409_CONFLICT = 409
        HTTP_422_UNPROCESSABLE_ENTITY = 422
        HTTP_500_INTERNAL_SERVER_ERROR = 500
        HTTP_503_SERVICE_UNAVAILABLE = 503

logger = logging.getLogger("bff.saga")

CUPOS_SERVICE_URL = os.getenv("CUPOS_SERVICE_URL", "http://cupos-service:5001").rstrip("/")
ELEGIBILIDAD_SERVICE_URL = os.getenv(
    "ELEGIBILIDAD_SERVICE_URL", "http://elegibilidad-service:5002"
).rstrip("/")
REGISTRO_SERVICE_URL = os.getenv(
    "REGISTRO_SERVICE_URL", "http://registro-service:5003"
).rstrip("/")

TIMEOUT_SEGUNDOS = float(os.getenv("SAGA_STEP_TIMEOUT", "3.5"))


def derivar_id_saga(idempotency_key: str | None) -> str:
    """
    Convierte la clave del cliente en un ID determinista para toda la Saga.
    Si la clave no se provee, genera un UUIDv4 aleatorio.
    Si es un UUID válido, lo conserva. De lo contrario, genera un UUIDv5 determinista.
    """
    if not idempotency_key or not idempotency_key.strip():
        return str(uuid.uuid4())
    clave = idempotency_key.strip()
    try:
        return str(uuid.UUID(clave))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, clave))


def comp_liberar_cupo(payload: Dict[str, Any], headers: Dict[str, str]) -> None:
    """Acción compensatoria del Paso 1: libera el cupo previamente reservado."""
    url = f"{CUPOS_SERVICE_URL}/cupos/liberar"
    id_saga = headers.get("X-Saga-ID", "desconocido")
    logger.info("[SAGA %s] Compensando: Liberando cupo en %s", id_saga, url)
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
        if r.status_code in (200, 204):
            logger.info("[SAGA %s] Cupo liberado exitosamente en compensación", id_saga)
        else:
            logger.error(
                "[SAGA %s] Error al compensar cupo (%d): %s",
                id_saga, r.status_code, r.text
            )
    except requests.RequestException as e:
        logger.critical(
            "[SAGA %s] Fallo al contactar cupos durante compensación: %s",
            id_saga, str(e)
        )


def _compensar(hechas: List[Tuple[str, Callable[[], None]]], id_saga: str) -> None:
    """
    Ejecuta las compensaciones registradas en ORDEN ESTRICTAMENTE INVERSO (LIFO),
    siguiendo el patrón de orquestación visto en cátedra.
    """
    logger.warning(
        "[SAGA %s] Ejecutando compensaciones inversas (%d pendientes)...",
        id_saga, len(hechas)
    )
    for nombre, accion in reversed(hechas):
        try:
            logger.info("[SAGA %s] Ejecutando compensación: %s", id_saga, nombre)
            accion()
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[SAGA %s] Error al ejecutar compensación %s: %s", id_saga, nombre, str(e))


def paso_reservar_cupo(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Paso 1: Reservar cupo de donación en la jornada."""
    url = f"{CUPOS_SERVICE_URL}/cupos/reservar"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
    except requests.RequestException as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Servicio de cupos no disponible: {str(e)}",
        ) from e

    if r.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No hay cupos disponibles para la jornada: {r.text}",
        )
    return r.json() if r.content else {}


def paso_validar_elegibilidad(
    payload: Dict[str, Any], headers: Dict[str, str]
) -> tuple[str, Dict[str, Any]]:
    """
    Paso 2: Validar la aptitud médica del donante en el servicio externo frágil.
    Retorna (estado_elegibilidad, datos_respuesta).
    Si el donante no es apto (422), lanza HTTPException(422) para disparar compensación.
    Si el validador cae o da timeout, degrada a 'PENDIENTE_VALIDACION' sin colgarse.
    """
    url = f"{ELEGIBILIDAD_SERVICE_URL}/elegibilidad/validar"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
        if r.status_code == 422:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Donante no apto clínicamente para donación",
            )
        if r.status_code in (500, 502, 503, 504):
            logger.warning(
                "Elegibilidad respondió con error (%d). Degradando estado",
                r.status_code
            )
            return "PENDIENTE_VALIDACION", {}

        return "APTO", (r.json() if r.content else {})
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.warning(
            "Elegibilidad inestable/caída (%s). Degradando a PENDIENTE_VALIDACION",
            type(e).__name__
        )
        return "PENDIENTE_VALIDACION", {}


def paso_registrar_donacion(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Paso 3: Registrar la donación agendada en firme en la base de datos de Registro."""
    url = f"{REGISTRO_SERVICE_URL}/registro/donaciones"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
    except requests.RequestException as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Falla de conectividad con registro-service: {str(e)}",
        ) from e

    if r.status_code not in (200, 201):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Falla al registrar cita en registro-service ({r.status_code}): {r.text}",
        )
    return r.json() if r.content else {}


def ejecutar_saga_agendamiento(
    payload: Dict[str, Any],
    id_saga: str,
    user_id: str | None = None,
    user_role: str | None = None,
) -> Dict[str, Any]:
    """
    Coordina la transacción distribuida 'Agendar una Donación' entre 3 servicios:
      1. Cupos: Reservar cupo (compensación: liberar cupo).
      2. Elegibilidad: Validar aptitud (degrada a PENDIENTE_VALIDACION si cae).
      3. Registro: Registrar donación en firme.

    Si el Paso 3 (o el Paso 2 por no apto) falla, ejecuta las COMPENSACIONES en orden inverso.
    """
    headers_internos = {
        "X-Saga-ID": id_saga,
        "X-Correlation-ID": id_saga,
        "X-User-Id": user_id or "sistema",
        "X-User-Role": user_role or "desconocido",
    }
    hechas: List[Tuple[str, Callable[[], None]]] = []

    logger.info("[SAGA %s] Iniciando saga para RUT %s", id_saga, payload.get("rut_donante"))

    try:
        # PASO 1: Reservar Cupo
        logger.info("[SAGA %s] Paso 1 -> Reservando cupo...", id_saga)
        paso_reservar_cupo(payload, headers_internos)
        hechas.append(("liberar_cupo", lambda: comp_liberar_cupo(payload, headers_internos)))

        # PASO 2: Validar Elegibilidad (Dependencia externa)
        logger.info("[SAGA %s] Paso 2 -> Validando elegibilidad médica...", id_saga)
        estado_eleg, _ = paso_validar_elegibilidad(payload, headers_internos)

        # PASO 3: Registrar Donación
        logger.info("[SAGA %s] Paso 3 -> Registrando cita médica...", id_saga)
        payload_registro = {
            **payload,
            "id_saga": id_saga,
            "estado_elegibilidad": estado_eleg,
        }
        datos_registro = paso_registrar_donacion(payload_registro, headers_internos)

        estado_final = "CONFIRMADA" if estado_eleg == "APTO" else "PENDIENTE_VALIDACION"
        logger.info("[SAGA %s] SAGA COMPLETADA EXITOSAMENTE: Estado=%s", id_saga, estado_final)

        return {
            "estado": estado_final,
            "id_saga": id_saga,
            "mensaje": "Agendamiento completado exitosamente",
            "cupo_reservado": True,
            "elegibilidad_estado": estado_eleg,
            "registro": datos_registro,
            "compensado": False,
        }

    except HTTPException as http_err:
        logger.warning(
            "[SAGA %s] Error HTTP (%d): %s",
            id_saga, http_err.status_code, http_err.detail
        )
        _compensar(hechas, id_saga)
        detalle = (
            f"{http_err.detail}. Transacción compensada y cupo liberado."
            if hechas else http_err.detail
        )
        raise HTTPException(status_code=http_err.status_code, detail=detalle) from http_err
    except Exception as e:
        logger.error("[SAGA %s] Error inesperado en la Saga: %s", id_saga, str(e))
        _compensar(hechas, id_saga)
        detalle = (
            f"Falla crítica en la Saga: {str(e)}. Transacción compensada y cupo liberado."
            if hechas else f"Falla en la Saga: {str(e)}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detalle,
        ) from e
