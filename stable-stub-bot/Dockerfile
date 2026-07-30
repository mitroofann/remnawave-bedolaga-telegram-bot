FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stub_bot.py .
COPY assets/ ./assets/

CMD ["python", "stub_bot.py"]
