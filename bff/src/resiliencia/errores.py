"""Excepciones que lanza el cliente resiliente sin llegar a llamar a la dependencia."""


class DependenciaNoDisponible(Exception):
    """La llamada se rechazó sin ejecutarse. La Saga debe degradar."""


class CircuitoAbierto(DependenciaNoDisponible):
    """El circuito está abierto (o semiabierto con la prueba en curso): fail-fast."""


class BulkheadLleno(DependenciaNoDisponible):
    """No hay cupo de concurrencia hacia la dependencia."""
