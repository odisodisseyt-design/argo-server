from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PACKS_DIR = ROOT / "server" / "packs"
DB_PATH = DATA_DIR / "world.db"


@dataclass(slots=True)
class Settings:
    host: str = os.getenv("TW_HOST", "0.0.0.0")
    port: int = int(os.getenv("TW_PORT", "8000"))
    public_base: str = os.getenv("TW_PUBLIC_BASE", "https://v3310666.hosted-by-vdsina.ru")
    min_client_version: int = int(os.getenv("TW_MIN_CLIENT_VERSION", "1"))
    provider: str = os.getenv("TW_PROVIDER", "ollama")
    model: str = os.getenv("TW_MODEL", "tw-qwen35-2b")
    intent_model: str = os.getenv("TW_INTENT_MODEL", "tw-qwen35-08b")
    narrator_model: str = os.getenv("TW_NARRATOR_MODEL", "tw-qwen35-2b")
    ollama_base: str = os.getenv("TW_OLLAMA_BASE", "http://127.0.0.1:11435")
    openai_base: str = os.getenv("TW_OPENAI_BASE", "https://openrouter.ai/api/v1")
    openai_key: str = os.getenv("TW_OPENAI_KEY", "")
    gemini_key: str = os.getenv("TW_GEMINI_KEY", "")
    gemini_model: str = os.getenv("TW_GEMINI_MODEL", "gemini-2.5-flash")
    temperature: float = float(os.getenv("TW_TEMPERATURE", "0.4"))
    num_ctx: int = int(os.getenv("TW_NUM_CTX", "4096"))
    request_timeout: float = float(os.getenv("TW_REQUEST_TIMEOUT", "120"))
    intent_timeout: float = float(os.getenv("TW_INTENT_TIMEOUT", "5"))
    narrator_timeout: float = float(os.getenv("TW_NARRATOR_TIMEOUT", "8"))
    default_pack: str = os.getenv("TW_DEFAULT_PACK", "refuge")
    operator_password: str = os.getenv("TW_OPERATOR_PASSWORD", "change-me")
    log_dir: Path = field(default_factory=lambda: DATA_DIR / "logs")
    backup_dir: Path = field(default_factory=lambda: DATA_DIR / "backups")
    locks_dir: Path = field(default_factory=lambda: DATA_DIR / "locks")
    updates_dir: Path = field(default_factory=lambda: DATA_DIR / "updates")
    db_path: Path = field(default_factory=lambda: Path(os.getenv("TW_DB_PATH", str(DB_PATH))))
    models_path: Path = field(default_factory=lambda: ROOT / "server" / "models.json")
    default_model_id: str = os.getenv("TW_DEFAULT_MODEL_ID", "fast")
    cheapllm_base: str = os.getenv("TW_CHEAPLLM_BASE", "https://cheapllm.shop/v1")
    cheapllm_key: str = os.getenv("CHEAPLLM_API_KEY", "")
    cheapllm_model: str = os.getenv("TW_CHEAPLLM_MODEL", "deepseek-v4.1-flash-1017")
    turn_max_chars: int = int(os.getenv("TW_TURN_MAX_CHARS", "1000"))
    orders_per_minute: int = int(os.getenv("TW_ORDERS_PER_MINUTE", "30"))
    orders_per_hour: int = int(os.getenv("TW_ORDERS_PER_HOUR", "200"))
    max_active_campaigns: int = int(os.getenv("TW_MAX_ACTIVE_CAMPAIGNS", "10"))
    idempotency_ttl_hours: int = int(os.getenv("TW_IDEMPOTENCY_TTL_HOURS", "24"))
    require_idempotency: bool = os.getenv("TW_REQUIRE_IDEMPOTENCY", "0").lower() in ("1", "true", "yes")
    order_deadline_seconds: int = int(os.getenv("TW_ORDER_DEADLINE_SECONDS", "3600"))
    lock_retry_after: int = int(os.getenv("TW_LOCK_RETRY_AFTER", "2"))
    resolution_stale_seconds: int = int(os.getenv("TW_RESOLUTION_STALE_SECONDS", "300"))

    def ensure_dirs(self) -> None:
        for path in (DATA_DIR, self.log_dir, self.backup_dir, self.locks_dir, self.updates_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
