FROM joyzoursky/python-chromedriver:3.9-selenium
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY test23.py .
COPY pollutions_names.json .
ENTRYPOINT ["python3", "test23.py"]