FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml .
COPY wakefinder wakefinder
RUN pip install --no-cache-dir -e .

# Один образ на все 4 процесса — какой запускать, выбирается командой при
# `docker run`/в docker-compose, не встроено в образ:
#   docker run --env-file .env wakefinder python -m wakefinder.chains.eth.main
#   docker run --env-file .env wakefinder python -m wakefinder.chains.eth.copytrade
#   docker run --env-file .env wakefinder python -m wakefinder.chains.solana.main
#   docker run --env-file .env wakefinder python -m wakefinder.chains.solana.copytrade
# Голый python -m запускается с пустыми pool_registry/watched_wallets (см.
# README "Инфраструктурная надёжность") — реальный конфиг подставляйте своей
# командой/скриптом-обёрткой поверх этого образа.
ENTRYPOINT ["python"]
