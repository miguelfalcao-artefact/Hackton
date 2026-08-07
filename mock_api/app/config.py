"""Runtime settings, all overridable by environment variable."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "ecommerce-api"

    database_url: str = "postgresql+psycopg://shop:shop@postgres:5432/shop"
    db_pool_size: int = 10
    db_connect_retries: int = 30
    db_connect_backoff_s: float = 1.0

    log_level: str = "INFO"
    log_dir: str = "/var/log/app"
    log_to_file: bool = True
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backups: int = 5

    seed_on_start: bool = True
    reset_on_start: bool = False
    seed_value: int = 42

    # A payment is declined when the order total exceeds this ceiling, or when
    # the payment_token starts with "tok_decline". Both rules are deterministic
    # on purpose: the traffic generator needs to be able to *provoke* a decline.
    payment_decline_above_cents: int = 500_000

    rate_limit_free_per_min: int = 60
    rate_limit_pro_per_min: int = 600
    rate_limit_enterprise_per_min: int = 6000

    # Format: "id:key:tier,id:key:tier". Tier is free | pro | enterprise.
    api_keys: str = (
        "acme-retail:acme-retail-key:enterprise,"
        "bolt-shop:bolt-shop-key:pro,"
        "quickcart:quickcart-key:free,"
        "nightowl-bot:nightowl-bot-key:free"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
