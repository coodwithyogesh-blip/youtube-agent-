FROM ubuntu:24.04 AS telegram-api-build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git cmake g++ gperf make zlib1g-dev libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git
WORKDIR /src/telegram-bot-api
RUN mkdir build && cd build && cmake -DCMAKE_BUILD_TYPE=Release .. && cmake --build . --target install -j2

FROM python:3.12-slim
RUN apt-get update && apt-get install -y libssl3 zlib1g && rm -rf /var/lib/apt/lists/*
COPY --from=telegram-api-build /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY start-local-api.sh .
RUN chmod +x start-local-api.sh
CMD ["./start-local-api.sh"]
