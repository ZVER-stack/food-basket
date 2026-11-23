from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Literal


def number(value) -> Decimal:
    try:
        result = Decimal(str(value).replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Некорректное число') from exc
    if not result.is_finite():
        raise ValueError('Число должно быть конечным')
    return result


def jsonable(value):
    return json.loads(json.dumps(value, default=lambda v: str(v) if isinstance(v, Decimal) else asdict(v)))


@dataclass(frozen=True)
class Product:
    sku: str
    service: str
    storefront_id: str
    zone_id: str
    name: str
    package_qty: Decimal
    package_unit: str
    price_rub: Decimal
    available: bool | None
    captured_at: str
    brand: str = ''
    category: str = ''
    product_url: str = ''
    nutrition_per: str = 'unknown'
    nutrition_source: str = 'unknown'
    kcal: str | None = None
    protein: str | None = None
    fat: str | None = None
    carbs: str | None = None


@dataclass
class AgentResult:
    text: str
    status: Literal['ok', 'partial', 'no_data', 'need_clarification', 'error']
    structured: dict[str, Any] = field(default_factory=dict)
    buttons: list[str] = field(default_factory=lambda: ['Главное меню', 'Изменить адрес'])

    def to_dict(self):
        return jsonable(asdict(self))
