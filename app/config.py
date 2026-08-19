"""
Application Configuration — WB FBS Manager
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_name: str = "WB FBS Manager"
    app_version: str = "1.0.0"
    debug: bool = False
    secret_key: str = "change-this-secret-key"

    # Database (defaults to local SQLite for instant testing without Docker)
    database_url: str = "sqlite+aiosqlite:///./wbfbs.db"
    database_url_sync: str = "sqlite:///./wbfbs.db"

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

    # Security / JWT & Bootstrap Admin
    jwt_secret_key: str = "change-this-jwt-secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440  # 24 hours
    admin_username: str = "admin"
    admin_password: str = "admin_password"
    admin_email: str = "admin@example.com"
    cors_origins: str = "*"  # comma-separated origins or "*"

    # Encryption key for DB-stored credentials (Fernet)
    encryption_key: str = "change-this-encryption-key-32-b"

    # Polling intervals
    wb_polling_interval: int = 60       # seconds
    archive_process_hour: int = 3       # 03:00 daily
    token_refresh_interval: int = 1800  # 30 minutes

    # КриптоПро
    cryptopro_bin_path: str = "/usr/local/bin/cryptcp"
    cryptopro_cert_thumbprint: str = ""

    # Wildberries API
    wb_api_base_url: str = "https://marketplace-api.wildberries.ru"
    wb_marketplace_base_url: str = "https://marketplace-api.wildberries.ru"

    # Честный знак (True API / ГИС МТ)
    cz_api_base_url: str = "https://markirovka.crpt.ru"
    cz_api_sandbox_url: str = "https://markirovka.sandbox.crpt.tech"
    cz_use_sandbox: bool = False
    cz_oms_id: str = ""

    # Flower
    flower_port: int = 5555
    flower_basic_auth: str = "admin:admin_password"

    @property
    def cz_effective_url(self) -> str:
        return self.cz_api_sandbox_url if self.cz_use_sandbox else self.cz_api_base_url

    def model_post_init(self, __context):
        import socket
        if "@postgres" in self.database_url:
            try:
                socket.gethostbyname("postgres")
            except socket.gaierror:
                # Postgres host not found (running locally outside Docker container)
                # Fallback to local SQLite for instant testing
                self.database_url = "sqlite+aiosqlite:///./wbfbs.db"
                self.database_url_sync = "sqlite:///./wbfbs.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
