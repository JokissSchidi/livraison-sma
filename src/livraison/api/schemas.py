"""Formats d'échange communs de l'API."""
from pydantic import BaseModel, Field


class PointGPS(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
