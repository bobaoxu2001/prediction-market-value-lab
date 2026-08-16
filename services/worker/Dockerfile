# The recurring scheduler, as the docker-compose "worker" service.
#
# Unlike the API, the worker runs the full pipeline including backtest and
# calibration jobs, which import the quant stack - so it installs
# requirements-dev.txt rather than the slim API runtime set.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt "psycopg[binary]>=3.2,<4"

COPY packages ./packages
COPY services/worker ./services/worker
COPY alembic.ini ./alembic.ini

RUN pip install --no-cache-dir -e packages/shared -e packages/market-normalization -e services/worker

CMD ["pmvl", "schedule"]
