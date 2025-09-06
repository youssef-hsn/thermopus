from __future__ import annotations

from datetime import timedelta
from typing import Dict, List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from worker.src.util.duration import parse_duration

class Settings(BaseSettings):
    """
    Central app configuration.
    Environment variables (examples):
      WORKER_PINS=4,17,22
      WORKER_SCAN_INTERVAL=5s
      WORKER_READ_INTERVAL=2s
      WORKER_READ_TIMEOUT=1s
      WORKER_METRICS_PORT=8000
      WORKER_LOG_LEVEL=INFO
      WORKER_USE_INOTIFY=false
      WORKER_LABEL_MAP={"28-00000abcdef0":"TankInlet","28-00000fedcba":"RoofProbe3"}
    """
    model_config = SettingsConfigDict(
        env_prefix="WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        validate_assignment=True,
        extra="ignore",
    )

    # One 1-Wire bus per GPIO (BCM numbering) if you enable multiple overlays
    pins: List[int] = Field(default_factory=lambda: [4], description="BCM GPIO pins used for 1-Wire")

    # Intervals / timeouts
    scan_interval: timedelta = Field(default=timedelta(seconds=5), description="How often to rescan for sensors")
    read_interval: timedelta = Field(default=timedelta(seconds=2), description="How often to read each sensor")
    read_timeout: timedelta = Field(default=timedelta(seconds=1), description="Per-sensor read timeout")

    # Metrics / logging
    metrics_port: int = Field(default=8000, ge=1, le=65535, description="Prometheus HTTP port")
    log_level: str = Field(default="INFO", description="Python logging/structlog level")
    use_inotify: bool = Field(default=False, description="Watch sysfs with inotify instead of polling")

    # Optional friendly names: ROM → human label
    label_map: Dict[str, str] = Field(default_factory=dict)

    # Optional: mark specific ROMs disabled at startup
    disabled_roms: List[str] = Field(default_factory=list)

    # Freshness window for declaring STALE
    freshness_window: timedelta = Field(default=timedelta(seconds=10))

    @field_validator("pins", mode="before")
    @classmethod
    def _parse_pins(cls, v: object) -> List[int]:
        if v is None:
            return [4]
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return [int(p) for p in parts]
        raise ValueError("pins must be a list[int] or comma-separated string")

    @field_validator("scan_interval", "read_interval", "read_timeout", "freshness_window", mode="before")
    @classmethod
    def _parse_durations(cls, v: object) -> timedelta:
        return parse_duration(v)  # raises ValueError on bad input

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_level(cls, v: object) -> str:
        return str(v).strip().upper()