FROM python:3
WORKDIR /usr/src/app
COPY requirements.txt .
RUN pip3 install -r requirements.txt
COPY main.py .
CMD [ "python3", "./main.py" ]
