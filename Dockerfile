# Stage 1: Build UI
FROM node:22-slim AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY vite.config.ts tsconfig.json viewer.html ./
COPY src/ ./src/
RUN npm run build

# Stage 2: Production
FROM node:22-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=builder /app/dist/viewer.html ./dist/
COPY server.ts ./
COPY data/artworks.db ./data/
EXPOSE 3001
CMD ["npm", "start"]
