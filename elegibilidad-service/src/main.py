from typing import Any, Dict, Optional

from src.caos import (
    FASTAPI_INSTALADO,
    FallaSimuladaError,
    aplicar_caos,
    logger,
    obtener_estado_caos,
    router as caos_router,
)

EDAD_MINIMA = 18
EDAD_MAXIMA = 65
PESO_MINIMO_KG = 50.0

# Almacén de historial clínico de donantes con contraindicaciones vigentes (clave: RUT normalizado sin puntos ni guión)
HISTORIAL_MEDICO_NO_APTOS: Dict[str, str] = {
    "000000000": "Hemoglobina insuficiente (< 12.5 g/dL) registrada en su último control clínico.",
    "11111111K": "Donación de sangre reciente hace menos de 3 meses (periodo de ventana vigente).",
    "333333333": "Cirugía menor o procedimiento endoscópico en los últimos 6 meses.",
}


def normalizar_rut(rut: str) -> str:
    """Normaliza un RUT eliminando espacios, puntos y guiones en mayúsculas para búsqueda uniforme."""
    return rut.strip().upper().replace(".", "").replace("-", "")


def consultar_motivo_inhabilitacion(rut_donante: str) -> Optional[str]:
    """Consulta el almacén de historial médico y retorna el motivo clínico si el donante no está apto."""
    rut_upper = rut_donante.strip().upper()
    if "NO-APTO" in rut_upper or "NO_APTO" in rut_upper:
        return "Donante registra contraindicación médica vigente en el historial clínico."
    clave_rut = normalizar_rut(rut_donante)
    return HISTORIAL_MEDICO_NO_APTOS.get(clave_rut)


class DonanteNoAptoError(Exception):
    """El donante no cumple los criterios clínicos para donar sangre (HTTP 422)."""

    def __init__(self, rut_donante: str, motivo: str, id_saga: Optional[str] = None):
        super().__init__(motivo)
        self.rut_donante = rut_donante
        self.motivo = motivo
        self.id_saga = id_saga


def evaluar_elegibilidad(
    rut_donante: str,
    edad: Optional[int] = None,
    peso_kg: Optional[float] = None,
    apto: Optional[bool] = None,
    elegible: Optional[bool] = None,
    id_saga: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evalúa la aptitud clínica de un donante aplicando primero las reglas de caos activas.
    Si el donante no cumple los requisitos médicos, lanza DonanteNoAptoError (HTTP 422).
    """
    aplicar_caos(id_saga=id_saga)

    rut_limpio = rut_donante.strip()

    if apto is False or elegible is False:
        motivo = "Donante marcado explícitamente como no apto en la evaluación clínica."
        logger.info("[ELEGIBILIDAD] NO APTO (422) | rut=%s | saga=%s | motivo=%s", rut_limpio, id_saga, motivo)
        raise DonanteNoAptoError(rut_donante=rut_limpio, motivo=motivo, id_saga=id_saga)

    motivo_historial = consultar_motivo_inhabilitacion(rut_limpio)
    if motivo_historial is not None:
        logger.info("[ELEGIBILIDAD] NO APTO (422) | rut=%s | saga=%s | motivo=%s", rut_limpio, id_saga, motivo_historial)
        raise DonanteNoAptoError(rut_donante=rut_limpio, motivo=motivo_historial, id_saga=id_saga)

    if edad is not None and (edad < EDAD_MINIMA or edad > EDAD_MAXIMA):
        motivo = f"Edad ({edad} años) fuera del rango clínico permitido ({EDAD_MINIMA} a {EDAD_MAXIMA} años)."
        logger.info("[ELEGIBILIDAD] NO APTO (422) | rut=%s | saga=%s | motivo=%s", rut_limpio, id_saga, motivo)
        raise DonanteNoAptoError(rut_donante=rut_limpio, motivo=motivo, id_saga=id_saga)

    if peso_kg is not None and peso_kg < PESO_MINIMO_KG:
        motivo = f"Peso corporal ({peso_kg} kg) inferior al mínimo clínico requerido ({PESO_MINIMO_KG} kg)."
        logger.info("[ELEGIBILIDAD] NO APTO (422) | rut=%s | saga=%s | motivo=%s", rut_limpio, id_saga, motivo)
        raise DonanteNoAptoError(rut_donante=rut_limpio, motivo=motivo, id_saga=id_saga)

    logger.info("[ELEGIBILIDAD] DONANTE APTO (200) | rut=%s | saga=%s", rut_limpio, id_saga or "N/A")
    return {
        "ok": True,
        "apto": True,
        "elegible": True,
        "rut_donante": rut_limpio,
        "id_saga": id_saga,
        "estado": "APTO",
        "motivo": "Donante cumple con los requisitos médicos de elegibilidad.",
    }


if FASTAPI_INSTALADO:
    from fastapi import FastAPI, Header, HTTPException, Request, status
    from fastapi.responses import JSONResponse
    from src.models import ValidarElegibilidadRequest, ValidarElegibilidadResponse

    app = FastAPI(
        title="Elegibilidad Service",
        description="Servicio externo simulado de validación de aptitud médica e inyección de caos",
        version="1.0.0",
    )

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
