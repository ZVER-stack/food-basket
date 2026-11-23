from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str
    groq_api_key: str
    model_name: str
    groq_base_url: str
    llm_mode: str
    catalog_path: str
    database_path: str
    zones_path: str
    geojson_path: str
    max_tools: int

    @classmethod
    def load(cls):
        load_dotenv()
        mode = os.getenv('LLM_MODE', 'mock')
        if mode not in ('mock', 'groq'):
            raise ValueError('LLM_MODE должен быть mock или groq')
        return cls(os.getenv('BOT_TOKEN', ''), os.getenv('GROQ_API_KEY', ''),
                   os.getenv('MODEL_NAME', 'openai/gpt-oss-120b'),
                   os.getenv('GROQ_BASE_URL', 'https://api.groq.com/openai/v1'), mode,
                   os.getenv('CATALOG_PATH', 'fixtures/catalog.json'),
                   os.getenv('DATABASE_PATH', 'data/users.sqlite'),
                   os.getenv('ZONES_PATH', ''), os.getenv('GEOJSON_PATH', ''),
                   int(os.getenv('AGENT_MAX_TOOLS', '8')))
