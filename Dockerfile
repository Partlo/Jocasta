FROM python:3.10-slim

WORKDIR /app

RUN mkdir /app/jocasta
COPY . .
COPY ./jocasta /app/jocasta
RUN export PYTHONPATH=/app/

RUN dir

RUN pip3 install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]