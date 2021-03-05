FROM python:3.9.1-alpine3.13

RUN  mkdir -p /kvstore/server/
COPY start requirements.txt /kvstore/
COPY server/ /kvstore/server/
RUN  cd /kvstore && pip install -r requirements.txt

WORKDIR  /kvstore
ENTRYPOINT ["./start"]
