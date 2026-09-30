# One-shot image that applies the Prisma migrations: `docker compose run --rm migrate`.
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /db
COPY package.json package-lock.json prisma.config.ts ./
RUN npm ci --no-audit --no-fund
COPY prisma ./prisma
CMD ["npx", "prisma", "migrate", "deploy"]
