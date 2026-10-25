from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "production"
    telegram_bot_token: str = ""
    owner_telegram_id: int | None = None

    postgres_db: str = "tradingbot"
    postgres_user: str = "tradingbot"
    postgres_password: str
    database_url: str
    redis_url: str = "redis://redis:6379/0"

    company_name: str = "شرکت بازرگانی"
    card_number: str = ""
    card_holder: str = ""
    iban: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
