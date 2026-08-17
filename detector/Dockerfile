FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY detector.py .

EXPOSE 8003
CMD ["uvicorn", "detector:app", "--host", "0.0.0.0", "--port", "8003"]
