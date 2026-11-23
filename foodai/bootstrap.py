from foodai.agents.basket import BasketAgent
from foodai.application import Application
from foodai.services.address import AddressService
from foodai.services.catalog import Catalog
from foodai.services.llm import GroqLLM, MockLLM
from foodai.storage import Storage


def build(settings):
    catalog = Catalog(settings.catalog_path)
    llm = MockLLM() if settings.llm_mode == 'mock' else GroqLLM(
        settings.groq_api_key, settings.model_name, settings.groq_base_url)
    return Application(Storage(settings.database_path),
        AddressService(catalog, settings.zones_path, settings.geojson_path),
        {'basket': BasketAgent(catalog, llm, settings.max_tools)})
