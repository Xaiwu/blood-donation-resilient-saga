"""
BFF (Backend For Frontend) y Orquestador de la SAGA para Donaciones.
Expone la agregación de datos por cliente y coordina el agendamiento distribuido.
"""
from concurrent.futures import ThreadPoolExecutor
import logging
from typing import Optional

import requests
from fastapi import FastAPI, Header, status

from .models import RespuestaAgendarDonacion, ResumenDonante, SolicitudAgendarDonacion
from .saga import (
    CUPOS_SERVICE_URL,
    REGISTRO_SERVICE_URL,
    derivar_id_saga,
    ejecutar_saga_agendamiento,
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
            url = f"{REGISTRO_SERVICE_URL}/registro/donantes/{rut}"
            r = requests.get(url, headers=headers, timeout=4.0)
            return r.json() if r.status_code == 200 and r.content else []
        except requests.RequestException as e:
            logger.warning("Error consultando citas de donante en registro: %s", str(e))
            return []

    def _consultar_cupos():
        try:
            url = f"{CUPOS_SERVICE_URL}/cupos/donante/{rut}"
            r = requests.get(url, headers=headers, timeout=4.0)
            return r.json() if r.status_code == 200 and r.content else []
        except requests.RequestException as e:
            logger.warning("Error consultando cupos de donante en cupos: %s", str(e))
            return []

    # Consulta concurrente utilizando ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        futuro_citas = executor.submit(_consultar_registro)
        futuro_cupos = executor.submit(_consultar_cupos)
        citas_historicas = futuro_citas.result()
        cupos_activos = futuro_cupos.result()

    proxima_cita = (
        citas_historicas[0]
        if (citas_historicas and isinstance(citas_historicas, list))
        else None
    )

    return ResumenDonante(
        rut=rut,
        nombre=proxima_cita.get("nombre_donante") if proxima_cita else None,
        citas_historicas=citas_historicas if isinstance(citas_historicas, list) else [],
        cupos_activos=cupos_activos if isinstance(cupos_activos, list) else [],
        proxima_cita=proxima_cita,
        estado_elegibilidad_reciente=(
            proxima_cita.get("estado_elegibilidad") if proxima_cita else None
        ),
    )
