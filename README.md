services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-stroykontrol}
      POSTGRES_USER: ${POSTGRES_USER:-stroykontrol}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-stroykontrol} -d ${POSTGRES_DB:-stroykontrol}"]
      interval: 5s
      timeout: 5s
      retries: 20
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes: [redisdata:/data]
  minio:
    image: minio/minio:latest
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:-stroykontrol}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?Set MINIO_ROOT_PASSWORD in .env}
    volumes: [miniodata:/data]
  app:
    build: .
    restart: unless-stopped
    env_file: .env
    depends_on:
      db: {condition: service_healthy}
      redis: {condition: service_started}
      minio: {condition: service_started}
    ports: ["8000:8000"]
    volumes: ["./uploads:/app/uploads"]
volumes:
  pgdata:
  redisdata:
  miniodata:
