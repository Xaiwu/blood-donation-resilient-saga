# Resiliencia sobre Elegibilidad

Circuit Breaker y Bulkhead para la llamada de la Saga a Elegibilidad (paso 2).

```
Circuit Breaker  ->  Bulkhead  ->  requests.post(..., timeout=...)
```

## Integración en `saga.py`

Son tres cambios:

```python
# 1. Arriba del archivo
try:
    from .resiliencia import ClienteResiliente, DependenciaNoDisponible
except ImportError:  # las pruebas importan saga como módulo suelto
    from resiliencia import ClienteResiliente, DependenciaNoDisponible

elegibilidad_resiliente = ClienteResiliente.desde_entorno()  # una sola instancia por proceso

# 2. En paso_validar_elegibilidad, cambiar requests.post por:
r = elegibilidad_resiliente.post(url, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)

# 3. Y agregar DependenciaNoDisponible a la excepción que degrada:
except (requests.Timeout, requests.ConnectionError, DependenciaNoDisponible) as e:
    return "PENDIENTE_VALIDACION", {}
```

El cliente devuelve el mismo `requests.Response`, así que la lógica de la Saga (apto, no apto, degradado) no cambia.

Para los experimentos conviene exponer el estado en `main.py`:

```python
from .saga import elegibilidad_resiliente

@app.get("/resiliencia/estado")
def estado_resiliencia():
    return elegibilidad_resiliente.resumen()
```

## Qué cuenta como fallo del circuito

| Respuesta de Elegibilidad | Circuito | Lo que recibe la Saga |
| :--- | :--- | :--- |
| Timeout o error de conexión | Cuenta como fallo | La misma excepción de `requests` |
| 5xx | Cuenta como fallo | La respuesta 5xx |
| 2xx o 4xx (incluye 422 no apto) | Cuenta como éxito | La respuesta |
| Circuito abierto | No se llama a Elegibilidad | `CircuitoAbierto` |
| Bulkhead lleno | No cuenta (no se llamó) | `BulkheadLleno` |

`CircuitoAbierto` y `BulkheadLleno` heredan de `DependenciaNoDisponible`.

## Configuración

| Variable | Por defecto | Uso |
| :--- | :--- | :--- |
| `CB_UMBRAL_FALLOS` | `3` | Fallos consecutivos para abrir |
| `CB_RECUPERACION_S` | `10` | Segundos abierto antes de pasar a semiabierto |
| `CB_ACTIVO` | `true` | `false` para el experimento sin circuito |
| `BULKHEAD_MAX_CONCURRENTES` | `5` | Llamadas simultáneas a Elegibilidad |
| `BULKHEAD_MAX_ESPERA_S` | `0` | Espera por un cupo antes de rechazar |
| `BULKHEAD_ACTIVO` | `true` | `false` para el experimento sin bulkhead |

El timeout es el mismo de la Saga (`SAGA_STEP_TIMEOUT`).

Correr el BFF con un solo worker de uvicorn (como hoy): el estado del circuito vive en memoria del proceso.

## Evidencia

Cada cambio de estado del circuito y cada rechazo del bulkhead queda como una línea JSON en el log del BFF:

```
{"evento": "circuito_transicion", "circuito": "elegibilidad", "desde": "CERRADO", "hacia": "ABIERTO", "fallos_consecutivos": 3, "motivo": "ConnectionError", ...}
{"evento": "bulkhead_rechazo", "bulkhead": "elegibilidad", "max_concurrentes": 5, ...}
```
