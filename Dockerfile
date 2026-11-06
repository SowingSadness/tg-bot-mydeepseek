FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Каталог кеша сравнений (/cmp) принадлежит рабочему пользователю: /app создан от root.
RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/var/cmp \
    && chown -R botuser:botuser /app/var
USER botuser

CMD ["python", "-m", "bot.main"]
