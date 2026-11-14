from contextlib import asynccontextmanager
import sqlite3
from typing import AsyncGenerator, Dict, List
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from src.db import (ColisionIdempotenciaError,JornadaSinCuposError,ReservaNoEncontradaError,
    init_db, liberar_cupo, logger, obtener_cupos_donante, reservar_cupo,)
from src.models import (CupoDonanteItem, LiberarCupoRequest, LiberarCupoResponse,
    ReservaCupoRequest, ReservaCupoResponse,)

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    yield

app = FastAPI(title="Cupos Service",description="Reserva y liberación de cupos para jornadas de donación de sangre",
    version="1.0.0",lifespan=lifespan,)

# Tipos de errores
@app.exception_handler(JornadaSinCuposError)
@app.exception_handler(ColisionIdempotenciaError)
async def conflicto_handler(_req: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

@app.exception_handler(ReservaNoEncontradaError)
async def no_encontrado_handler(_req: Request, exc: ReservaNoEncontradaError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

@app.exception_handler(sqlite3.OperationalError)
async def sqlite_ocupado_handler(_req: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    logger.error("[CUPOS] Bloqueo temporal de SQLite (503): %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Base de datos temporalmente ocupada por alta concurrencia. Reintente."},
    )

@app.exception_handler(sqlite3.Error)
async def sqlite_error_handler(_req: Request, exc: sqlite3.Error) -> JSONResponse:
    logger.error("[CUPOS] Error interno de SQLite (500): %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno de persistencia en el servicio de cupos."},
    )

@app.get("/health", status_code=status.HTTP_200_OK)
def healthcheck() -> Dict[str, str]:
    return {"status": "ok", "service": "cupos-service"}

@app.post("/cupos/reservar", response_model=ReservaCupoResponse, status_code=status.HTTP_201_CREATED)
def endpoint_reservar_cupo(payload: ReservaCupoRequest, response: Response) -> ReservaCupoResponse:
    """Reserva un cupo 201 si es nuevo, 200 si es idempotente, 409 si no hay cupos o hay colisión"""
    resultado = reservar_cupo(
        rut_donante=payload.rut_donante,
        id_jornada=payload.id_jornada,
        horario=payload.horario,
        id_saga=payload.id_saga,
    )
    response.status_code = status.HTTP_200_OK if resultado.get("idempotente") else status.HTTP_201_CREATED
    return ReservaCupoResponse(**resultado)

@app.post("/cupos/liberar", response_model=LiberarCupoResponse, status_code=status.HTTP_200_OK)
def endpoint_liberar_cupo(payload: LiberarCupoRequest) -> LiberarCupoResponse:
    """Libera un cupo reservado de forma idempotente 200 si libera, 404 si no existe"""
    resultado = liberar_cupo(id_saga=payload.id_saga)
    return LiberarCupoResponse(**resultado)

@app.get("/cupos/donante/{rut}", response_model=List[CupoDonanteItem], status_code=status.HTTP_200_OK)
def endpoint_cupos_por_donante(rut: str) -> List[CupoDonanteItem]:
    """Obtiene los cupos reservados activos de un donante por su RUT."""
    if not rut.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El parámetro 'rut' no puede estar vacío.",
        )
    return [CupoDonanteItem(**c) for c in obtener_cupos_donante(rut_donante=rut.strip())]
