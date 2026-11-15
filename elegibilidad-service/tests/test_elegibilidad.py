"""Pruebas unitarias de validación clínica e inyección de caos para elegibilidad-service."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.caos import (
    FallaSimuladaError,
    configurar_falla,
    configurar_latencia,
    obtener_estado_caos,
    resetear_caos,
)
from src.main import DonanteNoAptoError, evaluar_elegibilidad

try:
    from fastapi.testclient import TestClient
    from src.main import app

    FASTAPI_DISPONIBLE = True
except ImportError:
    FASTAPI_DISPONIBLE = False


class TestElegibilidadLogicaYCaos(unittest.TestCase):
    """Pruebas sobre la lógica de negocio y el simulador de caos."""

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

    def test_rechazo_por_edad_y_peso_fuera_de_rango(self) -> None:
        """Verifica que menores de 18, mayores de 65 o con peso < 50 kg lancen DonanteNoAptoError."""
        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=17, peso_kg=65.0)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=66, peso_kg=65.0)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=30, peso_kg=49.5)

    def test_rechazo_por_rut_inhabilitado_o_flag_explicito(self) -> None:
        """Verifica rechazo clínico por RUT en lista negra o flag apto=False."""
        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("00.000.000-0", edad=30, peso_kg=70.0)

        with self.assertRaises(DonanteNoAptoError):
            evaluar_elegibilidad("12.345.678-9", edad=30, peso_kg=70.0, apto=False)

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
    def test_endpoints_caos_latencia_falla_y_reset(self, mock_sleep) -> None:
        """Verifica los endpoints /elegibilidad/config/* y /config/* para la demostración ABET 6."""
        # 1. Configurar latencia en /elegibilidad/config/latencia
        r_lat = self.client.post("/elegibilidad/config/latencia", json={"latencia_segundos": 5.0})
        self.assertEqual(r_lat.status_code, 200)
        self.assertEqual(r_lat.json()["latencia_segundos"], 5.0)

        r_val = self.client.post("/elegibilidad/validar", json={"rut_donante": "12.345.678-9"})
        self.assertEqual(r_val.status_code, 200)
        mock_sleep.assert_called_once_with(5.0)

        # 2. Activar falla HTTP 500 en /config/falla
        r_falla = self.client.post("/config/falla", json={"simular_falla": True})
        self.assertEqual(r_falla.status_code, 200)
        self.assertTrue(r_falla.json()["simular_falla"])

        r_caido = self.client.post("/elegibilidad/validar", json={"rut_donante": "12.345.678-9"})
        self.assertEqual(r_caido.status_code, 500)

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
