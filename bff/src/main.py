"""
BFF (Backend For Frontend) y Orquestador de la SAGA para Donaciones.
Expone la agregación de datos por cliente y coordina el agendamiento distribuido.
"""
from concurrent.futures import ThreadPoolExecutor
import logging
from typing import Optional
from urllib.parse import quote

import requests
from fastapi import FastAPI, Header, HTTPException, status

from .models import RespuestaAgendarDonacion, ResumenDonante, SolicitudAgendarDonacion
from .saga import (
    CUPOS_SERVICE_URL,
    REGISTRO_SERVICE_URL,
    derivar_id_saga,
    ejecutar_saga_agendamiento,
    elegibilidad_resiliente,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("bff")

app = FastAPI(
    title="BFF - Backend for Frontend & Orquestador Saga",
    version="1.0.0",
    description="Agregador de datos y orquestador de transacciones para donaciones",
)


@app.get("/health", tags=["Infraestructura"])
@app.get("/salud", tags=["Infraestructura"])
def health_check():
    """Verifica el estado del servicio BFF y sus dependencias configuradas."""
    return {
        "status": "ok",
        "servicio": "bff",
        "puerto": 5000,
        "cupos_target": CUPOS_SERVICE_URL,
        "registro_target": REGISTRO_SERVICE_URL,
    }


@app.get("/resiliencia/estado", tags=["Resiliencia"])
def estado_resiliencia():
    """Expone las métricas y estado actual del Circuit Breaker y Bulkhead hacia Elegibilidad."""
    return elegibilidad_resiliente.resumen()


@app.post(
    "/api/v1/donaciones/agendar",
    response_model=RespuestaAgendarDonacion,
    status_code=status.HTTP_201_CREATED,
    tags=["Saga Agendamiento"],
)
def agendar_donacion(
    solicitud: SolicitudAgendarDonacion,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """
    Orquestador principal de la Saga 'Agendar una Donación'.
    1. Deriva id_saga determinista a partir de Idempotency-Key (UUIDv5).
    2. Ejecuta los pasos distribuidos: Reservar Cupo -> Validar Elegibilidad -> Registrar Cita.
    3. Si ocurre algún error en los pasos finales, ejecuta compensación inversa (liberar cupo).
    """
    id_saga = derivar_id_saga(idempotency_key)
    logger.info(
        "[BFF] Solicitud recibida. Idempotency-Key='%s', ID_Saga='%s'",
        idempotency_key,
        id_saga,
    )

    resultado = ejecutar_saga_agendamiento(
        payload=solicitud.model_dump(),
        id_saga=id_saga,
        user_id=x_user_id,
        user_role=x_user_role,
    )

    return resultado


@app.get(
    "/api/v1/donantes/{rut}/resumen",
    response_model=ResumenDonante,
    tags=["Agregación BFF"],
)
def obtener_resumen_donante(
    rut: str,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """
    Endpoint de agregación del BFF:
    Consulta concurrentemente con requests a cupos-service y registro-service
    para componer y retornar un único payload adaptado al cliente web/móvil.
    """
    headers = {
        "X-User-Id": x_user_id or "sistema",
        "X-User-Role": x_user_role or "desconocido",
    }

    def _consultar_registro():
        try:
            url = f"{REGISTRO_SERVICE_URL}/registro/donantes/{quote(rut, safe='')}"
            r = requests.get(url, headers=headers, timeout=4.0)
            if r.status_code == 200:
                datos = r.json() if r.content else []
                return (datos if isinstance(datos, list) else []), None
            msg = f"registro-service respondió HTTP {r.status_code}"
            logger.warning("Error consultando citas de donante en registro: %s", msg)
            return [], msg
        except (requests.RequestException, ValueError) as e:
            msg = f"registro-service no disponible ({type(e).__name__})"
            logger.warning("Error consultando citas de donante en registro: %s", str(e))
            return [], msg

    def _consultar_cupos():
        try:
            url = f"{CUPOS_SERVICE_URL}/cupos/donante/{quote(rut, safe='')}"
            r = requests.get(url, headers=headers, timeout=4.0)
            if r.status_code == 200:
                datos = r.json() if r.content else []
                return (datos if isinstance(datos, list) else []), None
            msg = f"cupos-service respondió HTTP {r.status_code}"
            logger.warning("Error consultando cupos de donante en cupos: %s", msg)
            return [], msg
        except (requests.RequestException, ValueError) as e:
            msg = f"cupos-service no disponible ({type(e).__name__})"
            logger.warning("Error consultando cupos de donante en cupos: %s", str(e))
            return [], msg

    # Consulta concurrente utilizando ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        futuro_citas = executor.submit(_consultar_registro)
        futuro_cupos = executor.submit(_consultar_cupos)
        citas_historicas, error_registro = futuro_citas.result()
        cupos_activos, error_cupos = futuro_cupos.result()

    advertencias = [err for err in (error_registro, error_cupos) if err is not None]
    if error_registro is not None and error_cupos is not None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicios de registro y cupos no disponibles: " + "; ".join(advertencias),
        )

    ultima_cita = citas_historicas[0] if citas_historicas else None

    return ResumenDonante(
        rut=rut,
        nombre=ultima_cita.get("nombre_donante") if ultima_cita else None,
        citas_historicas=citas_historicas,
        cupos_activos=cupos_activos,
        ultima_cita=ultima_cita,
        estado_elegibilidad_reciente=(
            ultima_cita.get("estado_elegibilidad") if ultima_cita else None
        ),
        datos_parciales=bool(advertencias),
        advertencias=advertencias,
    )
