from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    app_env: str = Field("development")
    app_host: str = Field("0.0.0.0")
    app_port: int = Field(8000)

    proxmox_url: str = Field(...)
    proxmox_realm: str = Field("pve")
    proxmox_user: str = Field("ai-stack")
    proxmox_token_id: str = Field("assistant")
    proxmox_token_secret: str = Field(...)
    proxmox_verify_ssl: bool = Field(False)

    qdrant_url: str = Field(...)
    qdrant_api_key: str = Field("")
    qdrant_current_collection_name: str = Field(
        "infrastructure_current",
    )
    qdrant_history_collection_name: str = Field(
        "infrastructure_history",
    )

    ollama_url: str = Field(...)
    ollama_model: str = Field("llama3.1:8b")
    loki_url: str = Field(...)
    prometheus_url: str = Field(...)
    approval_db_path: str = Field("data/approvals.sqlite3")

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
