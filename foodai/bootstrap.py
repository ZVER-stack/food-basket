from foodai.agents.basket import BasketAgent
from foodai.application import Application
from foodai.services.address import AddressService
from foodai.services.catalog import Catalog
from foodai.services.llm import GroqLLM, MockLLM
from foodai.storage import Storage
from foodai.services.geocoding import PhotonGeocoder
from pathlib import Path
import os


def build(settings):
    catalog = Catalog(settings.catalog_path)
    llm = MockLLM() if settings.llm_mode == 'mock' else GroqLLM(
        settings.groq_api_key, settings.model_name, settings.groq_base_url,
        os.getenv('LLM_USAGE_PATH', str(Path(settings.database_path).with_name('llm_usage.jsonl'))))
    return Application(Storage(settings.database_path),
        AddressService(catalog, settings.zones_path, settings.geojson_path,
                       PhotonGeocoder(Path(settings.database_path).with_name('geocoding.sqlite'),
                                      os.getenv('GEOCODER_URL', 'https://photon.komoot.io/api/'))),
        {'basket': BasketAgent(catalog, llm, settings.max_tools)})
