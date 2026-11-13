"""Cliente HTTP resiliente para la dependencia frágil (Elegibilidad).

Envuelve ``requests.post`` con, de afuera hacia adentro:

    Circuit Breaker  ->  Bulkhead  ->  requests.post(..., timeout=...)

No interpreta la respuesta: devuelve el ``requests.Response`` tal cual, así la
Saga sigue decidiendo qué es apto, no apto o degradado. Lo único que agrega es:

- Si el circuito está abierto o el bulkhead lleno, lanza una subclase de
  ``DependenciaNoDisponible`` sin llamar a Elegibilidad. La Saga debe tratarla
  igual que un timeout: degradar a PENDIENTE_VALIDACION.
- Cuenta como fallo del circuito: timeout, error de conexión y respuestas 5xx.
  Una respuesta 5xx igual se devuelve a la Saga para que degrade como hoy.
- Cualquier otra respuesta (2xx, 4xx como el 422 de no apto) muestra que la
  dependencia está viva: cuenta como éxito.
- Un rechazo del bulkhead no cuenta como fallo, porque Elegibilidad no llegó a
  ser llamado.
"""

import os
from typing import Any

import requests

from .bulkhead import Bulkhead
from .circuit_breaker import CircuitBreaker


class _Respuesta5xx(Exception):
    """Uso interno: hace que una respuesta 5xx cuente como fallo del circuito."""

    def __init__(self, respuesta: requests.Response) -> None:
        super().__init__(f"HTTP {respuesta.status_code}")
        self.respuesta = respuesta


def _es_fallo(exc: Exception) -> bool:
    return isinstance(exc, (requests.Timeout, requests.ConnectionError, _Respuesta5xx))


class ClienteResiliente:
    def __init__(self, circuit_breaker: CircuitBreaker, bulkhead: Bulkhead) -> None:
        self.circuit_breaker = circuit_breaker
        self.bulkhead = bulkhead

    @classmethod
    def desde_entorno(cls, nombre: str = "elegibilidad") -> "ClienteResiliente":
        """Configuración desde variables de entorno (docker-compose).

        CB_ACTIVO=false y BULKHEAD_ACTIVO=false apagan cada patrón para correr
        los experimentos sin él, con el mismo código.
        """
        return cls(
            CircuitBreaker(
                nombre,
                umbral_fallos=int(os.getenv("CB_UMBRAL_FALLOS", "3")),
                tiempo_recuperacion_s=float(os.getenv("CB_RECUPERACION_S", "10")),
                activo=_bool_entorno("CB_ACTIVO", True),
            ),
            Bulkhead(
                nombre,
                max_concurrentes=int(os.getenv("BULKHEAD_MAX_CONCURRENTES", "5")),
                max_espera_s=float(os.getenv("BULKHEAD_MAX_ESPERA_S", "0")),
                activo=_bool_entorno("BULKHEAD_ACTIVO", True),
            ),
        )

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Igual que ``requests.post``; ``timeout`` es obligatorio."""
        if kwargs.get("timeout") is None:
            raise ValueError("toda llamada a la dependencia debe llevar timeout")

        def _llamada() -> requests.Response:
            respuesta = requests.post(url, **kwargs)
            if respuesta.status_code >= 500:
                raise _Respuesta5xx(respuesta)
            return respuesta

        try:
            return self.circuit_breaker.llamar(
                lambda: self.bulkhead.ejecutar(_llamada), es_fallo=_es_fallo
            )
        except _Respuesta5xx as exc:
            return exc.respuesta

    def resumen(self) -> dict:
        """Estado del circuito y del bulkhead, para un endpoint de métricas."""
        return {
            "circuit_breaker": self.circuit_breaker.resumen(),
            "bulkhead": self.bulkhead.resumen(),
        }


def _bool_entorno(nombre: str, por_defecto: bool) -> bool:
    valor = os.getenv(nombre)
    if valor is None:
        return por_defecto
    return valor.strip().lower() in {"1", "true", "si", "sí", "yes", "on"}
