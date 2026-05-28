# VPS deploy guide

This guide is for moving Starvell Repricer from a local Mac to a VPS.

## Important safety rule

Do not run the Mac worker and the VPS worker at the same time with the same Starvell
account/session.

Before starting on the VPS:

```bash
docker compose stop worker bot
```

or fully stop the local stack:

```bash
docker compose down
```

## Recommended VPS

```text
OS: Ubuntu 22.04 LTS
RAM: 2 GB minimum
CPU: 1-2 vCPU
Region: Moscow or closest low-latency region to Starvell/proxy provider
Disk: 20 GB minimum
```

## Install Docker

Run on the VPS:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and log back in, then check:

```bash
docker --version
docker compose version
```

## Clone the repository

```bash
git clone https://github.com/rlstasha/starvell-repricer.git
cd starvell-repricer
git checkout codex/safe-speed-optimizations
```

## Configure `.env`

Create the runtime env file:

```bash
cp .env.example .env
nano .env
```

Copy from the Mac `.env`:

```text
Telegram bot token
owner Telegram id
Starvell cookies/session/auth values
proxy URLs
price write endpoint settings
database/redis passwords if customized
```

Do not paste `.env` into chats or logs. It contains secrets.

## Start

```bash
docker compose config -q
docker compose up -d --build
docker compose ps
```

Expected services:

```text
postgres
redis
migrate exited 0
seed exited 0
worker running
bot running
```

## Check logs

```bash
docker compose logs --tail=120 worker
docker compose logs --tail=120 bot
```

Useful live checks:

```bash
docker compose logs --since=10m worker | grep -E "price_updated|price_update_failed|429|rate_limited|repricer_limiter_snapshot"
docker compose logs --since=10m bot | grep -E "telegram_polling|TelegramNetworkError|callback"
```

## Safe update

```bash
git fetch origin
git pull --ff-only
docker compose config -q
docker compose run --rm worker python -m pytest -q
docker compose up -d --build
docker compose ps
```

## Roll back to an older commit

Find a known-good commit:

```bash
git log --oneline -n 20
```

Checkout and rebuild:

```bash
git checkout <commit_hash>
docker compose up -d --build
docker compose ps
```

If rollback fixes production, create a note with:

```text
bad commit
good commit
worker logs around the failure
Telegram symptoms
```

## Redis limiter reset after changing limits

If request limits are changed, stop worker and clear old limiter/backoff keys before restart:

```bash
docker compose stop worker
docker compose exec -T redis sh -lc "redis-cli --scan --pattern 'repricer:account-token-limit*' | xargs -r redis-cli DEL; redis-cli --scan --pattern 'repricer:token-bucket:*' | xargs -r redis-cli DEL; redis-cli --scan --pattern 'repricer:burst:*' | xargs -r redis-cli DEL; redis-cli --scan --pattern 'repricer:backoff:*' | xargs -r redis-cli DEL"
docker compose up -d --build worker
```

## Do not run two active workers

Running Mac and VPS at the same time with the same Starvell session can cause:

```text
duplicate price writes
extra 429/rate_limited
confusing Telegram status
stale Redis/account limiter state
```

One active production stack at a time is the safe rule.

