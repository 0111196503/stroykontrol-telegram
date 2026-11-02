from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    redis_url: str = "redis://redis:6379/0"
    jwt_secret: str
    jwt_expire_minutes: int = 720
    app_base_url: str = "http://localhost:8000"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_url: str = ""
    timezone: str = "Europe/Moscow"
    report_hour: int = 18
    report_minute: int = 0
    upload_dir: str = "/app/uploads"
    max_upload_mb: int = 20
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "stroykontrol"
    minio_secret_key: str = ""
    minio_bucket: str = "stroykontrol-photos"
    minio_secure: bool = False
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""

settings = Settings()
