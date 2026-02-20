"""
Configuration management for the load balancer.
"""
import json
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class SubscriptionConfig(BaseModel):
    """Configuration for a single Anthropic subscription."""
    name: str
    api_key: str
    max_concurrent: int = Field(default=5, ge=1, le=50)
    priority: int = Field(default=1, ge=1)
    enabled: bool = True


class ServerConfig(BaseModel):
    """Server configuration."""
    host: str = "0.0.0.0"
    port: int = 8080


class ExternalAccessConfig(BaseModel):
    """Configuration for external (tunnel) access."""
    enabled: bool = False
    api_token: str = ""
    allowed_clients: list[str] = []


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""
    cooldown_seconds: int = Field(default=60, ge=1)
    burst_limit: int = Field(default=10, ge=1)


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = "INFO"
    format: str = "json"  # "json" or "text"


class Config(BaseModel):
    """Main configuration."""
    subscriptions: list[SubscriptionConfig]
    server: ServerConfig = Field(default_factory=ServerConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    external: ExternalAccessConfig = Field(default_factory=ExternalAccessConfig)


def load_config(path: Optional[str] = None) -> Config:
    """
    Load configuration from file.
    
    Args:
        path: Path to config file. Defaults to config.json in project root.
        
    Returns:
        Parsed Config object.
        
    Raises:
        FileNotFoundError: If config file doesn't exist.
        ValidationError: If config is invalid.
    """
    if path is None:
        # Look for config.json in project root
        path = Path(__file__).parent.parent / "config.json"
    else:
        path = Path(path)
    
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.json to config.json and configure your subscriptions."
        )
    
    with open(path) as f:
        data = json.load(f)
    
    return Config(**data)


# Global config instance (loaded on import if available, otherwise None)
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> Config:
    """Reload configuration from disk."""
    global _config
    _config = load_config()
    return _config
