FROM node:20-bookworm-slim
# git é necessário porque o Baileys (QR Code) puxa uma dependência do GitHub
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY bridge/package.json bridge/package.json
RUN cd bridge && npm install --omit=dev
COPY . .
ENV APP_ENV=production DATA_DIR=/data
CMD ["sh", "start.sh"]
