# The read-only research API, as the docker-compose "api" service.
#
# Slim on purpose: the API never imports the quant stack (numpy/pandas/scipy),
# so requirements.txt - not requirements-dev.txt - is the dependency set. The
# shared package pulls in alembic, which the compose entrypoint uses to migrate
# the Postgres database before serving.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt uvicorn[standard]==0.38.0

COPY packages ./packages
COPY services/api ./services/api
COPY alembic.ini ./alembic.ini

RUN pip install --no-cache-dir -e packages/shared -e packages/market-normalization -e services/api

EXPOSE 8000
CMD ["uvicorn", "pmvl_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
