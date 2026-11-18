"""Bulkhead: límite de llamadas simultáneas hacia una dependencia.

Los endpoints síncronos del BFF corren en un pool de hilos compartido. Si
Elegibilidad se pone lenta, sin límite cada petición dejaría un hilo bloqueado
esperándola, hasta agotar el pool y dejar sin hilos al resto de endpoints
(por ejemplo, el resumen del donante). El bulkhead deja como máximo
``max_concurrentes`` hilos esperando a Elegibilidad y rechaza el exceso.

Con ``max_espera_s = 0`` (por defecto) el rechazo es inmediato.
"""

import logging
import threading
from typing import Callable, TypeVar

from .errores import BulkheadLleno
from .eventos import registrar_evento

T = TypeVar("T")

log = logging.getLogger("resiliencia.bulkhead")


class Bulkhead:
    def __init__(
        self,
        nombre: str,
        max_concurrentes: int = 5,
        max_espera_s: float = 0.0,
        activo: bool = True,
    ) -> None:
        if max_concurrentes < 1:
            raise ValueError("max_concurrentes debe ser >= 1")
        if max_espera_s < 0:
            raise ValueError("max_espera_s no puede ser negativo")
        self.nombre = nombre
        self.max_concurrentes = max_concurrentes
        self.max_espera_s = max_espera_s
        self.activo = activo
        self._semaforo = threading.BoundedSemaphore(max_concurrentes)
        self._candado = threading.Lock()

        self.en_curso = 0
        self.metricas = {"admitidas": 0, "rechazadas": 0, "max_en_curso_observado": 0}

    def resumen(self) -> dict:
        with self._candado:
            return {
                "nombre": self.nombre,
                "activo": self.activo,
                "max_concurrentes": self.max_concurrentes,
                "max_espera_s": self.max_espera_s,
                "en_curso": self.en_curso,
                **self.metricas,
            }

    def ejecutar(self, operacion: Callable[[], T]) -> T:
        if not self.activo:
            return self._medir(operacion)

        if self.max_espera_s == 0:
            admitida = self._semaforo.acquire(blocking=False)
        else:
            admitida = self._semaforo.acquire(timeout=self.max_espera_s)

        if not admitida:
            with self._candado:
                self.metricas["rechazadas"] += 1
            # No se informa en_curso: entre el acquire fallido y esta línea otra
            # llamada puede haber terminado, y el número no sería el del rechazo.
            registrar_evento(
                log,
                "bulkhead_rechazo",
                bulkhead=self.nombre,
                max_concurrentes=self.max_concurrentes,
            )
            raise BulkheadLleno(
                f"bulkhead '{self.nombre}' lleno (máximo {self.max_concurrentes} llamadas simultáneas)"
            )
        try:
            return self._medir(operacion)
        finally:
            self._semaforo.release()

    def _medir(self, operacion: Callable[[], T]) -> T:
        # También mide con el bulkhead desactivado, para que el experimento
        # "sin patrón" muestre cuántas llamadas llegan a acumularse.
        with self._candado:
            self.metricas["admitidas"] += 1
            self.en_curso += 1
            self.metricas["max_en_curso_observado"] = max(
                self.metricas["max_en_curso_observado"], self.en_curso
            )
        try:
            return operacion()
        finally:
            with self._candado:
                self.en_curso -= 1
