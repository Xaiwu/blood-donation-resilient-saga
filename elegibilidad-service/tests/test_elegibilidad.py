"""Pruebas unitarias de validación clínica e inyección de caos para elegibilidad-service."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.caos import (
    FallaSimuladaError,
    _parse_codigo_falla,
    _parse_float,
    configurar_falla,
    configurar_latencia,
    obtener_estado_caos,
    resetear_caos,
)
from src.main import DonanteNoAptoError, evaluar_elegibilidad, normalizar_rut

try:
    from fastapi.testclient import TestClient
    from src.main import app

    FASTAPI_DISPONIBLE = True
except ImportError:
    FASTAPI_DISPONIBLE = False


class TestElegibilidadLogicaYCaos(unittest.TestCase):
    """Pruebas sobre la lógica de negocio, historial médico y simulador de caos."""

    def setUp(self) -> None:
        resetear_caos()

    def tearDown(self) -> None:
        resetear_caos()

    def test_donante_apto_con_y_sin_parametros_opcionales(self) -> None:
        """Verifica que un donante con parámetros válidos o solo RUT sea evaluado como APTO."""
        r1 = evaluar_elegibilidad("12.345.678-9", edad=28, peso_kg=72.5, id_saga="saga-01")
        self.assertTrue(r1["ok"])
        self.assertTrue(r1["apto"])
        self.assertTrue(r1["elegible"])
        self.assertEqual(r1["estado"], "APTO")

        r2 = evaluar_elegibilidad("12.345.678-9", id_saga="saga-02")
        self.assertTrue(r2["apto"])
        self.assertEqual(r2["estado"], "APTO")

    def test_rechazo_por_edad_y_peso_fuera_de_rango_o_nan_inf(self) -> None:
        """Verifica que menores de 18, mayores de 65, peso < 50 kg o NaN/inf lancen DonanteNoAptoError."""
        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=17, peso_kg=65.0)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=66, peso_kg=65.0)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=30, peso_kg=49.5)

        for peso_invalido in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(DonanteNoAptoError):
                evaluar_elegibilidad("12.345.678-9", edad=30, peso_kg=peso_invalido)

    def test_rechazo_por_historial_medico_rut_normalizado_vacio_y_flags(self) -> None:
        """Verifica normalización de RUTs en el almacén médico, RUT vacío/inválido y flags apto/elegible=False."""
        self.assertEqual(normalizar_rut(" 11.111.111-k "), "11111111K")

        for rut_vacio in ("", "   ", "---", "..."):
            with self.assertRaises(DonanteNoAptoError):
                evaluar_elegibilidad(rut_vacio, edad=30, peso_kg=70.0)

        with self.assertRaises(DonanteNoAptoError) as ctx_hemo:
            evaluar_elegibilidad("00.000.000-0", edad=30, peso_kg=70.0)
        self.assertIn("Hemoglobina", ctx_hemo.exception.motivo)

        with self.assertRaises(DonanteNoAptoError) as ctx_ventana:
            evaluar_elegibilidad("11111111-k", edad=30, peso_kg=70.0)
        self.assertIn("Donación de sangre reciente", ctx_ventana.exception.motivo)

        with self.assertRaises(DonanteNoAptoError) as ctx_cirugia:
            evaluar_elegibilidad("33.333.333-3", edad=30, peso_kg=70.0)
        self.assertIn("Cirugía menor", ctx_cirugia.exception.motivo)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=30, peso_kg=70.0, apto=False)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=30, peso_kg=70.0, elegible=False)

    def test_validacion_limites_caos_y_parsers_defensivos(self) -> None:
        """Verifica que latencias fuera de [0, 60], NaN/inf o códigos HTTP no 5xx sean rechazados."""
        for lat_invalida in (-0.5, 60.1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                configurar_latencia(lat_invalida)

        for codigo_invalido in (200, 404, 499, 600):
            with self.assertRaises(ValueError):
                configurar_falla(True, codigo_falla=codigo_invalido)

        self.assertEqual(_parse_codigo_falla("", 500), 500)
        self.assertEqual(_parse_codigo_falla("200", 500), 500)
        self.assertEqual(_parse_codigo_falla("503", 500), 503)
        self.assertEqual(_parse_float("invalido", 0.0), 0.0)
        self.assertEqual(_parse_float("999", 0.0), 60.0)

    @patch("src.caos.time.sleep")
    def test_inyeccion_latencia_y_falla_simulada(self, mock_sleep) -> None:
        """Verifica que el simulador de caos aplique time.sleep y lance FallaSimuladaError cuando se activa."""
        configurar_latencia(4.5)
        self.assertEqual(obtener_estado_caos()["latencia_segundos"], 4.5)

        res = evaluar_elegibilidad("12.345.678-9", edad=25, peso_kg=68.0, id_saga="saga-lenta")
        self.assertTrue(res["apto"])
        mock_sleep.assert_called_once_with(4.5)

        configurar_falla(True, codigo_falla=503)
        with self.assertRaises(FallaSimuladaError) as ctx:
            evaluar_elegibilidad("12.345.678-9", id_saga="saga-caida")
        self.assertEqual(ctx.exception.codigo_falla, 503)

        resetear_caos()
        estado = obtener_estado_caos()
        self.assertFalse(estado["simular_falla"])
        self.assertEqual(estado["latencia_segundos"], 0.0)

    def test_concurrencia_evaluacion_simultanea(self) -> None:
        """Verifica con múltiples hilos simultáneos que la evaluación y lectura de caos sea thread-safe."""
        total_hilos = 8
        barrera = threading.Barrier(total_hilos)

        def evaluar_concurrente(indice: int) -> str:
            barrera.wait()
            if indice % 2 == 0:
                res = evaluar_elegibilidad(f"12.345.67{indice}-9", edad=30, peso_kg=70.0, id_saga=f"saga-c-{indice}")
                return res["estado"]
            try:
                evaluar_elegibilidad("00.000.000-0", edad=30, peso_kg=70.0, id_saga=f"saga-c-{indice}")
                return "APTO"
            except DonanteNoAptoError:
                return "NO_APTO"

        with ThreadPoolExecutor(max_workers=total_hilos) as pool:
            resultados = list(pool.map(evaluar_concurrente, range(total_hilos)))

        self.assertEqual(resultados.count("APTO"), 4)
        self.assertEqual(resultados.count("NO_APTO"), 4)


@unittest.skipUnless(FASTAPI_DISPONIBLE, "FastAPI/TestClient no instalado en el entorno local")
class TestElegibilidadEndpoints(unittest.TestCase):
    """Pruebas de contrato HTTP con TestClient para validación y endpoints de caos."""

    def setUp(self) -> None:
        resetear_caos()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        resetear_caos()

    def test_contrato_http_validar_apto_y_no_apto_422(self) -> None:
        """Verifica respuestas HTTP 200 (apto) y 422 (no apto con apto=False) compatibles con la Saga."""
        resp_ok = self.client.post(
            "/elegibilidad/validar",
            json={"rut_donante": "12.345.678-9", "edad": 30, "peso_kg": 70.0},
            headers={"X-Saga-ID": "saga-http-01"},
        )
        self.assertEqual(resp_ok.status_code, 200)
        self.assertTrue(resp_ok.json()["apto"])
        self.assertEqual(resp_ok.json()["id_saga"], "saga-http-01")

        resp_no_apto = self.client.post(
            "/elegibilidad/validar",
            json={"rut_donante": "12.345.678-9", "edad": 25, "peso_kg": 45.0},
            headers={"X-Saga-ID": "saga-http-02"},
        )
        self.assertEqual(resp_no_apto.status_code, 422)
        self.assertFalse(resp_no_apto.json()["apto"])
        self.assertEqual(resp_no_apto.json()["estado"], "NO_APTO")

    @patch("src.caos.time.sleep")
    def test_endpoints_caos_latencia_falla_query_params_y_rechazos_422(self, mock_sleep) -> None:
        """Verifica los endpoints /elegibilidad/config/* y /config/*, query params y rechazos 422."""
        # 1. Configurar latencia por body y por query param (?latencia=2.5)
        r_lat = self.client.post("/elegibilidad/config/latencia", json={"latencia_segundos": 5.0})
        self.assertEqual(r_lat.status_code, 200)
        self.assertEqual(r_lat.json()["latencia_segundos"], 5.0)

        r_lat_qp = self.client.post("/config/latencia?latencia=2.5")
        self.assertEqual(r_lat_qp.status_code, 200)
        self.assertEqual(r_lat_qp.json()["latencia_segundos"], 2.5)

        # Rechazo 422 si latencia es negativa o > 60s
        r_lat_inv = self.client.post("/config/latencia", json={"latencia_segundos": -2.0})
        self.assertEqual(r_lat_inv.status_code, 422)

        r_val = self.client.post("/elegibilidad/validar", json={"rut_donante": "12.345.678-9"})
        self.assertEqual(r_val.status_code, 200)
        mock_sleep.assert_called_once_with(2.5)

        # 2. Activar falla HTTP 503 por query params (?simular_falla=true&codigo_falla=503) y consultar GET /config
        r_falla = self.client.post("/config/falla?simular_falla=true&codigo_falla=503")
        self.assertEqual(r_falla.status_code, 200)
        self.assertTrue(r_falla.json()["simular_falla"])
        self.assertEqual(r_falla.json()["codigo_falla"], 503)

        r_cfg = self.client.get("/elegibilidad/config")
        self.assertEqual(r_cfg.status_code, 200)
        self.assertEqual(r_cfg.json()["codigo_falla"], 503)

        # Rechazo 422 si el código de falla no es 5xx
        r_falla_inv = self.client.post("/config/falla", json={"simular_falla": True, "codigo_falla": 400})
        self.assertEqual(r_falla_inv.status_code, 422)

        r_caido = self.client.post("/elegibilidad/validar", json={"rut_donante": "12.345.678-9"})
        self.assertEqual(r_caido.status_code, 503)

        # El healthcheck sigue respondiendo 200 aunque el caos esté activo
        r_health = self.client.get("/health")
        self.assertEqual(r_health.status_code, 200)

        # 3. Resetear caos y volver a validar con éxito
        r_reset = self.client.post("/elegibilidad/config/reset")
        self.assertEqual(r_reset.status_code, 200)
        self.assertFalse(r_reset.json()["simular_falla"])

        r_ok = self.client.post("/elegibilidad/validar", json={"rut_donante": "12.345.678-9"})
        self.assertEqual(r_ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
