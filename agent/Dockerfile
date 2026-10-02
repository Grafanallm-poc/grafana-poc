FROM python:3.12-slim

WORKDIR /srv

COPY agent/requirements.txt ./agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY agent ./agent

ENV PYTHONPATH=/srv

CMD ["uvicorn", "agent.app:app", "--host", "0.0.0.0", "--port", "8000"]
