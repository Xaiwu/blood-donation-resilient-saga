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
MAX_REINTENTOS_COMPENSACION = int(os.getenv("SAGA_COMP_RETRIES", "3"))


class ErrorResultadoInciertoRegistro(Exception):
    """Excepción lanzada cuando el Paso 3 (Registro) falla con estado incierto (ej. timeout)."""


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


def comp_liberar_cupo(payload: Dict[str, Any], headers: Dict[str, str]) -> bool:
    """
    Acción compensatoria del Paso 1: libera el cupo previamente reservado.
    Reintenta hasta MAX_REINTENTOS_COMPENSACION veces y retorna True si tuvo éxito.
    """
    url = f"{CUPOS_SERVICE_URL}/cupos/liberar"
    id_saga = headers.get("X-Saga-ID", "desconocido")
    for intento in range(1, MAX_REINTENTOS_COMPENSACION + 1):
        logger.info(
            "[SAGA %s] Compensando: Liberando cupo en %s (intento %d/%d)",
            id_saga, url, intento, MAX_REINTENTOS_COMPENSACION
        )
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
            if r.status_code in (200, 204, 404):
                logger.info("[SAGA %s] Cupo liberado exitosamente en compensación", id_saga)
                return True
            logger.error(
                "[SAGA %s] Error al compensar cupo (%d): %s",
                id_saga, r.status_code, r.text
            )
        except requests.RequestException as e:
            logger.critical(
                "[SAGA %s] Fallo al contactar cupos durante compensación: %s",
                id_saga, str(e)
            )
    return False


def comp_anular_registro(id_saga: str, payload: Dict[str, Any], headers: Dict[str, str]) -> bool:
    """
    Acción compensatoria del Paso 3: anula el registro de donación en caso de estado incierto.
    Trata 404 como éxito idempotente (el registro no alcanzó a persistirse).
    """
    url = f"{REGISTRO_SERVICE_URL}/registro/donaciones/{id_saga}/anular"
    for intento in range(1, MAX_REINTENTOS_COMPENSACION + 1):
        logger.info(
            "[SAGA %s] Compensando: Anulando registro en %s (intento %d/%d)",
            id_saga, url, intento, MAX_REINTENTOS_COMPENSACION
        )
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
            if r.status_code in (200, 204, 404):
                logger.info("[SAGA %s] Registro anulado/verificado en compensación", id_saga)
                return True
            logger.error(
                "[SAGA %s] Error al anular registro (%d): %s",
                id_saga, r.status_code, r.text
            )
        except requests.RequestException as e:
            logger.critical(
                "[SAGA %s] Fallo al contactar registro durante compensación: %s",
                id_saga, str(e)
            )
    return False


def _compensar(hechas: List[Tuple[str, Callable[[], bool]]], id_saga: str) -> bool:
    """
    Ejecuta las compensaciones registradas en ORDEN ESTRICTAMENTE INVERSO (LIFO).
    Retorna True si todas las compensaciones tuvieron éxito, False si alguna falló.
    """
    logger.warning(
        "[SAGA %s] Ejecutando compensaciones inversas (%d pendientes)...",
        id_saga, len(hechas)
    )
    todas_ok = True
    for nombre, accion in reversed(hechas):
        try:
            logger.info("[SAGA %s] Ejecutando compensación: %s", id_saga, nombre)
            exito = accion()
            if exito is False:
                todas_ok = False
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(
                "[SAGA %s] Error al ejecutar compensación %s: %s",
                id_saga, nombre, str(e)
            )
            todas_ok = False
    return todas_ok


def paso_reservar_cupo(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Paso 1: Reservar cupo de donación en la jornada."""
    url = f"{CUPOS_SERVICE_URL}/cupos/reservar"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
    except requests.Timeout as e:
        # Si la reserva dio timeout, pudo haberse procesado: liberamos preventivamente
        comp_liberar_cupo(payload, headers)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Timeout en servicio de cupos (reserva liberada preventivamente): {str(e)}",
        ) from e
    except requests.RequestException as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Servicio de cupos no disponible: {str(e)}",
        ) from e

    if r.status_code in (200, 201):
        return r.json() if r.content else {}
    if r.status_code == 409:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No hay cupos disponibles para la jornada: {r.text}",
        )
    if r.status_code >= 500:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Error interno en servicio de cupos ({r.status_code}): {r.text}",
        )
    raise HTTPException(
        status_code=r.status_code,
        detail=f"Solicitud rechazada por servicio de cupos ({r.status_code}): {r.text}",
    )


def paso_validar_elegibilidad(
    payload: Dict[str, Any], headers: Dict[str, str]
) -> tuple[str, Dict[str, Any]]:
    """
    Paso 2: Validar la aptitud médica del donante en el servicio externo frágil.
    Retorna (estado_elegibilidad, datos_respuesta).
    - 2xx con apto=True (o sin flag explícito False): retorna 'APTO'.
    - 422 o 2xx con apto=False: lanza HTTPException(422) para compensar y abortar.
    - 5xx o Timeout/ConnectionError: degrada a 'PENDIENTE_VALIDACION'.
    - Otros 4xx (ej. 400, 404): lanza HTTPException con ese código para compensar.
    """
    url = f"{ELEGIBILIDAD_SERVICE_URL}/elegibilidad/validar"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
        if r.status_code == 422:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Donante no apto clínicamente para donación",
            )
        if r.status_code >= 500:
            logger.warning(
                "Elegibilidad respondió con error 5xx (%d). Degradando estado",
                r.status_code
            )
            return "PENDIENTE_VALIDACION", {}
        if not 200 <= r.status_code < 300:
            raise HTTPException(
                status_code=r.status_code,
                detail=f"Error de validación en elegibilidad ({r.status_code}): {r.text}",
            )

        datos = r.json() if r.content else {}
        if isinstance(datos, dict) and (
            datos.get("apto") is False or datos.get("elegible") is False
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Donante no apto clínicamente para donación",
            )
        return "APTO", datos
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.warning(
            "Elegibilidad inestable/caída (%s). Degradando a PENDIENTE_VALIDACION",
            type(e).__name__
        )
        return "PENDIENTE_VALIDACION", {}


def paso_registrar_donacion(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """
    Paso 3: Registrar la donación agendada en firme en la base de datos de Registro.
    Trata 409 Conflict como éxito idempotente para no liberar el cupo en reintentos.
    """
    url = f"{REGISTRO_SERVICE_URL}/registro/donaciones"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
    except requests.RequestException as e:
        raise ErrorResultadoInciertoRegistro(
            f"Falla de conectividad/timeout con registro-service: {str(e)}"
        ) from e

    if r.status_code in (200, 201, 409):
        if r.status_code == 409:
            logger.info("Registro respondió 409 (duplicado idempotente); conservando reserva")
        return r.json() if r.content else {"id_saga": payload.get("id_saga"), "idempotente": True}

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Falla al registrar cita en registro-service ({r.status_code}): {r.text}",
    )


def _formatear_detalle_compensacion(
    detalle_base: str, hechas: List[Any], compensado_ok: bool
) -> str:
    """Construye el mensaje de error reflejando con veracidad el resultado de la compensación."""
    if not hechas:
        return detalle_base
    if compensado_ok:
        return f"{detalle_base}. Transacción compensada y cupo liberado."
    return (
        f"{detalle_base}. Atención: falló la compensación automática; "
        "el cupo requiere liberación/reconciliación."
    )


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
      3. Registro: Registrar donación en firme (compensación: anular registro).

    Si el Paso 3 (o el Paso 2 por no apto) falla, ejecuta las COMPENSACIONES en orden inverso.
    """
    headers_internos = {
        "X-Saga-ID": id_saga,
        "X-Correlation-ID": id_saga,
        "X-User-Id": user_id or "sistema",
        "X-User-Role": user_role or "desconocido",
    }
    hechas: List[Tuple[str, Callable[[], bool]]] = []

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
        try:
            datos_registro = paso_registrar_donacion(payload_registro, headers_internos)
        except ErrorResultadoInciertoRegistro as err_incierto:
            # Estado incierto en Paso 3: apilar compensación de Registro antes de liberar cupo
            hechas.append(
                (
                    "anular_registro",
                    lambda: comp_anular_registro(id_saga, payload_registro, headers_internos),
                )
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(err_incierto),
            ) from err_incierto

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
        compensado_ok = _compensar(hechas, id_saga)
        detalle = _formatear_detalle_compensacion(http_err.detail, hechas, compensado_ok)
        raise HTTPException(status_code=http_err.status_code, detail=detalle) from http_err
    except Exception as e:
        logger.error("[SAGA %s] Error inesperado en la Saga: %s", id_saga, str(e))
        compensado_ok = _compensar(hechas, id_saga)
        detalle = _formatear_detalle_compensacion(
            f"Falla crítica en la Saga: {str(e)}", hechas, compensado_ok
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detalle,
        ) from e
