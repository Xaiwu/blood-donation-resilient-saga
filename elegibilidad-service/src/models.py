from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class ValidarElegibilidadRequest(BaseModel):
    """Payload de entrada para validar la aptitud médica de un donante."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True, allow_inf_nan=False)

    id_saga: Optional[str] = Field(default=None, min_length=1, description="Identificador único de la transacción SAGA")
    rut_donante: str = Field(..., min_length=1, description="RUT del donante a validar")
    nombre_donante: Optional[str] = Field(default=None, description="Nombre completo del donante")
    edad: Optional[int] = Field(default=None, description="Edad del donante en años (rango apto: 18 a 65)")
    peso_kg: Optional[float] = Field(default=None, description="Peso corporal en kilogramos (mínimo apto: 50.0 kg)")
    apto: Optional[bool] = Field(default=None, description="Indicador explícito de aptitud médica para pruebas")
    elegible: Optional[bool] = Field(default=None, description="Alias opcional de aptitud médica para pruebas")


class ValidarElegibilidadResponse(BaseModel):
    """Respuesta de validación médica de elegibilidad."""

    ok: bool = True
    apto: bool = True
    elegible: bool = True
    rut_donante: str
    id_saga: Optional[str] = None
    estado: Literal["APTO", "NO_APTO"] = "APTO"
    motivo: str


class ConfigurarLatenciaRequest(BaseModel):
    """Payload para inyectar latencia artificial en el servicio de elegibilidad."""

    model_config = ConfigDict(allow_inf_nan=False)

    latencia_segundos: float = Field(
        ..., ge=0.0, le=60.0, description="Segundos de demora por petición (0.0 a 60.0)"
    )


class ConfigurarFallaRequest(BaseModel):
    """Payload para activar o desactivar la simulación de caída (HTTP 5xx)."""

    simular_falla: bool = Field(default=True, description="True para forzar error HTTP 5xx")
    codigo_falla: int = Field(default=500, ge=500, le=599, description="Código de error HTTP 5xx a simular")


class EstadoCaosResponse(BaseModel):
    """Respuesta con el estado actual del simulador de caos."""

    ok: bool = True
    simular_falla: bool
    latencia_segundos: float
    codigo_falla: int = 500
    mensaje: str
