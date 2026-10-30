FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/factory \
    BT_WALLET_PATH=/home/factory/.bittensor/wallets \
    FACTORY_BIND_HOST=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY subnet_factory ./subnet_factory
COPY scripts ./scripts
COPY examples ./examples
RUN pip install --no-cache-dir . \
    && useradd --system --create-home --uid 10001 factory \
    && mkdir -p /app/data /app/artifacts \
    && chown -R factory:factory /app

USER 10001:10001
EXPOSE 8000
CMD ["sh", "scripts/run_service.sh"]
