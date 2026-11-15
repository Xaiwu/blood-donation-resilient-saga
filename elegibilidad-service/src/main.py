from typing import Any, Dict, Optional
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.caos import (
    DonanteNoAptoError,
    FallaSimuladaError,
    evaluar_elegibilidad,
    obtener_estado_caos,
    router as caos_router,
)
from src.models import ValidarElegibilidadRequest, ValidarElegibilidadResponse

app = FastAPI(
    title="Elegibilidad Service",
    description="Servicio externo simulado de validación de aptitud médica e inyección de caos",
    version="1.0.0",
)

if caos_router is not None:
    app.include_router(caos_router)


@app.exception_handler(DonanteNoAptoError)
async def no_apto_handler(_req: Request, exc: DonanteNoAptoError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "ok": False,
            "apto": False,
            "elegible": False,
            "rut_donante": exc.rut_donante,
            "id_saga": exc.id_saga,
            "estado": "NO_APTO",
            "motivo": exc.motivo,
            "detail": exc.motivo,
        },
    )


@app.exception_handler(FallaSimuladaError)
async def falla_simulada_handler(_req: Request, exc: FallaSimuladaError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.codigo_falla,
        content={
            "ok": False,
            "error": "SERVICIO_ELEGIBILIDAD_CAIDO",
            "detail": str(exc),
        },
    )


@app.get("/health", status_code=status.HTTP_200_OK)
@app.get("/salud", status_code=status.HTTP_200_OK)
def healthcheck() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "elegibilidad-service",
        "caos": obtener_estado_caos(),
    }


@app.post("/elegibilidad/validar", response_model=ValidarElegibilidadResponse, status_code=status.HTTP_200_OK)
def endpoint_validar_elegibilidad(
    payload: ValidarElegibilidadRequest,
    x_saga_id: Optional[str] = Header(default=None, alias="X-Saga-ID"),
) -> ValidarElegibilidadResponse:
    """Valida si el donante es apto para donar sangre (200 si es apto, 422 si no es apto, 500 si hay falla simulada)."""
    if not payload.rut_donante.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El campo 'rut_donante' no puede estar vacío.",
        )
    saga_id = (payload.id_saga or x_saga_id or "").strip() or None
    resultado = evaluar_elegibilidad(
        rut_donante=payload.rut_donante,
        edad=payload.edad,
        peso_kg=payload.peso_kg,
        apto=payload.apto,
        elegible=payload.elegible,
        id_saga=saga_id,
    )
    return ValidarElegibilidadResponse(**resultado)
