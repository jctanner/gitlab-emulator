import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Base URL for generating API URLs in responses
    BASE_URL: str = "http://localhost:8000"

    # Directory for bare git repositories
    DATA_DIR: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

    # SQLite database URL
    DATABASE_URL: str = ""

    # Secret key for JWT/session signing
    SECRET_KEY: str = "change-me-in-production"

    # Admin credentials (created on first startup)
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"

    # Runner registration token for official gitlab-runner validation
    RUNNER_REGISTRATION_TOKEN: str = "runner-registration-token"

    # Comma-separated host allowlist for include:remote.
    CI_REMOTE_INCLUDE_ALLOWED_HOSTS: str = "localhost,127.0.0.1"

    # Hostname for Caddy TLS / glab CLI integration
    HOSTNAME: str = "glemu.local"

    # Server config
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # SSH transport
    SSH_ENABLED: bool = True
    SSH_PORT: int = 2222
    SSH_HOST_KEY_PATH: str = ""

    model_config = {"env_prefix": "GITLAB_EMULATOR_"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.DATABASE_URL:
            self.DATABASE_URL = f"sqlite+aiosqlite:///{self.DATA_DIR}/gitlab_emulator.db"


settings = Settings()
