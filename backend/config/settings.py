from functools import lru_cache

from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    app_env: str = Field("development", env="APP_ENV")
    app_host: str = Field("0.0.0.0", env="APP_HOST")
    app_port: int = Field(8000, env="APP_PORT")

    proxmox_url: str = Field(..., env="PROXMOX_URL")
    proxmox_realm: str = Field("pve", env="PROXMOX_REALM")
    proxmox_user: str = Field("ai-stack", env="PROXMOX_USER")
    proxmox_token_id: str = Field("assistant", env="PROXMOX_TOKEN_ID")
    proxmox_token_secret: str = Field(..., env="PROXMOX_TOKEN_SECRET")
    proxmox_verify_ssl: bool = Field(False, env="PROXMOX_VERIFY_SSL")

    qdrant_url: str = Field(..., env="QDRANT_URL")
    qdrant_api_key: str = Field("", env="QDRANT_API_KEY")
    qdrant_current_collection_name: str = Field(
        "infrastructure_current",
        env="QDRANT_CURRENT_COLLECTION_NAME",
    )
    qdrant_history_collection_name: str = Field(
        "infrastructure_history",
        env="QDRANT_HISTORY_COLLECTION_NAME",
    )

    ollama_url: str = Field(..., env="OLLAMA_URL")
    ollama_model: str = Field("llama3.1:8b", env="OLLAMA_MODEL")
    loki_url: str = Field(..., env="LOKI_URL")
    prometheus_url: str = Field(..., env="PROMETHEUS_URL")
    approval_db_path: str = Field("data/approvals.sqlite3", env="APPROVAL_DB_PATH")

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def proxmox_token_name(self) -> str:
        if "@" in self.proxmox_user:
            user_identity = self.proxmox_user
        else:
            user_identity = f"{self.proxmox_user}@{self.proxmox_realm}"
        return f"{user_identity}!{self.proxmox_token_id}"

    @property
    def proxmox_auth_header(self) -> str:
        return f"PVEAPIToken={self.proxmox_token_name}={self.proxmox_token_secret}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
