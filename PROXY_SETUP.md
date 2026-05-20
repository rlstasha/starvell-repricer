# Proxy Profiles For Starvell Repricer

Проект может работать с одного сервера, но отправлять запросы Starvell через разные proxy/IP.

Схема:

```text
Наш сервер / приложение
        |
        +-- proxy_fast_1 -> Starvell
        +-- proxy_fast_2 -> Starvell
        +-- proxy_slow   -> Starvell
```

Если proxy не заданы, проект работает напрямую как раньше.

## Что такое proxy

Proxy - это промежуточный сервер. Приложение отправляет запрос в proxy, а proxy уже идет на Starvell со своего IP.

VPS - это полноценный удаленный сервер. Proxy проще: вы не запускаете там Docker и проект, а только используете его IP для запросов.

## Где брать proxy

Подойдут HTTP, HTTPS или SOCKS5 proxy от провайдера, которому вы доверяете. Важно, чтобы proxy был стабильным, не публичным бесплатным и имел понятный лимит запросов.

## Формат proxy URL

Поддерживаются:

```text
http://login:password@ip:port
https://login:password@ip:port
socks5://login:password@ip:port
```

Пример:

```env
PROXY_FAST_1_URL=http://login:password@1.1.1.1:8000
PROXY_FAST_2_URL=http://login:password@2.2.2.2:8000
PROXY_SLOW_URL=http://login:password@3.3.3.3:8000
```

Логин и пароль proxy не выводятся в логах. В логах будет маска:

```text
http://***:***@1.1.1.1:8000
```

## Настройка `.env`

Добавьте или обновите:

```env
PROXY_MODE=enabled

PROXY_FAST_1_URL=http://login:password@1.1.1.1:8000
PROXY_FAST_2_URL=http://login:password@2.2.2.2:8000
PROXY_SLOW_URL=http://login:password@3.3.3.3:8000

PROXY_FAST_1_POSITIONS=500,800,1000
PROXY_FAST_2_POSITIONS=400,1200,1700,2000
PROXY_SLOW_POSITIONS=40,80,200,2100,2500,3600,4500,10000,22500

GLOBAL_REQUEST_LIMIT_PER_MINUTE=300
TOKEN_LIMIT_MODE=true
ACCOUNT_EFFECTIVE_LIMIT_PER_MINUTE=300
ACCOUNT_MIN_LIMIT_PER_MINUTE=60
ACCOUNT_LIMIT_DECREASE_STEP_PER_MINUTE=30
ACCOUNT_LIMIT_RAMP_STEP_PER_MINUTE=20
ACCOUNT_LIMIT_RAMP_IDLE_SECONDS=180
PROXY_FAST_1_REQUEST_LIMIT_PER_MINUTE=100
PROXY_FAST_2_REQUEST_LIMIT_PER_MINUTE=100
PROXY_SLOW_REQUEST_LIMIT_PER_MINUTE=100

REQUEST_BURST_LIMIT=5
REQUEST_MIN_DELAY_MS=100
REQUEST_MAX_DELAY_MS=5000
REQUEST_JITTER_MS=50
REQUEST_BACKOFF_FACTOR=2

SAFE_MODE_ENABLED=true
SAFE_MODE_ON_429=true
SAFE_MODE_ON_403=true
SAFE_MODE_COOLDOWN_SECONDS=300
```

Если конкретный proxy пустой, эта группа идет напрямую:

```env
PROXY_FAST_1_URL=
```

Если все proxy URL пустые, весь проект идет напрямую.

## Распределение лотов

Fast 1:

```text
500
800
1000
```

Частота при лимите 100/мин: около 1.8 сек на позицию.

Fast 2:

```text
400
1200
1700
2000
```

Частота при лимите 100/мин: около 2.4 сек на позицию.

Slow:

```text
40
80
200
2100
2500
3600
4500
10000
22500
```

Частота при лимите 100/мин: около 5.4 сек на позицию.

## Проверить proxy

```bash
python -m app.check_proxies
```

В Docker:

```bash
docker compose run --rm worker python -m app.check_proxies
```

Команда покажет внешний IP, позиции, лимит, частоту и статус. Пароли proxy не выводятся.

## Проверить лимиты

```bash
python -m app.check_proxy_limits
```

В Docker:

```bash
docker compose run --rm worker python -m app.check_proxy_limits
```

Если сумма proxy-лимитов больше `GLOBAL_REQUEST_LIMIT_PER_MINUTE`, приложение не стартует и команда покажет ошибку.

`TOKEN_LIMIT_MODE=true` добавляет общий лимит аккаунта/сессии поверх трех proxy.
Он не фиксируется на `60/мин`: стартует с `300/мин`, при `429` снижается шагом
`30/мин` до безопасного минимума `60/мин`, а после 10 минут без новых `429`
растет обратно на `10/мин`. Worker также учитывает `Retry-After` и временно
замедляет только тот proxy profile, который получил ограничение.

Интервалы проверок живые: Fast 1 обычно `1.5-2.2 сек`, Fast 2 `2.0-3.0 сек`,
Slow `4.5-6.5 сек`. Если цена конкурента по позиции часто меняется, эта
позиция проверяется чаще; если изменений нет, интервал постепенно растет.

## Как отключить proxy

Вариант 1:

```env
PROXY_MODE=disabled
```

Вариант 2:

```env
PROXY_FAST_1_URL=
PROXY_FAST_2_URL=
PROXY_SLOW_URL=
```

После этого запросы идут напрямую с IP сервера.

## Как поменять распределение лотов

Измените:

```env
PROXY_FAST_1_POSITIONS=500,800,1000
PROXY_FAST_2_POSITIONS=400,1200,1700,2000
PROXY_SLOW_POSITIONS=40,80,200,2100,2500,3600,4500,10000,22500
```

Одна позиция не должна быть в двух списках одновременно.

## Как проверить Telegram

1. Откройте бота.
2. Нажмите `/start`.
3. Откройте `📊 Прокси и лимиты`.
4. Проверьте IP, лимиты, позиции, режим изменения цен и safe mode.

## Реальное изменение цен

Прокси не включают реальные изменения цен автоматически. Для записи цены должны
быть одновременно настроены:

```env
DRY_RUN=false
ENABLE_REAL_PRICE_WRITES=true
MARKET_UPDATE_LOT_PRICE_URL=https://starvell.com/api/offers/{lot_id}/partial-update
MARKET_UPDATE_LOT_PRICE_METHOD=POST
MARKET_UPDATE_PRICE_PAYLOAD_STYLE=partial_update
MARKET_UPDATE_PRICE_CONTENT_TYPE=json
```

Безопасным GET frontend-кода Starvell найден endpoint страницы списка
предложений. Frontend получает мои предложения через `POST /api/offers/list-my`,
а сохраняет измененные строки через `partialUpdate`:

```env
MARKET_UPDATE_LOT_PRICE_URL=https://starvell.com/api/offers/{lot_id}/partial-update
MARKET_UPDATE_LOT_PRICE_METHOD=POST
MARKET_UPDATE_PRICE_PAYLOAD_STYLE=partial_update
MARKET_UPDATE_PRICE_CONTENT_TYPE=json
```

Payload:

```json
{
  "availability": 927,
  "price": "123",
  "minOrderCurrencyAmount": null,
  "isActive": true
}
```

Диагностика записи цены без `--confirm` ничего не отправляет:

```bash
python -m app.test_price_update --lot-id 2000 --price 123 --debug
```

Чтобы получить status/body/headers ответа Starvell без секретов и перебрать
известные payload-варианты, добавьте явное подтверждение:

```bash
python -m app.test_price_update --lot-id 2000 --price 123 --debug --confirm
```

При `--debug --confirm` команда показывает URL, method, payload, response body,
response headers и content-type без cookie/session/csrf/token/proxy password.
Она перебирает payload-варианты и JSON/form content-type. Если endpoint
неизвестен, откройте Starvell вручную, измените цену своего лота, посмотрите
DevTools -> Network и найдите POST/PATCH/PUT запрос сохранения цены.

Проверка:

```bash
python -m app.check_price_write_config
python -m app.test_price_update --lot-id 2000 --price 123
```

IP появляется после запуска worker или после команды `python -m app.check_proxies`.

## Безопасность

Не коммитьте `.env`.

В логах запрещено выводить:

```text
proxy login
proxy password
Telegram token
Starvell cookie/session/csrf
```

Проект показывает только маску proxy URL.
