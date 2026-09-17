# Optional status page. Built only under `docker compose --profile frontend`.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# M14/M16: the frontend reads the API URL client-side (localStorage-held bearer
# token means the fetch runs in the browser, not the server), and Next.js only
# inlines NEXT_PUBLIC_* vars at `next build` time — a runtime `environment:` in
# docker-compose.yml is too late. Must come in as a build ARG.
ARG NEXT_PUBLIC_BACKEND_URL
ENV NEXT_PUBLIC_BACKEND_URL=${NEXT_PUBLIC_BACKEND_URL}
RUN npm run build

FROM node:22-slim AS run
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/package.json /app/package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /app/.next ./.next
COPY --from=build /app/next.config.mjs ./
EXPOSE 3000
CMD ["npm", "run", "start"]
