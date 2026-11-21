"""
Esquemas Pydantic y DTOs para el Backend For Frontend (BFF).
Define los contratos de entrada/salida para la Saga y agregación de donantes.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class SolicitudAgendarDonacion(BaseModel):
    """Payload de entrada para la orquestación de la Saga 'Agendar una Donación'."""
    model_config = ConfigDict(str_strip_whitespace=True)

    rut_donante: str = Field(..., min_length=1, description="RUT o identificación del donante")
    nombre_donante: str = Field(..., min_length=1, description="Nombre completo del donante")
    id_jornada: str = Field(..., min_length=1, description="Identificador de la jornada")
    horario: str = Field(..., min_length=1, description="Horario agendado (ej. 10:30)")
    email_donante: Optional[str] = Field(None, description="Correo electrónico del donante")
    telefono_donante: Optional[str] = Field(None, description="Teléfono de contacto")
    edad: Optional[int] = Field(None, ge=0, description="Edad del donante")
    peso_kg: Optional[float] = Field(
        None, ge=0.0, allow_inf_nan=False, description="Peso corporal en kg"
    )
    grupo_sanguineo: Optional[str] = Field(None, description="Grupo sanguíneo si se conoce")


class RespuestaAgendarDonacion(BaseModel):
    """Respuesta unificada de la Saga al cliente perimetral."""
    estado: str = Field(..., description="CONFIRMADA, PENDIENTE_VALIDACION o FALLIDA")
    id_saga: str = Field(..., description="Identificador de trazabilidad distribuida")
    mensaje: str = Field(..., description="Descripción del resultado de la operación")
    registro: Optional[Dict[str, Any]] = Field(None, description="Detalles del registro de cita")
    cupo_reservado: Optional[bool] = Field(False, description="Indica si se reservó el cupo")
    elegibilidad_estado: Optional[str] = Field(None, description="Resultado médico externo")
    compensado: bool = Field(False, description="Indica si se ejecutó compensación")


class ResumenDonante(BaseModel):
    """DTO de agregación para el endpoint del cliente BFF."""
    rut: str
    nombre: Optional[str] = None
    citas_historicas: List[Dict[str, Any]] = Field(default_factory=list)
    cupos_activos: List[Dict[str, Any]] = Field(default_factory=list)
    ultima_cita: Optional[Dict[str, Any]] = None
    estado_elegibilidad_reciente: Optional[str] = None
    datos_parciales: bool = False
    advertencias: List[str] = Field(default_factory=list)
