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
- Rate limiter: отдельный лимит worker через Redis, по умолчанию `100` запросов в минуту на каждый worker.
- Фильтр конкурентов: рейтинг, отсутствие рейтинга, свой продавец, неактивный продавец.
- Стратегия `undercut_by_1`: цена конкурента минус `step`, с ограничениями `min_price` и `max_price`.
- Fallback: `keep_current` или `set_max_price`.
- Telegram-бот с owner-only доступом, inline-меню, карточками позиций, статусом, логами и переключателем dry-run.
- Proxy profiles: `proxy_fast_1`, `proxy_fast_2`, `proxy_slow` с отдельными лимитами, Redis-lock и heartbeat.
- Тесты pytest для ключевых сценариев.

## Важное про API сайта

Реальный API Starvell / Statvell не зашит по проекту. Вся работа с сайтом находится только в:

```text
app/market/client.py
```

Класс `StarvellClient` содержит безопасные GET-методы чтения данных и отдельный
защитный метод записи цены, который сейчас намеренно блокирует любые изменения:

```python
check_connection()
get_market_offers(position_amount: int, lot_id: str | None)
get_my_lot(position_amount: int, lot_id: str | None)
get_my_lots()
get_account_info()
update_my_lot_price(position_amount: int, lot_id: str | None, new_price: Decimal)
```

Чтение своих данных работает через GET, а публичный рынок читается через
read-only endpoint списка офферов:

- `MARKET_ACCOUNT_INFO_URL` и `MARKET_MY_LOTS_URL` обычно указывают на профиль продавца;
- `MARKET_OFFERS_API_URL` по умолчанию `/api/offers/list-by-category` и используется
  для списка публичных предложений-конкурентов по `subCategoryId`;
- `MARKET_OFFERS_URL` по умолчанию `/roblox/packages` оставлен как fallback HTML-страницы;
- текущая цена своего лота читается со страницы `/offers/{lot_id}`.

Для безопасной проверки подключения можно указать найденные через DevTools GET URL:

```env
MARKET_ACCOUNT_INFO_URL=
MARKET_MY_LOTS_URL=
MARKET_OFFERS_URL=/roblox/packages
MARKET_OFFERS_API_URL=/api/offers/list-by-category
MARKET_OFFERS_LIMIT=100
```

Реальное изменение цены на сайте пока не включено: `update_my_lot_price()`
останавливается до любых POST/PATCH/PUT-запросов.

Текущий источник конкурентов:

```env
MARKET_OFFERS_URL=/roblox/packages
MARKET_OFFERS_API_URL=/api/offers/list-by-category
```

HTML-страница `/roblox/packages` отдает только серверный срез публичных офферов.
Для полного списка по номиналам репрайсер использует read-only запрос
`/api/offers/list-by-category` с `categoryId=40` и нужным `subCategoryId`.
Цены этот запрос не меняет.

Диагностика конкурентов:

```bash
python -m app.diagnose_competitors
```

Команда показывает по каждой позиции: `lot_id`, URL, количество офферов до и
после фильтра, причины отбраковки и лучшего найденного конкурента. Cookie,
session и token не выводятся.

## Проверка подключения Starvell

Проверка выполняет только безопасные GET-запросы. Она не меняет цены и не вызывает
POST/PATCH/PUT:

```bash
python -m app.check_starvell_connection
```

Команда:

- проверяет авторизацию, если настроен `MARKET_ACCOUNT_INFO_URL`;
- показывает `seller_id` и `username`, если они найдены в ответе;
- показывает, видит ли мои лоты, если настроен `MARKET_MY_LOTS_URL`;
- не выводит `MARKET_SESSION_COOKIE`, токены или CSRF;
- объясняет ошибки `401`, `403`, `429`, `500` простым текстом.

Если URL еще неизвестны, откройте Starvell в браузере, DevTools -> Network, и найдите:

- GET-запрос кабинета/профиля, который возвращает текущего пользователя;
- GET-запрос кабинета, который возвращает мои активные лоты.

Эти URL нужно добавить в `.env` как `MARKET_ACCOUNT_INFO_URL` и `MARKET_MY_LOTS_URL`.

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
MARKET_ACCOUNT_INFO_URL=...
MARKET_MY_LOTS_URL=...
MARKET_OFFERS_URL=/roblox/packages
MARKET_OFFERS_API_URL=/api/offers/list-by-category
MARKET_OFFERS_LIMIT=100
```

Секреты не коммитить. Файл `.env` находится в `.gitignore`.

## Proxy profiles

Подробная инструкция: [PROXY_SETUP.md](/Users/user/starvell-repricer/PROXY_SETUP.md).

Один сервер может отправлять запросы Starvell через три proxy/IP:

```env
PROXY_MODE=enabled
PROXY_FAST_1_URL=http://login:password@1.1.1.1:8000
PROXY_FAST_2_URL=http://login:password@2.2.2.2:8000
PROXY_SLOW_URL=http://login:password@3.3.3.3:8000
```

Если `PROXY_MODE=disabled` или все `PROXY_*_URL` пустые, проект работает напрямую.

Распределение по умолчанию:

```env
PROXY_FAST_1_POSITIONS=500,800,1000
PROXY_FAST_2_POSITIONS=400,1200,1700,2000
PROXY_SLOW_POSITIONS=40,80,200,2100,2500,3600,4500,10000,22500
```

Лимиты:

```env
GLOBAL_REQUEST_LIMIT_PER_MINUTE=300
PROXY_FAST_1_REQUEST_LIMIT_PER_MINUTE=100
PROXY_FAST_2_REQUEST_LIMIT_PER_MINUTE=100
PROXY_SLOW_REQUEST_LIMIT_PER_MINUTE=100
```

Проверки:

```bash
python -m app.check_proxy_limits
python -m app.check_proxies
```

Proxy login/password не пишутся в логи: показывается только маска вида
`http://***:***@1.1.1.1:8000`.

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

Один сервер, все сервисы локально:

```bash
docker compose up -d --build
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

Ручной split-worker режим оставлен для отладки:

```bash
docker compose --profile split-workers up worker-fast-1 worker-fast-2 worker-slow
```

Telegram-бот:

```bash
docker compose up bot
```

## Multi-server режим

Сейчас основной вариант распределения нагрузки - proxy profiles. Старый вариант с
несколькими VPS оставлен как дополнительный сценарий:
[MULTI_SERVER_DEPLOY.md](/Users/user/starvell-repricer/MULTI_SERVER_DEPLOY.md).

Текущая схема:

```text
MAIN SERVER: Telegram bot, PostgreSQL, Redis
VPS #1: worker-fast-1 -> 500, 800, 1000
VPS #2: worker-fast-2 -> 400, 1200, 1700, 2000
VPS #3: worker-slow -> 40, 80, 200, 2100, 2500, 3600, 4500, 10000, 22500
```

Основные env-переменные:

```env
GLOBAL_REQUEST_LIMIT_PER_MINUTE=300
WORKER_FAST_1_REQUEST_LIMIT_PER_MINUTE=100
WORKER_FAST_2_REQUEST_LIMIT_PER_MINUTE=100
WORKER_SLOW_REQUEST_LIMIT_PER_MINUTE=100
WORKER_FAST_1_POSITIONS=500,800,1000
WORKER_FAST_2_POSITIONS=400,1200,1700,2000
WORKER_SLOW_POSITIONS=40,80,200,2100,2500,3600,4500,10000,22500
POSITION_LOCK_TTL_SECONDS=30
```

Main server:

```bash
cp .env.main.example .env
docker compose -f docker-compose.yml -f docker-compose.main.yml up -d --build
```

Worker fast 1:

```bash
cp .env.worker-fast-1.example .env
docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build worker-fast-1
```

Worker fast 2:

```bash
cp .env.worker-fast-2.example .env
docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build worker-fast-2
```

Worker slow:

```bash
cp .env.worker-slow.example .env
docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build worker-slow
```

Проверки:

```bash
python -m app.check_proxy_limits
python -m app.check_worker_ip
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
- `📊 Прокси и лимиты`
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
включенных позиций каждого типа и примерную частоту проверки.

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
app/repricer/worker_groups.py     # группы fast_1, fast_2, slow
app/repricer/locks.py             # Redis lock/lease позиций
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
