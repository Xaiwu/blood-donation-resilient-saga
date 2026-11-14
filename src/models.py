from pydantic import BaseModel, ConfigDict, Field


class ReservaCupoRequest(BaseModel):
    """Payload de entrada para reservar un cupo en una jornada"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    id_saga: str = Field(..., min_length=1, description="Identificador único de la transacción SAGA")
    rut_donante: str = Field(..., min_length=1, description="RUT del donante")
    id_jornada: str = Field(..., min_length=1, description="Identificador de la jornada")
    horario: str = Field(..., min_length=1, description="Bloque horario solicitado (ej. 09:00)")

class ReservaCupoResponse(BaseModel):
    """Respuesta exitosa al reservar un cupo."""

    ok: bool = True
    cupo_id: str
    id_saga: str
    id_jornada: str
    horario: str
    estado: str = "RESERVADO"
    idempotente: bool = False

class LiberarCupoRequest(BaseModel):
    """Payload de entrada para liberar un cupo"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    id_saga: str = Field(..., min_length=1, description="ID de la SAGA a compensar")

class LiberarCupoResponse(BaseModel):
    """Respuesta exitosa al liberar un cupo."""

    ok: bool = True
    cupo_id: str
    id_saga: str
    estado: str = "LIBERADO"
    idempotente: bool = False
    mensaje: str

class CupoDonanteItem(BaseModel):
    """Salida esperada al consultar los cupos de un donante"""

    id_cupo: str
    id_jornada: str
    horario: str
    estado: str = "RESERVADO"
