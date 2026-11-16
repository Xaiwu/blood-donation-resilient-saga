from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class RegistrarDonacionRequest(BaseModel):
    """Payload de entrada para registrar una cita médica de donación."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    id_saga: Optional[str] = Field(default=None, min_length=1, description="Identificador único de la transacción SAGA")
    rut_donante: str = Field(..., min_length=1, description="RUT del donante")
    nombre_donante: str = Field(..., min_length=1, description="Nombre completo del donante")
    id_jornada: str = Field(..., min_length=1, description="Identificador de la jornada")
    horario: str = Field(..., min_length=1, description="Bloque horario agendado")
    estado_elegibilidad: Literal["APTO", "PENDIENTE_VALIDACION"] = Field(
        default="APTO", description="Resultado de elegibilidad médica ('APTO' o 'PENDIENTE_VALIDACION')"
    )


class RegistrarDonacionResponse(BaseModel):
    """Respuesta exitosa al confirmar el registro de una cita médica."""

    ok: bool = True
    cita_id: str
    id_saga: str
    estado_elegibilidad: str
    estado: str = "CONFIRMADA"
    idempotente: bool = False


class AnularDonacionResponse(BaseModel):
    """Respuesta exitosa al anular una cita médica por compensación SAGA."""

    ok: bool = True
    cita_id: str
    id_saga: str
    estado: str = "ANULADA"
    idempotente: bool = False
    mensaje: str


class CitaDonanteItem(BaseModel):
    """Salida esperada al consultar las citas médicas activas de un donante."""

    cita_id: str
    rut_donante: str
    nombre_donante: str
    id_jornada: str
    horario: str
    estado_elegibilidad: str
    estado: str = "CONFIRMADA"
