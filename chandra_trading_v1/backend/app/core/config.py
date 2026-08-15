from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    app_secret_key: str = "change-me"
    credential_encryption_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    database_url: str = "sqlite:///./chandra.db"
    mt5_path: str = ""
    default_symbol: str = "XAUUSD"
    default_timeframe: str = "M1"
    default_lot: float = 0.01

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
