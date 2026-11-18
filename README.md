# Sistema distribuido resiliente — Donación de sangre (Forma N)

Agendamiento de donaciones de sangre como una **Saga orquestada** entre tres servicios de dominio (Cupos, Elegibilidad y Registro), expuesta por un **API Gateway** y un **BFF**, y protegida con **timeouts, Circuit Breaker y Bulkhead** sobre la dependencia frágil (Elegibilidad).

## Arquitectura

| Servicio | Puerto | Responsabilidad |
|---|---|---|
| `api-gateway` | **8080 (único expuesto)** | Valida el JWT, limita peticiones por usuario y enruta `/api/*` al BFF |
| `bff` | 5000 | Orquesta la Saga, agrega datos de Cupos y Registro, aplica CB y Bulkhead a Elegibilidad |
| `cupos-service` | 5001 | Reserva y libera cupos de una jornada (SQLite propio) |
| `elegibilidad-service` | 5002 | Valida si el donante es apto. Simula un servicio externo lento e inestable |
| `registro-service` | 5003 | Registra y anula la donación agendada (SQLite propio) |

## Levantar el sistema

Requisito: Docker Desktop.

```bash
docker compose up --build
```

Todos los servicios tienen healthcheck; el gateway queda disponible en `http://localhost:8080` cuando los cinco están `healthy`. Para partir de cero (borra las bases): `docker compose down -v`.

## Uso básico

**1. Obtener un token** (usuarios de prueba: `operador`/`operador123` con escritura, `lector`/`lector123` solo lectura):

```bash
curl -s -X POST http://localhost:8080/auth/token \
  -H "Content-Type: application/json" \
  -d '{"usuario": "operador", "clave": "operador123"}'
```

**2. Agendar una donación** (la `Idempotency-Key` hace que un reintento no duplique la reserva):

```bash
curl -s -X POST http://localhost:8080/api/v1/donaciones/agendar \
  -H "Authorization: Bearer <TOKEN>" -H "Idempotency-Key: demo-1" \
  -H "Content-Type: application/json" \
  -d '{"rut_donante": "12.345.678-9", "nombre_donante": "Ana Pérez", "id_jornada": "JORNADA-01", "horario": "10:00", "edad": 30, "peso_kg": 70}'
```

Respuestas: `201 CONFIRMADA` (apto), `201 PENDIENTE_VALIDACION` (Elegibilidad no respondió y se degradó), `422` (no apto, compensado), `409` (sin cupos), `5xx` (falla compensada).

**3. Resumen del donante** (el BFF combina Registro y Cupos en una sola respuesta):

```bash
curl -s http://localhost:8080/api/v1/donantes/12.345.678-9/resumen -H "Authorization: Bearer <TOKEN>"
```

Jornadas precargadas: `JORNADA-01` (10 cupos), `JORNADA-02` (5), `JORNADA-UNICA` (1), `JORNADA-LLENA` (0).
RUT no apto en el historial de Elegibilidad: `00.000.000-0`, `11.111.111-K`, `33.333.333-3`.

> En PowerShell usa `curl.exe` en lugar de `curl`.

## Provocar los escenarios de falla

### Opción 1: un comando por escenario

Con el sistema levantado, desde la raíz del repositorio (solo usa Python estándar):

```bash
python experimentos/demo.py feliz                    # 1. Camino feliz e idempotencia
python experimentos/demo.py compensacion             # 2. Falla del último paso -> compensación en orden inverso
python experimentos/demo.py circuito                 # 3. Elegibilidad lenta y luego caída, con Circuit Breaker
python experimentos/demo.py circuito --sin-patron    #    el mismo experimento sin Circuit Breaker
python experimentos/demo.py bulkhead                 # 3. 60 solicitudes simultáneas con Elegibilidad lenta, con Bulkhead
python experimentos/demo.py bulkhead --sin-patron    #    el mismo experimento sin Bulkhead
python experimentos/demo.py ratelimit                # Ráfaga contra el gateway (HTTP 429)
python experimentos/demo.py todos                    # Todo lo anterior + tabla comparativa
```

Cada comando levanta el sistema con la configuración que necesita (por ejemplo, el CB apagado en `--sin-patron`), activa la falla, envía las solicitudes **por el gateway con JWT**, verifica que Cupos y Registro sigan consistentes y guarda la salida, los CSV, el estado del circuito y los eventos del BFF en `experimentos/resultados/<fecha>/`. Al terminar restaura la configuración por defecto.

Para los escenarios de carga el script sube el rate limit (`RATE_LIMIT_RPM`); con el valor por defecto (15 por minuto y usuario) el gateway rechazaría la mayor parte de la ráfaga con 429 antes de llegar al BFF.

La corrida de referencia está versionada en [`experimentos/evidencia/`](experimentos/evidencia/).

### Opción 2: a mano

Los servicios internos no tienen puertos al host, así que los endpoints de caos se llaman desde dentro de su contenedor. Por ejemplo, para poner lenta a Elegibilidad:

```bash
docker compose exec -T elegibilidad-service python -c "import urllib.request as u; print(u.urlopen(u.Request('http://localhost:5002/config/latencia', data=b'{\"latencia_segundos\": 5}', headers={'Content-Type': 'application/json'}, method='POST')).read().decode())"
```

| Escenario | Servicio | Endpoint | Body |
|---|---|---|---|
| Elegibilidad lenta | `elegibilidad-service:5002` | `POST /config/latencia` | `{"latencia_segundos": 5}` |
| Elegibilidad caída | `elegibilidad-service:5002` | `POST /config/falla` | `{"simular_falla": true}` |
| Registro cae (no graba) | `registro-service:5003` | `POST /config/falla` | `{"simular_falla": true}` |
| Registro graba y responde tarde (resultado incierto) | `registro-service:5003` | `POST /config/latencia` | `{"latencia_segundos": 5}` |
| Volver a la normalidad | ambos | `POST /config/reset` | — |

Después de activar una falla, agenda como en la sección anterior y observa la respuesta.

| Escenario | Qué hacer | Qué se espera |
|---|---|---|
| 1. Éxito | Agendar sin fallas | `201 CONFIRMADA`; cupo `RESERVADO` y cita registrada |
| 2. Fallo con compensación | Registro graba y responde tarde, y agendar | `500` "compensada y cupo liberado"; en el log del BFF: `anular_registro` y luego `liberar_cupo` |
| 3. Cascada contenida | Elegibilidad lenta, agendar varias veces; luego caída | Las 3 primeras tardan ~3,5 s y el circuito se abre; las siguientes responden en milisegundos con `PENDIENTE_VALIDACION` |

### Leer la evidencia

```bash
# Estado y métricas del Circuit Breaker y del Bulkhead
docker compose exec -T bff python -c "import urllib.request as u; print(u.urlopen('http://localhost:5000/resiliencia/estado').read().decode())"

# Transiciones del circuito y rechazos del bulkhead (líneas JSON)
docker compose logs bff | grep -E "circuito_transicion|bulkhead_rechazo"

# Compensaciones ejecutadas por la Saga
docker compose logs bff | grep "Ejecutando compensación"
```

## Configuración

Variables del `docker-compose.yml` (se pueden sobrescribir al levantar, por ejemplo `CB_ACTIVO=false docker compose up -d`):

| Variable | Defecto | Uso |
|---|---|---|
| `SAGA_STEP_TIMEOUT` | `3.5` | Timeout (s) de cada paso de la Saga |
| `SAGA_COMP_RETRIES` / `SAGA_COMP_BACKOFF` | `3` / `0.3` | Reintentos y backoff de las compensaciones |
| `CB_ACTIVO` | `true` | Activa el Circuit Breaker sobre Elegibilidad |
| `CB_UMBRAL_FALLOS` / `CB_RECUPERACION_S` | `3` / `10` | Fallos seguidos para abrir y segundos hasta probar de nuevo |
| `BULKHEAD_ACTIVO` | `true` | Activa el Bulkhead sobre Elegibilidad |
| `BULKHEAD_MAX_CONCURRENTES` / `BULKHEAD_MAX_ESPERA_S` | `5` / `0` | Llamadas simultáneas permitidas y espera por un lugar |
| `RATE_LIMIT_RPM` | `15` | Peticiones por minuto y usuario en el gateway |
| `BFF_TIMEOUT_SEGUNDOS` | `40` | Timeout del gateway hacia el BFF |
| `JWT_SECRET` | (clave de desarrollo) | Secreto para firmar los tokens |

## Pruebas

Cada servicio tiene sus pruebas en `tests/` (con `pytest` y las dependencias de `requirements-dev.txt`):

```bash
cd bff && python -m pytest -q
```

## Estructura

```
api-gateway/           Gateway: JWT, rate limit y proxy al BFF
bff/                   BFF: Saga (src/saga.py), agregación y resiliencia (src/resiliencia/)
cupos-service/         Cupos con SQLite (WAL, BEGIN IMMEDIATE, idempotencia por id_saga)
elegibilidad-service/  Validador externo simulado con inyección de caos
registro-service/      Registro con SQLite e inyección de fallas
experimentos/          demo.py y evidencia de la corrida de referencia
```

## Equipo

| Integrante | Componentes |
|---|---|
| Jesús Guevara (`Xaiwu`) | API Gateway, BFF y orquestador Saga |
| Francisco Cifuentes (`pcfuenr`) | Servicios de dominio: Cupos, Registro y Elegibilidad |
| Rodrigo Bascuñán (`Rodri0819`) | Circuit Breaker, Bulkhead, docker-compose y experimentos |
