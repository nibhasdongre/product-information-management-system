"""Settings and model specs, loaded from config/*.yaml with env overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PACKAGE_ROOT / "config"


@dataclass(frozen=True)
class RetrySettings:
    tries: int
    base_s: float
    cap_s: float


@dataclass(frozen=True)
class Settings:
    catalog_dir: Path
    output_dir: Path
    render_dpi: int
    default_model: str
    retry: RetrySettings
    batch_max_requests: int
    batch_max_bytes: int


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str
    max_output_tokens: int
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    batch_discount: float

    def cost_usd(self, input_tokens: int, output_tokens: int, batch: bool = False) -> float:
        cost = (input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok) / 1_000_000
        return cost * self.batch_discount if batch else cost


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (PACKAGE_ROOT / path).resolve()


def _read_yaml(name: str, config_dir: Path) -> dict:
    with open(config_dir / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(config_dir: Path = CONFIG_DIR) -> Settings:
    raw = _read_yaml("settings.yaml", config_dir)
    retry = raw["retry"]
    batch = raw["batch"]
    return Settings(
        catalog_dir=_resolve(os.environ.get("PIM_CATALOG_DIR", raw["catalog_dir"])),
        output_dir=_resolve(os.environ.get("PIM_OUTPUT_DIR", raw["output_dir"])),
        render_dpi=int(raw["render_dpi"]),
        default_model=raw["default_model"],
        retry=RetrySettings(tries=int(retry["tries"]), base_s=float(retry["base_s"]), cap_s=float(retry["cap_s"])),
        batch_max_requests=int(batch["max_requests"]),
        batch_max_bytes=int(batch["max_mb"]) * 1024 * 1024,
    )


def load_models(config_dir: Path = CONFIG_DIR) -> dict[str, ModelSpec]:
    raw = _read_yaml("models.yaml", config_dir)["models"]
    return {name: ModelSpec(name=name, **fields) for name, fields in raw.items()}


def get_model(models: dict[str, ModelSpec], name: str) -> ModelSpec:
    try:
        return models[name]
    except KeyError:
        known = ", ".join(sorted(models))
        raise KeyError(f"Unknown model '{name}'. Configured models: {known}") from None
