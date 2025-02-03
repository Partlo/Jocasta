FROM python:3.10-slim

WORKDIR /app

COPY . .

RUN pip3 install --no-cache-dir -r requirements.txt

COPY jocasta/ jocasta/

ENV PYTHONPATH /app

CMD ["python", "main.py"]