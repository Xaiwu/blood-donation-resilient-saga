"""
API Gateway del Sistema de Gestión de Donación de Sangre.
Único punto perimetral expuesto (:8080) con autenticación JWT, rate limiting y proxy a BFF.
"""

import logging
import os

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import (
    ROL_LECTURA,
    autenticar,
    crear_token,
    verificar_token,
)
from .rate_limiter import RateLimiterMemoria

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("api-gateway")

BFF_URL = os.getenv("BFF_URL", "http://bff:5000").rstrip("/")
BFF_TIMEOUT_SEGUNDOS = float(os.getenv("BFF_TIMEOUT_SEGUNDOS", "40.0"))
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "15"))

app = FastAPI(
    title="API Gateway - Sistema de Donación de Sangre",
    version="1.0.0",
    description="Punto de acceso perimetral con JWT, rate limiting y enrutamiento hacia BFF",
)

# Configuración de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rate_limiter = RateLimiterMemoria(max_peticiones=RATE_LIMIT_RPM, ventana_segundos=60)


@app.middleware("http")
async def rate_limiting_middleware(request: Request, call_next):
    """Aplica control de flujo por IP rechazando ráfagas que superen el umbral configurado."""
    if request.url.path in ("/health", "/salud", "/docs", "/openapi.json"):
        return await call_next(request)

    ip_cliente = request.client.host if request.client else "127.0.0.1"
    permitido, retry_after = rate_limiter.verificar_limite(ip_cliente)

    if not permitido:
        logger.warning(
            "Rate limit excedido para IP %s en ruta %s", ip_cliente, request.url.path
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "TOO_MANY_REQUESTS",
                "mensaje": (
                    f"Límite de peticiones excedido "
                    f"(máximo {rate_limiter.max_peticiones} req/min)"
                ),
            },
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)


class SolicitudLogin(BaseModel):
    """DTO para el endpoint de obtención de token JWT."""

    usuario: str
    clave: str


@app.get("/health", tags=["Infraestructura"])
@app.get("/salud", tags=["Infraestructura"])
def health_check():
    """Endpoint de monitoreo y verificación de conectividad básica."""
    return {
        "status": "ok",
        "servicio": "api-gateway",
        "puerto": 8080,
        "bff_target": BFF_URL,
    }


@app.post("/auth/token", tags=["Seguridad"])
def obtener_token(datos: SolicitudLogin):
    """Genera token JWT Bearer validando credenciales."""
    rol = autenticar(datos.usuario, datos.clave)
    if not rol:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "CREDENCIALES_INVALIDAS",
                "mensaje": "Usuario o contraseña incorrectos",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return crear_token(datos.usuario, rol)


# PROXY PERIMETRAL HACIA BFF
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_hacia_bff(
    request: Request,
    path: str,
    payload: dict = Depends(verificar_token),
):
    """
    Router perimetral con cliente síncrono 'requests':
    1. Valida centralizadamente el JWT en cabecera Authorization mediante Depends(verificar_token).
    2. Comprueba control de acceso (403 si rol lectura intenta mutar vía POST/PUT/DELETE).
    3. Inyecta cabeceras internas sanitizadas hacia el BFF: X-User-Id, X-User-Role.
    4. Reenvía la petición al BFF mediante requests.request.
    """
    es_operacion_mutante = request.method in ("POST", "PUT", "DELETE", "PATCH")
    if es_operacion_mutante and payload.get("rol") == ROL_LECTURA:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "PROHIBIDO",
                "mensaje": f"El rol '{ROL_LECTURA}' no tiene permisos para mutar recursos",
            },
        )

    # Construir cabeceras hacia el BFF eliminando las de salto y cualquier intento de suplantación
    cabeceras_internas = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in (
            "host",
            "content-length",
            "x-user-id",
            "x-user-role",
        )
    }

    # Inyección de identidad verificada por el Gateway
    cabeceras_internas["X-User-Id"] = str(payload.get("sub"))
    cabeceras_internas["X-User-Role"] = str(payload.get("rol"))

    # Preservar Idempotency-Key si el cliente la proporcionó
    idempotency_key = request.headers.get("idempotency-key")
    if idempotency_key:
        cabeceras_internas["Idempotency-Key"] = idempotency_key

    cuerpo = await request.body()
    url_destino = f"{BFF_URL}/api/{path}"
    parametros = dict(request.query_params)

    def _ejecutar_proxy():
        return requests.request(
            method=request.method,
            url=url_destino,
            params=parametros,
            headers=cabeceras_internas,
            data=cuerpo,
            timeout=BFF_TIMEOUT_SEGUNDOS,
        )

    try:
        resp = await run_in_threadpool(_ejecutar_proxy)

        cabeceras_respuesta = {
            k: v
            for k, v in resp.headers.items()
            if k.lower()
            not in (
                "content-length",
                "content-encoding",
                "transfer-encoding",
                "connection",
            )
        }

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=cabeceras_respuesta,
            media_type=resp.headers.get("content-type"),
        )
    except requests.exceptions.ConnectionError:
        logger.error("Error de conexión al intentar alcanzar BFF en %s", url_destino)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "BFF_NO_DISPONIBLE",
                "mensaje": "No fue posible conectar con el servicio BFF de backend",
            },
        )
    except requests.exceptions.Timeout:
        logger.error("Timeout al comunicarse con BFF en %s", url_destino)
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={
                "error": "GATEWAY_TIMEOUT",
                "mensaje": "Tiempo de espera agotado al conectar con el servicio BFF",
            },
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("Error inesperado en proxy del Gateway: %s", str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "GATEWAY_ERROR", "mensaje": str(exc)},
        )
