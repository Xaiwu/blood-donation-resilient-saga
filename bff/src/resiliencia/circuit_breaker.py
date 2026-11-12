"""Circuit Breaker con estados CERRADO, ABIERTO y SEMIABIERTO.

- CERRADO: las llamadas pasan. Cada fallo suma 1 a los fallos consecutivos y
  un éxito los vuelve a 0. Al llegar a ``umbral_fallos`` el circuito se abre.
- ABIERTO: toda llamada se rechaza al instante con ``CircuitoAbierto``, sin
  tocar la dependencia. Pasado ``tiempo_recuperacion_s`` pasa a SEMIABIERTO.
- SEMIABIERTO: deja pasar una sola llamada de prueba; las demás se rechazan.
  Si la prueba funciona se cierra; si falla vuelve a abrirse.

Es seguro entre hilos: los endpoints síncronos de FastAPI corren en un pool de
hilos, así que varias peticiones pueden usar el mismo circuito a la vez.
"""

import logging
import threading
import time
from enum import Enum
from typing import Callable, TypeVar

from .errores import CircuitoAbierto
from .eventos import registrar_evento

T = TypeVar("T")

log = logging.getLogger("resiliencia.circuit_breaker")


class Estado(str, Enum):
    CERRADO = "CERRADO"
    ABIERTO = "ABIERTO"
    SEMIABIERTO = "SEMIABIERTO"


def _siempre_fallo(_exc: Exception) -> bool:
    return True


class CircuitBreaker:
    def __init__(
        self,
        nombre: str,
        umbral_fallos: int = 3,
        tiempo_recuperacion_s: float = 10.0,
        activo: bool = True,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        if umbral_fallos < 1:
            raise ValueError("umbral_fallos debe ser >= 1")
        if tiempo_recuperacion_s < 0:
            raise ValueError("tiempo_recuperacion_s no puede ser negativo")
        self.nombre = nombre
        self.umbral_fallos = umbral_fallos
        self.tiempo_recuperacion_s = tiempo_recuperacion_s
        self.activo = activo
        self._reloj = reloj
        self._candado = threading.Lock()

        self._estado = Estado.CERRADO
        self._fallos_consecutivos = 0
        self._abierto_desde: float | None = None
        self._prueba_en_curso = False

        self.metricas = {
            "llamadas": 0,
            "exitos": 0,
            "fallos": 0,
            "rechazos_fail_fast": 0,
            "aperturas": 0,
        }

    @property
    def estado(self) -> Estado:
        with self._candado:
            return self._estado_actual()

    def resumen(self) -> dict:
        with self._candado:
            return {
                "nombre": self.nombre,
                "activo": self.activo,
                "estado": self._estado_actual().value,
                "fallos_consecutivos": self._fallos_consecutivos,
                "umbral_fallos": self.umbral_fallos,
                "tiempo_recuperacion_s": self.tiempo_recuperacion_s,
                **self.metricas,
            }

    def llamar(
        self,
        operacion: Callable[[], T],
        es_fallo: Callable[[Exception], bool] = _siempre_fallo,
    ) -> T:
        """Ejecuta ``operacion`` protegida por el circuito.

        ``es_fallo`` decide qué excepciones cuentan para abrir el circuito; las
        demás se propagan sin cambiar el estado.
        """
        if not self.activo:
            return operacion()

        with self._candado:
            estado = self._estado_actual()
            if estado is Estado.ABIERTO or (
                estado is Estado.SEMIABIERTO and self._prueba_en_curso
            ):
                self.metricas["rechazos_fail_fast"] += 1
                raise CircuitoAbierto(f"circuito '{self.nombre}' en estado {estado.value}")
            es_prueba = estado is Estado.SEMIABIERTO
            if es_prueba:
                self._prueba_en_curso = True
            self.metricas["llamadas"] += 1

        try:
            resultado = operacion()
        except Exception as exc:
            with self._candado:
                if es_fallo(exc):
                    self._registrar_fallo(exc)
                elif es_prueba:
                    # La prueba no alcanzó a evaluar la dependencia: se permite otra.
                    self._prueba_en_curso = False
            raise

        with self._candado:
            self._registrar_exito()
        return resultado

    # Los métodos siguientes asumen que el candado ya está tomado.

    def _estado_actual(self) -> Estado:
        if (
            self._estado is Estado.ABIERTO
            and self._abierto_desde is not None
            and self._reloj() - self._abierto_desde >= self.tiempo_recuperacion_s
        ):
            self._transicion(Estado.SEMIABIERTO)
        return self._estado

    def _registrar_exito(self) -> None:
        self.metricas["exitos"] += 1
        self._fallos_consecutivos = 0
        if self._estado is Estado.SEMIABIERTO:
            self._transicion(Estado.CERRADO)

    def _registrar_fallo(self, exc: Exception) -> None:
        self.metricas["fallos"] += 1
        self._fallos_consecutivos += 1
        if self._estado is Estado.SEMIABIERTO or (
            self._estado is Estado.CERRADO
            and self._fallos_consecutivos >= self.umbral_fallos
        ):
            self._transicion(Estado.ABIERTO, motivo=type(exc).__name__)

    def _transicion(self, nuevo: Estado, motivo: str | None = None) -> None:
        anterior = self._estado
        if anterior is nuevo:
            return
        self._estado = nuevo
        self._prueba_en_curso = False
        if nuevo is Estado.ABIERTO:
            self._abierto_desde = self._reloj()
            self.metricas["aperturas"] += 1
        elif nuevo is Estado.CERRADO:
            self._abierto_desde = None
            self._fallos_consecutivos = 0
        registrar_evento(
            log,
            "circuito_transicion",
            circuito=self.nombre,
            desde=anterior.value,
            hacia=nuevo.value,
            fallos_consecutivos=self._fallos_consecutivos,
            motivo=motivo,
        )
