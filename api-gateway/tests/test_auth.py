import os
import sys
import time
import pytest
import jwt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from auth import (
    HTTPAuthorizationCredentials,
    HTTPException,
    JWT_ALGORITMO,
    JWT_SECRET,
    ROL_ESCRITURA,
    ROL_LECTURA,
    autenticar,
    crear_token,
    requiere_escritura,
    verificar_token,
)


@pytest.mark.parametrize(
    "usuario,clave,rol_esperado",
    [
        ("lector", "lector123", ROL_LECTURA),
        ("operador", "operador123", ROL_ESCRITURA),
    ],
)
def test_autenticacion_credenciales_correctas(usuario, clave, rol_esperado):
    assert autenticar(usuario, clave) == rol_esperado


@pytest.mark.parametrize(
    "usuario,clave",
    [
        ("lector", "clave_invalida"),
        ("usuario_inexistente", "clave"),
        ("", ""),
    ],
)
def test_autenticacion_credenciales_incorrectas(usuario, clave):
    assert autenticar(usuario, clave) is None


def test_creacion_y_verificacion_token_valido():
    datos_token = crear_token("operador", ROL_ESCRITURA)
    token_str = datos_token["access_token"]

    credenciales = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_str)
    payload = verificar_token(credenciales)

    assert payload["sub"] == "operador"
    assert payload["rol"] == ROL_ESCRITURA


def test_token_expirado_lanza_401():
    # Crear token con timestamp expirado en el pasado
    payload_expirado = {
        "sub": "operador",
        "rol": ROL_ESCRITURA,
        "iat": int(time.time()) - 100,
        "exp": int(time.time()) - 10,
    }
    token_expirado = jwt.encode(payload_expirado, JWT_SECRET, algorithm=JWT_ALGORITMO)
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_expirado)

    with pytest.raises(HTTPException) as exc_info:
        verificar_token(cred)
    assert exc_info.value.status_code == 401


def test_token_inexistente_lanza_401():
    with pytest.raises(HTTPException) as exc_info:
        verificar_token(None)
    assert exc_info.value.status_code == 401


def test_control_acceso_requiere_escritura_permite_operador():
    payload_escritura = {"sub": "operador", "rol": ROL_ESCRITURA}
    assert requiere_escritura(payload_escritura) == payload_escritura


def test_control_acceso_requiere_escritura_bloquea_lector():
    payload_lectura = {"sub": "lector", "rol": ROL_LECTURA}
    with pytest.raises(HTTPException) as exc_info:
        requiere_escritura(payload_lectura)
    assert exc_info.value.status_code == 403
