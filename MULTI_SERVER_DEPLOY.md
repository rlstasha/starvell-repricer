# Multi-Server Deploy: 2 быстрых VPS и 1 медленный

Этот документ описывает простой production-запуск Starvell Repricer на нескольких VPS.

## Что такое основные слова

VPS - это удаленный сервер, который вы арендуете у хостинга.

IP - это публичный адрес VPS в интернете. У каждого worker нужен свой IP.

Docker - программа, которая запускает проект в контейнерах.

Docker Compose - файл с описанием контейнеров: bot, postgres, redis, worker.

`.env` - локальный файл с настройками и секретами. Его нельзя коммитить в GitHub.

PostgreSQL - база данных проекта. Там хранятся позиции, ID лотов, настройки, логи и heartbeat worker.

Redis - быстрый сервис для rate limit и lock/lease, чтобы два worker не взяли одну позицию одновременно.

Worker - процесс репрайсера. Он берет назначенные позиции, читает Starvell и считает цену.

Lock/lease - временная блокировка позиции. Если worker упал, lock сам истекает через TTL.

## Итоговая схема

```text
MAIN SERVER
├── Telegram bot
├── PostgreSQL
└── Redis

VPS #1 / IP #1
└── worker-fast-1

VPS #2 / IP #2
└── worker-fast-2

VPS #3 / IP #3
└── worker-slow
```

Прокси не используются. Каждый worker идет в Starvell со своего VPS/IP.

## Распределение позиций

```text
worker-fast-1
Позиции: 500, 800, 1000
Лимит: 100/мин
Примерная частота: около 1.8 сек
```

```text
worker-fast-2
Позиции: 400, 1200, 1700, 2000
Лимит: 100/мин
Примерная частота: около 2.4 сек
```

```text
worker-slow
Позиции: 40, 80, 200, 2100, 2500, 3600, 4500, 10000, 22500
Лимит: 100/мин
Примерная частота: около 5.4 сек
```

Fast 1 быстрее, потому что у него меньше позиций при том же лимите.

## Установка Docker на Ubuntu VPS

Выполнить на каждом VPS:

```bash
apt update
apt install -y docker.io docker-compose-plugin git
systemctl enable docker
systemctl start docker
docker --version
docker compose version
```

## Как загрузить проект

На каждом сервере:

```bash
git clone https://github.com/USERNAME/starvell-repricer.git
cd starvell-repricer
```

Замените `USERNAME` на владельца репозитория.

## Что брать из текущего `.env`

На main server нужны:

```text
TELEGRAM_BOT_TOKEN
OWNER_TELEGRAM_IDS
MARKET_SESSION_COOKIE
MARKET_CSRF_TOKEN
OWN_SELLER_ID
OWN_SELLER_USERNAME
MARKET_ACCOUNT_INFO_URL
MARKET_MY_LOTS_URL
MARKET_OFFERS_URL
MARKET_OFFERS_API_URL
MARKET_OFFERS_LIMIT
```

На worker VPS нужны Starvell-настройки:

```text
MARKET_SESSION_COOKIE
MARKET_CSRF_TOKEN
OWN_SELLER_ID
OWN_SELLER_USERNAME
MARKET_ACCOUNT_INFO_URL
MARKET_MY_LOTS_URL
MARKET_OFFERS_URL
MARKET_OFFERS_API_URL
MARKET_OFFERS_LIMIT
```

Telegram token на worker VPS не нужен.

## Main server

Создать `.env`:

```bash
cp .env.main.example .env
nano .env
```

Заполнить Telegram, Starvell, PostgreSQL и Redis. Пароли должны быть сильными.

Запуск main server:

```bash
docker compose -f docker-compose.yml -f docker-compose.main.yml up -d --build
```

На main server запускаются bot, PostgreSQL, Redis, migrate и seed. Worker на main server не запускаются.

## Firewall main server

PostgreSQL и Redis нельзя открывать всему интернету.

Разрешите доступ только IP worker VPS:

```bash
ufw allow OpenSSH
ufw allow from WORKER_1_IP to any port 5432
ufw allow from WORKER_2_IP to any port 5432
ufw allow from WORKER_3_IP to any port 5432
ufw allow from WORKER_1_IP to any port 6379
ufw allow from WORKER_2_IP to any port 6379
ufw allow from WORKER_3_IP to any port 6379
ufw enable
```

Не делайте `ufw allow 5432` и `ufw allow 6379` без ограничения IP.

## Worker fast 1

На VPS #1:

```bash
cp .env.worker-fast-1.example .env
nano .env
docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build worker-fast-1
```

В `.env` заменить:

```text
MAIN_SERVER_IP
MAIN_POSTGRES_PASSWORD
MAIN_REDIS_PASSWORD
MARKET_SESSION_COOKIE
OWN_SELLER_ID
```

## Worker fast 2

На VPS #2:

```bash
cp .env.worker-fast-2.example .env
nano .env
docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build worker-fast-2
```

## Worker slow

На VPS #3:

```bash
cp .env.worker-slow.example .env
nano .env
docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build worker-slow
```

## Проверить IP worker

На каждом worker VPS:

```bash
docker compose -f docker-compose.yml -f docker-compose.worker.yml run --rm worker-fast-1 python -m app.check_worker_ip
```

Для fast 2 заменить service:

```bash
docker compose -f docker-compose.yml -f docker-compose.worker.yml run --rm worker-fast-2 python -m app.check_worker_ip
```

Для slow:

```bash
docker compose -f docker-compose.yml -f docker-compose.worker.yml run --rm worker-slow python -m app.check_worker_ip
```

IP у трех worker должны быть разными.

## Проверить лимиты

На любом сервере:

```bash
python -m app.check_worker_limits
```

В Docker:

```bash
docker compose run --rm worker-fast-1 python -m app.check_worker_limits
```

Команда покажет позиции, лимит и примерную частоту для `fast_1`, `fast_2`, `slow`.

## Проверить Telegram

1. Откройте бота.
2. Нажмите `/start`.
3. Откройте `📊 Серверы и лимиты`.
4. Проверьте:
   - Fast 1 показывает `500, 800, 1000`;
   - Fast 2 показывает `400, 1200, 1700, 2000`;
   - Slow показывает остальные позиции;
   - IP появились после первого heartbeat worker;
   - Dry-run включен, если `DRY_RUN=true`;
   - Safe mode выключен, если ошибок нет.

## Как изменить позиции

Поменяйте эти переменные в `.env`:

```env
WORKER_FAST_1_POSITIONS=500,800,1000
WORKER_FAST_2_POSITIONS=400,1200,1700,2000
WORKER_SLOW_POSITIONS=40,80,200,2100,2500,3600,4500,10000,22500
```

Важно: одна позиция не должна быть в двух группах. `python -m app.check_worker_limits` покажет ошибку, если позиция назначена дважды.

На main server тоже держите эти значения актуальными, потому что Telegram берет схему из main `.env`.

## Как изменить лимиты

Поменяйте:

```env
GLOBAL_REQUEST_LIMIT_PER_MINUTE=300
WORKER_FAST_1_REQUEST_LIMIT_PER_MINUTE=100
WORKER_FAST_2_REQUEST_LIMIT_PER_MINUTE=100
WORKER_SLOW_REQUEST_LIMIT_PER_MINUTE=100
```

После изменения перезапустите нужные контейнеры:

```bash
docker compose restart worker-fast-1
```

## Как добавить новый VPS

1. Добавьте новую группу worker в коде `app/repricer/worker_groups.py`.
2. Добавьте лимит и positions в `app/core/config.py`.
3. Добавьте service в `docker-compose.yml` и `docker-compose.worker.yml`.
4. Добавьте `.env.worker-new.example`.
5. Разрешите IP нового VPS в firewall main server.
6. Запустите worker на новом VPS.
7. Проверьте Telegram и `python -m app.check_worker_limits`.

## Как убрать VPS

1. Остановите worker на этом VPS:

```bash
docker compose down
```

2. Перенесите его позиции в другую группу через env positions.
3. Перезапустите оставшиеся worker.
4. Проверьте Telegram.

## Как вернуться к одному серверу

Можно запустить все на одном сервере:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
```

Так поднимутся PostgreSQL, Redis, bot и три worker на одной машине. IP будет один, но код, lock и dry-run останутся теми же.

## Как остановить

```bash
docker compose down
```

Для main server с override:

```bash
docker compose -f docker-compose.yml -f docker-compose.main.yml down
```

## Как обновить проект

На каждом сервере:

```bash
git pull
docker compose build
docker compose up -d
```

Для worker VPS используйте тот же compose command, которым запускали нужный service.

## Проверки после запуска

```bash
docker compose config
python -m app.check_worker_limits
python -m app.check_worker_ip
docker compose ps
docker compose logs --tail=100 worker-fast-1
docker compose logs --tail=100 worker-fast-2
docker compose logs --tail=100 worker-slow
```

Проверьте, что `DRY_RUN=true`, пока не готов реальный write endpoint Starvell.
