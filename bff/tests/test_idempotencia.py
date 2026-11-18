import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from saga import derivar_id_saga


@pytest.mark.parametrize("clave_vacia", [None, "", "   ", "\t\n"])
def test_clave_nula_o_vacia_genera_uuid4_valido(clave_vacia):
    id_saga = derivar_id_saga(clave_vacia)
    uuid_obj = uuid.UUID(id_saga)
    assert uuid_obj.version == 4


def test_claves_vacias_consecutivas_generan_ids_distintos():
    id_1 = derivar_id_saga(None)
    id_2 = derivar_id_saga(None)
    assert id_1 != id_2


def test_clave_arbitraria_genera_uuid5_determinista():
    clave = "peticion-cliente-donacion-xyz-987"
    id_a = derivar_id_saga(clave)
    id_b = derivar_id_saga(clave)

    assert id_a == id_b, "Múltiples llamadas con la misma clave deben generar el mismo id_saga"
    esperado = str(uuid.uuid5(uuid.NAMESPACE_URL, clave))
    assert id_a == esperado, "Debe coincidir exactamente con el UUIDv5 generado por namespace URL"


def test_clave_que_ya_es_uuid_se_conserva():
    uuid_original = str(uuid.uuid4())
    id_saga = derivar_id_saga(uuid_original)
    assert id_saga == uuid_original, "Si la clave ya es un UUID válido, debe conservarse idéntico"
