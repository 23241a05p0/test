FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rate_limiter.py token_bucket.lua app.py ./

EXPOSE 8080
CMD ["python", "app.py"]
