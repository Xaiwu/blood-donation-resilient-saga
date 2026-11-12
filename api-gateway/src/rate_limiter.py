"""
Módulo de Rate Limiting perimetral para el API Gateway.
Implementa un limitador en memoria mediante ventana deslizante por IP.
"""
import time
from collections import defaultdict


class RateLimiterMemoria:  # pylint: disable=too-few-public-methods
    """
    Implementa un limitador de tasa perimetral mediante ventana deslizante por dirección IP.
    Protege los microservicios internos frente a ráfagas y tráfico abusivo.
    """
    def __init__(self, max_peticiones: int = 15, ventana_segundos: int = 60):
        self.max_peticiones = max_peticiones
        self.ventana_segundos = ventana_segundos
        self.registro = defaultdict(list)

    def verificar_limite(self, ip_cliente: str) -> tuple[bool, int]:
        """Verifica si la IP cliente puede realizar una petición según la cuota actual."""
        ahora = time.time()
        inicio_ventana = ahora - self.ventana_segundos
        # Limpiar marcas de tiempo fuera de la ventana deslizante actual
        self.registro[ip_cliente] = [
            ts for ts in self.registro[ip_cliente] if ts > inicio_ventana
        ]

        if len(self.registro[ip_cliente]) >= self.max_peticiones:
            primer_ts = self.registro[ip_cliente][0]
            tiempo_restante = max(1, int(self.ventana_segundos - (ahora - primer_ts)))
            return False, tiempo_restante

        self.registro[ip_cliente].append(ahora)
        return True, 0
