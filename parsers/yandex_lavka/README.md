# Адаптер Яндекс Лавки

Минимальный адаптер сравнения корзин по интерфейсу из проектной инструкции:

```python
from yandex_lavka import YandexLavkaParser

parser = YandexLavkaParser()
address = "Москва, Тверская улица, 1"

products = parser.search("молоко 3,2%", address, limit=5)
cart = parser.calculate_cart(
    [{"product_id": products[0]["product_id"], "quantity": 1}],
    address,
)

slow_cart = parser.calculate_cart(
    [{"product_id": products[0]["product_id"], "quantity": 1}],
    address,
    depot_type="supermarket",
    delivery_option="personal",
)

# Оба режима считаются последовательно в одной гостевой корзине.
cart_options = parser.calculate_cart_options(
    [{"product_id": products[0]["product_id"], "quantity": 1}],
    address,
    depot_type="supermarket",
)
fast_cart = cart_options["quotes"]["on_demand"]
slow_cart = cart_options["quotes"].get("personal")
```

У Лавки две отдельные продуктовые витрины с разными складами и ассортиментом:

```python
products_by_storefront = parser.search_storefronts(
    "молоко 3,2%",
    address,
    limit=5,
)

nearby_lavka = products_by_storefront["regular"]
big_lavka = products_by_storefront["supermarket"]
```

`regular` соответствует «Лавке у дома», `supermarket` — «Большой Лавке».
Позиции из разных витрин нельзя смешивать в одной корзине: каждую витрину нужно
рассчитывать отдельно с тем же `depot_type`.

`delivery_option="on_demand"` выбирает быструю доставку, а `"personal"` —
вариант «Подождать». Парсер переключает режим через API и повторно читает
корзину, поэтому стоимость, ETA и минимальная сумма являются подтверждёнными.
`calculate_cart_options()` делает оба расчёта в одной гостевой сессии. Если
вариант «Подождать» для выбранной витрины недоступен, быстрая котировка остаётся
в `quotes`, а причина появляется в `errors["personal"]`.

Контрольную корзину из пяти товаров можно проверить сразу в обеих витринах:

```powershell
python scripts/check_yandex_lavka_basket.py `
  --address 'Москва, Рязанский проспект, 2к2' `
  --output output/yandex_lavka_control_basket.json
```

Скрипт сначала пытается выбрать одинаковые SKU, но итоговую доступность всегда
берёт из пересчитанной корзины. Товар может иметь `available=true` в поиске и
всё же быть отклонён складом при добавлении; такой товар будет указан в
`unavailable_items`, а состояние корзины станет `partial`.

Деньги во внешнем результате возвращаются целыми копейками. Каждый вызов создаёт
новую гостевую сессию и отдельную корзину. Авторизация, подтверждение и оплата
заказа не выполняются.

Можно передать уже разобранный адрес и координаты, чтобы не вызывать геокодер:

```python
location = {
    "city": "Москва",
    "street": "Тверская улица",
    "house": "1",
    "longitude": 37.6147157174475,
    "latitude": 55.75631527958286,
    "depot_type": "supermarket",
}
```

## Проверка

Юнит-тесты не требуют сторонних библиотек и не обращаются к сети:

```powershell
python -m unittest discover -s tests -v
```

Живой тест использует публичный тестовый адрес и включается явно:

```powershell
$env:LAVKA_LIVE_TEST = "1"
python -m unittest tests.test_live -v
```

Техническая карта запросов находится в
[`docs/yandex-lavka-research.md`](docs/yandex-lavka-research.md).

API Яндекс Лавки внутренний и может измениться. При `429` адаптер возвращает
`RATE_LIMITED`, при CAPTCHA — `ANTI_BOT_CHALLENGE`; обход защит не реализуется.

## Сопоставление H3-зон со складами

Перед массовым сбором каталогов адреса следует сгруппировать по складам. Скрипт
ниже выполняет только лёгкие запросы `service-info` для двух витрин, сохраняет
результат после каждой проверки и считает, сколько уникальных каталогов
останется после дедупликации:

```powershell
python scripts/map_yandex_lavka_hexagon_depots.py `
  --input C:\path\to\hexagons.csv
```

Для безопасной проверки первых пяти зон:

```powershell
python scripts/map_yandex_lavka_hexagon_depots.py `
  --input C:\path\to\hexagons.csv `
  --limit 5
```

Результаты сохраняются в `data/yandex_lavka_hexagons/hexagon_depots.csv` и
`data/yandex_lavka_hexagons/depot_summary.json`. Повторный запуск пропускает уже
проверенные пары `h3_index + storefront`. Между запросами всегда выдерживается
пауза не менее одной секунды; при `429`, CAPTCHA или отказе гостевой сессии сбор
останавливается с сохранённой контрольной точкой.

## Полный каталог по категориям

Новый сборщик обходит дерево `grocery` отдельно для «Лавки у дома» и
«Большой Лавки». Он сохраняет продуктовые категории, включая отсутствующие
товары, но пропускает аптеку, зоотовары, косметику и товары для дома.

Безопасная проверка одной категории и двух подробных карточек на витрину:

```powershell
python scripts/collect_yandex_lavka_categories.py `
  --stage all `
  --max-categories 1 `
  --max-details 2
```

Собрать все товарные карточки по категориям без тысяч запросов подробностей:

```powershell
python scripts/collect_yandex_lavka_categories.py --stage categories
```

Затем возобновляемо обогащать товары составом, КБЖУ, описанием и остальными
полями полной карточки:

```powershell
python scripts/collect_yandex_lavka_categories.py `
  --stage details `
  --resume `
  --max-details 100
```

После проверки ограничения можно убрать. Результат находится в
`data/yandex_lavka_catalog/catalog.sqlite3`, а неизменённые ответы API — в
`data/yandex_lavka_catalog/raw/<run_id>/*.jsonl`. Повторный запуск с `--resume`
пропускает уже завершённые категории и подробные карточки. Минимальная пауза
между запросами — одна секунда; при `429`, CAPTCHA или отказе гостевой сессии
сбор останавливается с сохранением контрольной точки.

## Первичный сбор по всей Москве

Городской сборщик читает готовое соответствие H3-зон складам, обходит каждый
уникальный склад один раз и хранит полную карточку один раз на `product_id`.
Цены и наличие сохраняются отдельно для каждого склада и запуска.

Контрольный запуск на трёх складах:

```powershell
python scripts/collect_yandex_lavka_city.py `
  --limit-depots 3 `
  --max-categories 2 `
  --max-details 10 `
  --workers 3
```

Первичный полный сбор:

```powershell
python scripts/collect_yandex_lavka_city.py `
  --stage all `
  --workers 4 `
  --request-interval 1.05
```

Продолжение после остановки:

```powershell
python scripts/collect_yandex_lavka_city.py --stage all --workers 4 --resume
```

У всех работников один ограничитель: старты запросов разнесены минимум на одну
секунду. Разбор ответов и запись результатов выполняются параллельно. При
`429`, `403`, CAPTCHA или требовании авторизации новые запросы прекращаются.

Основной результат находится в `data/yandex_lavka_city/city_catalog.sqlite3`,
сжатые исходные ответы — в `data/yandex_lavka_city/raw/<run_id>/`. Экспорт в
37 колонок выполняется после сбора:

```powershell
python scripts/export_yandex_lavka_city_csv.py `
  --output data/yandex_lavka_city/lavka_primary.csv.gz
```

Сжатый CSV раскрывается обычным gzip-архиватором. Полные описания в нём
повторяются для каждой H3-зоны, поэтому нормализованная SQLite-база занимает
существенно меньше места и остаётся основным хранилищем.
