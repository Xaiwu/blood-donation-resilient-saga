"""
Módulo de Autenticación y Autorización perimetral mediante JWT Bearer.
Centraliza la validación de tokens y el control de acceso RBAC.
"""
import hmac
import logging
import os
import time

import jwt

try:
    from fastapi import Depends, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError:
    # Fallback para pruebas unitarias desacopladas del framework web
    class HTTPException(Exception):  # type: ignore
        """Excepción HTTP simulada para entorno de pruebas sin FastAPI."""
        def __init__(self, status_code: int, detail=None, headers=None):
            self.status_code = status_code
            self.detail = detail
            self.headers = headers
            super().__init__(str(detail))

    class HTTPAuthorizationCredentials:  # type: ignore  # pylint: disable=too-few-public-methods
        """Credenciales Bearer simuladas para pruebas unitarias."""
        def __init__(self, scheme: str, credentials: str):
            self.scheme = scheme
            self.credentials = credentials

    class HTTPBearer:  # type: ignore  # pylint: disable=too-few-public-methods
        """Esquema de seguridad Bearer simulado."""
        def __init__(self, auto_error: bool = True):
            pass

    def Depends(fn):  # type: ignore  # pylint: disable=invalid-name
        """Inyección de dependencias simulada."""
        return fn


logger = logging.getLogger("gateway.auth")

JWT_SECRET = os.getenv("JWT_SECRET")

if not JWT_SECRET:
    JWT_SECRET = "jwt-secret-distribuido-banco-sangre-token-seguro-2026"
    logger.warning(
        "JWT_SECRET no configurado: se utilizó clave por defecto para desarrollo"
    )

JWT_ALGORITMO = "HS256"
JWT_EXPIRA_SEGUNDOS = int(os.getenv("JWT_EXPIRA_SEGUNDOS", "3600"))

ROL_LECTURA = "lectura"
ROL_ESCRITURA = "escritura"
ROLES_VALIDOS = (ROL_LECTURA, ROL_ESCRITURA)

USUARIO_LECTURA = os.getenv("USUARIO_LECTURA", "lector")
CLAVE_LECTURA = os.getenv("CLAVE_LECTURA", "lector123")

USUARIO_ESCRITURA = os.getenv("USUARIO_ESCRITURA", "operador")
CLAVE_ESCRITURA = os.getenv("CLAVE_ESCRITURA", "operador123")

USUARIOS = {
    USUARIO_LECTURA: (CLAVE_LECTURA, ROL_LECTURA),
    USUARIO_ESCRITURA: (CLAVE_ESCRITURA, ROL_ESCRITURA),
}

_esquema_bearer = HTTPBearer(auto_error=False)


# ERROR 401
def _no_autenticado(mensaje: str) -> HTTPException:
    """Genera una excepción HTTP 401 con encabezado WWW-Authenticate."""
    return HTTPException(
        status_code=401,
        detail={"error": "NO_AUTENTICADO", "mensaje": mensaje},
        headers={"WWW-Authenticate": "Bearer"},
    )


def autenticar(usuario: str, clave: str) -> str | None:
    """Valida usuario y contraseña en tiempo constante contra ataques de temporización."""
    registro = USUARIOS.get(usuario)
    clave_esperada, rol = registro if registro else ("", None)
    clave_ok = hmac.compare_digest(
        clave.encode("utf-8"), clave_esperada.encode("utf-8")
    )
    return rol if (registro is not None and clave_ok) else None


def crear_token(usuario: str, rol: str) -> dict:
    """Genera un token firmado JWT HS256 con tiempo de expiración."""
    ahora = int(time.time())
    payload = {
        "sub": usuario,
        "rol": rol,
        "iat": ahora,
        "exp": ahora + JWT_EXPIRA_SEGUNDOS,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITMO)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": JWT_EXPIRA_SEGUNDOS,
    }


def verificar_token(
    credenciales: HTTPAuthorizationCredentials | None = Depends(_esquema_bearer),
) -> dict:
    """Valida la firma, expiración y claims del token JWT Bearer."""
    if credenciales is None:
        raise _no_autenticado("Token faltante o inválido")
    try:
        payload = jwt.decode(
            credenciales.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITMO],
            options={"require": ["exp", "sub", "rol"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _no_autenticado("Token expirado") from exc
    except jwt.InvalidTokenError as exc:
        raise _no_autenticado("Token faltante o inválido") from exc

    if payload.get("rol") not in ROLES_VALIDOS:
        raise _no_autenticado("Token faltante o inválido")
    return payload


def requiere_escritura(payload: dict = Depends(verificar_token)) -> dict:
    """Aplica control de acceso por rol verificando permisos de mutación."""
    if payload.get("rol") != ROL_ESCRITURA:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PROHIBIDO",
                "mensaje": f"El rol '{payload.get('rol')}' no puede realizar esta operación",
            },
        )
    return payload
