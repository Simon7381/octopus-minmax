# Use scripts/build_image.py or the release workflow to resolve this version.
ARG PLAYWRIGHT_VERSION
FROM docker.io/library/python:3.14-slim-bookworm AS python_runtime
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

# Apply Ubuntu security updates before adding the application runtime.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Microsoft's Noble image supplies the fonts and browser system dependencies.
# Keep the application on Python 3.14 instead of Noble's system Python 3.12.
COPY --from=python_runtime /usr/local /usr/local
ENV PATH="/usr/local/bin:${PATH}"
ARG PLAYWRIGHT_VERSION

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade --no-cache-dir -r requirements.txt "playwright==${PLAYWRIGHT_VERSION}"
# Invisible Playwright uses a separate Firefox build from Microsoft's browsers.
RUN python -m invisible_playwright fetch
COPY . /app
RUN mkdir -p /app/logs
EXPOSE 5050

CMD ["python", "-u", "src/main.py"]
