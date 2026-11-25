from pathlib import Path
import os
from typing import Literal
from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'sqlite:///./data/techstock.db'
    llm_provider: Literal['openai', 'openrouter'] = 'openai'
    openrouter_api_key: str = ''
    openai_api_key: str = ''
    openai_model: str = 'gpt-4.1-mini'
    langsmith_tracing: bool = False
    langsmith_project: str = 'techstock-ai'
    langsmith_api_key: str = ''
    email_mode: str = 'sandbox'
    smtp_host: str = 'localhost'
    smtp_port: int = 1025
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_from: str = 'orders@techstock.test'
    smtp_allowed_recipients: str = ''
    max_agent_steps: int = Field(default=12, ge=1, le=12)
    max_tool_retries: int = Field(default=2, ge=0, le=2)
    scraping_request_delay: float = Field(default=2, ge=2)
    scraping_validated_routes: bool = True
    scraping_max_pages: int = Field(default=20, ge=1, le=100)
    scraping_render_js: bool = False
    spacenet_gaming_listing_limit: int = Field(default=96, ge=1, le=200)
    shopping_fresh_hours: int = Field(default=6, ge=1, le=720)
    shopping_live_discovery: bool = True
    shopping_local_planner: bool = True
    shopping_model_timeout_seconds: float = Field(default=25, gt=0, le=60)
    catalog_auto_refresh: bool = True
    catalog_refresh_hours: int = Field(default=24, ge=1, le=168)
    catalog_max_age_hours: int = Field(default=48, ge=1, le=720)
    offer_max_age_hours: int = 48
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

settings = Settings()
