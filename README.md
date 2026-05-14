# Starvell / Statvell Roblox Repricer

Репрайсер для раздела `Roblox -> Донат робуксов -> моментально`.

Проект сделан в dry-run-first режиме: по умолчанию он считает новую цену и пишет в логи/БД, что бы изменил, но не отправляет изменение цены на сайт.

## Что уже реализовано

- Python 3.12, aiogram 3, SQLAlchemy 2, PostgreSQL, Redis, Docker Compose.
- Модели БД для позиций, настроек, состояния, снимков конкурентов и логов обновлений.
- Seed позиций: `40, 80, 200, 400, 500, 800, 1000, 1200, 1700, 2000, 2100, 2500, 3600, 4500, 10000, 22500`.
- Seed ID лотов для всех известных позиций. Для `40` робуксов ID пока не указан.
- High priority для `400, 500, 800, 1000, 1200, 1700, 2000`.
- Priority queue: `70%` лимита на high-позиции и `30%` на normal-позиции.
- Rate limiter: не больше `100` запросов в минуту через Redis.
- Фильтр конкурентов: рейтинг, отсутствие рейтинга, свой продавец, неактивный продавец.
- Стратегия `undercut_by_1`: цена конкурента минус `step`, с ограничениями `min_price` и `max_price`.
- Fallback: `keep_current` или `set_max_price`.
- Telegram-бот с owner-only доступом, inline-меню, карточками позиций, статусом, логами и переключателем dry-run.
- Тесты pytest для ключевых сценариев.

## Важное про API сайта

Реальный API Starvell / Statvell не зашит по проекту. Вся работа с сайтом находится только в:

```text
app/market/client.py
```

Класс `StarvellClient` содержит методы-заглушки:

```python
get_market_offers(position_amount: int, lot_id: str | None)
get_my_lot(position_amount: int, lot_id: str | None)
update_my_lot_price(position_amount: int, lot_id: str | None, new_price: Decimal)
get_account_info()
```

Когда будут точные endpoint-ы сайта, нужно заменить TODO внутри этого класса. Остальной проект менять не нужно.

## Настройка

Скопировать пример env:

```bash
cp .env.example .env
```

Заполнить:

```env
TELEGRAM_BOT_TOKEN=...
OWNER_TELEGRAM_IDS=123456789,987654321
OWN_SELLER_ID=...
OWN_SELLER_USERNAME=...
MARKET_SESSION_COOKIE=...
```

Секреты не коммитить. Файл `.env` находится в `.gitignore`.

## Dry-run

Dry-run включен по умолчанию:

```env
DRY_RUN=true
```

Первое значение берется из `.env` и сохраняется в таблицу `app_settings`. Дальше режим можно переключать кнопкой в Telegram-боте без перезапуска контейнеров.

В этом режиме worker:

- получает/считает данные;
- пишет лог `repricer_dry_run_price_update`;
- сохраняет расчет в БД;
- не вызывает реальное обновление цены на сайте.

Чтобы разрешить реальные изменения после реализации API:

```env
DRY_RUN=false
```

## Запуск через Docker

```bash
docker compose up --build
```

Отдельно миграции:

```bash
docker compose run --rm migrate
```

Seed позиций:

```bash
docker compose run --rm seed
```

Worker:

```bash
docker compose up worker
```

Telegram-бот:

```bash
docker compose up bot
```

## Локальный запуск

Нужен Python 3.12.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
python -m scripts.seed_positions
python -m app.worker_main
```

Бот:

```bash
python -m app.bot_main
```

## Telegram-бот

Запуск через Docker:

```bash
docker compose up bot
```

Главное меню открывается через `/start`. Дальше управление идет через inline-кнопки:

- `📦 Позиции`
- `⚙️ Общие настройки`
- `📊 Статус`
- `🧪 Dry-run включить/выключить`
- `📝 Логи последних действий`

Доступ есть у всех пользователей из `OWNER_TELEGRAM_IDS`:

```env
OWNER_TELEGRAM_IDS=123456789,987654321
```

Если `OWNER_TELEGRAM_IDS` пустой, работает старое поле:

```env
OWNER_TELEGRAM_ID=123456789
```

Если пишет другой пользователь, бот отвечает: `Нет доступа`.

### Как узнать Telegram ID

Самый простой способ: написать боту `@userinfobot` в Telegram и взять значение `Id`.

Если владелец уже прописан в `.env`, можно написать этому боту команду:

```text
/id
```

### Как поменять владельца

1. Остановить контейнер бота.
2. Изменить `OWNER_TELEGRAM_IDS` в `.env`.
3. Запустить бота снова:

```bash
docker compose up -d bot
```

### Как добавить владельца

Добавьте Telegram ID через запятую:

```env
OWNER_TELEGRAM_IDS=123456789,987654321,555555555
```

Пробелы вокруг запятых допустимы. Если в списке есть нечисловой ID, бот не стартует.

### Как поменять ID лота

1. Откройте `/start`.
2. Нажмите `📦 Позиции`.
3. Выберите позицию.
4. Нажмите `🔗 ID лота`.
5. Отправьте новый числовой ID.

Чтобы очистить ID, отправьте `-`. Для позиции без ID бот показывает:

```text
ID лота: не указан
Не найден ID лота. Репрайс невозможен.
```

### Приоритеты

В карточке позиции кнопка `⚡ Приоритет` переключает `высокий` и `обычный`.

По умолчанию общий лимит делится так:

```env
REQUEST_LIMIT_PER_MINUTE=100
HIGH_PRIORITY_PERCENT=70
NORMAL_PRIORITY_PERCENT=30
```

В статусе бот показывает, сколько запросов в минуту идет на high и normal, сколько
включенных позиций каждого типа и примерную частоту проверки. Расчет исходит из того,
что одна проверка позиции занимает примерно два запроса к Starvell.

### Как проверить защиту

1. Запустить бота с вашим ID в `OWNER_TELEGRAM_IDS`.
2. Написать `/start` с вашего Telegram-аккаунта: должно открыться меню.
3. Написать `/start` с другого аккаунта: бот должен ответить `Нет доступа`.

## Тесты

```bash
pytest -q
```

Или без локального Python:

```bash
docker compose run --rm worker pytest -q
```

## Основные файлы

```text
app/market/client.py              # единственный слой Starvell/Statvell API
app/repricer/engine.py            # обработка одной позиции
app/repricer/scheduler.py         # бесконечный worker loop
app/repricer/rate_limiter.py      # Redis/in-memory rate limiter
app/repricer/price_strategy.py    # расчет целевой цены
app/repricer/competitor_filter.py # фильтр конкурентов
app/db/models.py                  # SQLAlchemy модели
app/bot_main.py                   # запуск Telegram-бота
app/bot/handlers/                 # обработчики Telegram-меню
tests/                            # pytest
```

## Передача другому человеку

1. Передать проект без `.env`.
2. Новый владелец создает `.env` из `.env.example`.
3. Указывает свой `OWNER_TELEGRAM_IDS`.
4. Указывает новый `TELEGRAM_BOT_TOKEN`.
5. Запускает `docker compose up --build`.
