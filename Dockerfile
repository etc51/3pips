FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements.txt \
    && python -m pip install \
        --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple \
        t-tech-investments

COPY . .

RUN mkdir -p reports/runtime reports/paper_runs/v7_live_20260525 reports/archives \
    && python -m py_compile \
        scripts/ubuntu_paper_supervisor.py \
        scripts/archive_paper_run.py \
        src/multi_futures_paper.py \
        src/multi_stocks_paper.py \
        src/paper_dashboard.py

EXPOSE 8768

CMD ["python", "scripts/ubuntu_paper_supervisor.py", "--project-root", "/app", "--python", "/usr/local/bin/python", "--dashboard-host", "0.0.0.0", "--dashboard-port", "8768"]

