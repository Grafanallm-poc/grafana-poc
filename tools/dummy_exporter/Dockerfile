FROM python:3.12-slim

WORKDIR /srv

COPY tools/dummy_exporter/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/dummy_exporter/exporter.py ./exporter.py

CMD ["python", "exporter.py"]
