from pathlib import Path
import pytest
from foodai.agents.basket import BasketAgent
from foodai.application import Application
from foodai.services.address import AddressService
from foodai.services.catalog import Catalog
from foodai.services.llm import MockLLM
from foodai.storage import Storage


@pytest.fixture
def catalog():
    return Catalog(Path(__file__).parents[1]/'fixtures/catalog.json')


@pytest.fixture
def app(tmp_path, catalog):
    return Application(Storage(tmp_path/'users.sqlite'), AddressService(catalog),
                       {'basket': BasketAgent(catalog, MockLLM())})
