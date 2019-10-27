FROM python:3
WORKDIR /usr/src/app
RUN pip3 install --user python-telegram-bot dataset cachetools requests Jinja2 mysqlclient
COPY . .
CMD [ "python3", "./main.py" ]