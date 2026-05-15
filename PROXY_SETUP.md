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
PROXY_FAST_1_REQUEST_LIMIT_PER_MINUTE=100
PROXY_FAST_2_REQUEST_LIMIT_PER_MINUTE=100
PROXY_SLOW_REQUEST_LIMIT_PER_MINUTE=100

REQUEST_BURST_LIMIT=5
REQUEST_MIN_DELAY_MS=300
REQUEST_MAX_DELAY_MS=5000
REQUEST_JITTER_MS=200
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
4. Проверьте IP, лимиты, позиции, dry-run и safe mode.

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
