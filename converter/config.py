"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Minimal .env loader for running without Docker. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ai_enabled: bool = field(default_factory=lambda: _bool(os.getenv("AI_ENABLED"), True))
    # Claude is only called when a user clicks "Apply notes", "Check for mistakes" or "Suggest fixes".
    # Set AI_AUTO_MAP=true to also have Claude refine the column mapping automatically on every upload.
    ai_auto_map: bool = field(default_factory=lambda: _bool(os.getenv("AI_AUTO_MAP"), False))
    ai_mask_phones: bool = field(default_factory=lambda: _bool(os.getenv("AI_MASK_PHONES"), True))
    mapping_model: str = field(default_factory=lambda: os.getenv("MAPPING_MODEL", "claude-sonnet-5-5"))
    repair_model: str = field(default_factory=lambda: os.getenv("REPAIR_MODEL", "claude-haiku-5-5"))
    ai_sample_values: int = field(default_factory=lambda: int(os.getenv("AI_SAMPLE_VALUES", "15")))
    schema_path: Path = field(
        default_factory=lambda: Path(os.getenv("SCHEMA_PATH", str(PROJECT_ROOT / "schemas" / "target_schema.yaml")))
    )
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))))
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "password").strip().lower())
    admin_username: str = field(default_factory=lambda: os.getenv("ADMIN_USERNAME", ""))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    import_workers: int = field(default_factory=lambda: max(1, int(os.getenv("IMPORT_WORKERS", "2"))))
    file_retention_days: int = field(default_factory=lambda: int(os.getenv("FILE_RETENTION_DAYS", "30")))
    timezone: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "America/New_York"))

    @property
    def ai_available(self) -> bool:
        return self.ai_enabled and bool(self.anthropic_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "converter.db"


def get_settings() -> Settings:
    return Settings()
