"""
Esquemas Pydantic y DTOs para el Backend For Frontend (BFF).
Define los contratos de entrada/salida para la Saga y agregación de donantes.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SolicitudAgendarDonacion(BaseModel):
    """Payload de entrada para la orquestación de la Saga 'Agendar una Donación'."""
    rut_donante: str = Field(..., description="RUT o identificación del donante")
    nombre_donante: str = Field(..., description="Nombre completo del donante")
    id_jornada: str = Field(..., description="Identificador de la jornada de donación")
    horario: str = Field(..., description="Horario agendado (ej. 10:30)")
    email_donante: Optional[str] = Field(None, description="Correo electrónico del donante")
    telefono_donante: Optional[str] = Field(None, description="Teléfono de contacto")
    edad: Optional[int] = Field(None, ge=18, le=65, description="Edad del donante")
    peso_kg: Optional[float] = Field(None, ge=50.0, description="Peso corporal en kg")
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
    proxima_cita: Optional[Dict[str, Any]] = None
    estado_elegibilidad_reciente: Optional[str] = None
