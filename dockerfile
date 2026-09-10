FROM docker.io/library/python:3.12-slim-bookworm

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps firefox
RUN mkdir -p /app/logs
EXPOSE 5050

CMD ["python", "-u", "src/main.py"]
