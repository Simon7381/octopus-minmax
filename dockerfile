FROM docker.io/library/python:3.12-slim-bookworm

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libx11-xcb1 \
    libxt6 \
    libasound2 \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m invisible_playwright fetch
COPY . /app
RUN mkdir -p /app/logs
EXPOSE 5050

CMD ["python", "-u", "src/main.py"]
