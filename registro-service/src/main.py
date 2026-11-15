from contextlib import asynccontextmanager
import os
import sqlite3
from typing import Any, AsyncGenerator, Dict, List
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from src.db import (CitaNoEncontradaError,ColisionIdempotenciaError,FallaSimuladaRegistroError,
    anular_donacion,init_db,logger,obtener_citas_donante,registrar_donacion,
    )
from src.models import ( AnularDonacionResponse,CitaDonanteItem,ConfigFallaRequest,
    RegistrarDonacionRequest,RegistrarDonacionResponse,)

ESTADO_CAOS: Dict[str, bool] = {"simular_falla": os.getenv("SIMULAR_FALLA", "false").lower() in ("true", "1", "yes"),}

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    yield

app = FastAPI(
    title="Registro Service",
    description="Persistencia y gestión transaccional de citas médicas de donación de sangre",
    version="1.0.0",
    lifespan=lifespan,
)

@app.exception_handler(ColisionIdempotenciaError)
async def conflicto_handler(_req: Request, exc: ColisionIdempotenciaError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

@app.exception_handler(CitaNoEncontradaError)
async def no_encontrado_handler(_req: Request, exc: CitaNoEncontradaError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

@app.exception_handler(FallaSimuladaRegistroError)
async def falla_simulada_handler(_req: Request, exc: FallaSimuladaRegistroError) -> JSONResponse:
    logger.error("[REGISTRO] Fallo inducido en Paso 3 (500): %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(exc)},
    )

@app.exception_handler(sqlite3.OperationalError)
async def sqlite_ocupado_handler(_req: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    logger.error("[REGISTRO] Bloqueo temporal de SQLite (503): %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Base de datos temporalmente ocupada por alta concurrencia. Reintente."},
    )

@app.exception_handler(sqlite3.Error)
async def sqlite_error_handler(_req: Request, exc: sqlite3.Error) -> JSONResponse:
    logger.error("[REGISTRO] Error interno de SQLite (500): %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno de persistencia en el servicio de registro."},
    )

@app.get("/health", status_code=status.HTTP_200_OK)
def healthcheck() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "registro-service",
        "simular_falla": ESTADO_CAOS["simular_falla"],
    }

@app.post("/registro/config/falla", status_code=status.HTTP_200_OK)
def configurar_falla(payload: ConfigFallaRequest) -> Dict[str, Any]:
    """Activa o desactiva la simulación de fallo HTTP 500 para demostrar el Escenario 2."""
    ESTADO_CAOS["simular_falla"] = payload.fallar
    logger.warning("[REGISTRO] Simulación de falla configurada en: %s", payload.fallar)
    return {"ok": True, "simular_falla": ESTADO_CAOS["simular_falla"]}

@app.post("/registro/donaciones", response_model=RegistrarDonacionResponse, status_code=status.HTTP_201_CREATED)
def endpoint_registrar_donacion(
    payload: RegistrarDonacionRequest, response: Response
) -> RegistrarDonacionResponse:
    """Registra una cita médica en firme (201 si es nueva, 200 si es idempotente, 409 si hay colisión)."""
    if ESTADO_CAOS["simular_falla"] or payload.grupo_sanguineo == "FALLA_REGISTRO":
        raise FallaSimuladaRegistroError(
            "Fallo simulado en el servicio de registro (Escenario 2: compensación inversa)."
        )

    resultado = registrar_donacion(
        id_saga=payload.id_saga,
        rut_donante=payload.rut_donante,
        nombre_donante=payload.nombre_donante,
        id_jornada=payload.id_jornada,
        horario=payload.horario,
        estado_elegibilidad=payload.estado_elegibilidad,
        email_donante=payload.email_donante,
        telefono_donante=payload.telefono_donante,
        edad=payload.edad,
        peso_kg=payload.peso_kg,
        grupo_sanguineo=payload.grupo_sanguineo,
    )
    response.status_code = status.HTTP_200_OK if resultado.get("idempotente") else status.HTTP_201_CREATED
    return RegistrarDonacionResponse(**resultado)

@app.post(
    "/registro/donaciones/{id_saga}/anular",
    response_model=AnularDonacionResponse,
    status_code=status.HTTP_200_OK,
)
def endpoint_anular_donacion(id_saga: str) -> AnularDonacionResponse:
    """Anula de forma idempotente una cita médica asociada a un id_saga (200 si anula, 404 si no existe)."""
    if not id_saga.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El parámetro 'id_saga' no puede estar vacío.",
        )
    resultado = anular_donacion(id_saga=id_saga.strip())
    return AnularDonacionResponse(**resultado)

@app.get("/registro/donantes/{rut}", response_model=List[CitaDonanteItem], status_code=status.HTTP_200_OK)
def endpoint_citas_por_donante(rut: str) -> List[CitaDonanteItem]:
    """Obtiene el historial de citas médicas activas de un donante por su RUT."""
    if not rut.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El parámetro 'rut' no puede estar vacío.",
        )
    return [CitaDonanteItem(**c) for c in obtener_citas_donante(rut_donante=rut.strip())]
