FROM docker.io/library/python:3.12-slim-bookworm

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m invisible_playwright fetch
COPY . /app
RUN mkdir -p /app/logs
EXPOSE 5050

CMD ["python", "-u", "src/main.py"]
