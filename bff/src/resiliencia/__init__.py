"""Patrones de resiliencia (Circuit Breaker + Bulkhead) para la llamada a Elegibilidad."""

from .bulkhead import Bulkhead
from .circuit_breaker import CircuitBreaker, Estado
from .cliente import ClienteResiliente
from .errores import BulkheadLleno, CircuitoAbierto, DependenciaNoDisponible

__all__ = [
    "Bulkhead",
    "BulkheadLleno",
    "CircuitBreaker",
    "CircuitoAbierto",
    "ClienteResiliente",
    "DependenciaNoDisponible",
    "Estado",
]
