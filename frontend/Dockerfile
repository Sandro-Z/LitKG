FROM python:3.11-slim

WORKDIR /frontend

COPY requirements.txt /frontend/requirements.txt
RUN pip install --no-cache-dir -r /frontend/requirements.txt

COPY app.py /frontend/app.py

EXPOSE 8501

CMD ["streamlit", "run", "/frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
