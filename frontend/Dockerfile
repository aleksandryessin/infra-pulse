FROM node:24-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Build-time UI data mode: fixture (default, synthetic) | replay | received.
ARG VITE_DATA_MODE=fixture
# 1: show the «Графики сигналов» menu link (Grafana at /grafana/, analyst and admin only).
ARG VITE_GRAFANA_LINK=
RUN VITE_DATA_MODE="$VITE_DATA_MODE" VITE_GRAFANA_LINK="$VITE_GRAFANA_LINK" npm run build
FROM nginx:1.28-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
