# Сервис «Разбор лога WCL». Сервер ничего не хранит о пользователях: ключ API, эталоны,
# настройки и история разборов живут в браузере каждого пользователя.
# Кэш публичных данных Warcraft Logs — только в оперативной памяти.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WCL_PUBLIC=1 \
    WCL_MEMCACHE_MB=150 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wcl_analyzer ./wcl_analyzer
COPY spell_meta.example.json ./

RUN useradd --create-home --uid 1000 app
USER app
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/status')"
CMD ["python", "-m", "wcl_analyzer", "ui", "--public", "--no-browser"]
