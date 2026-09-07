# frontend — status page (M7)

One Next.js page listing recent PR reviews (id, status, cost, confidence),
read from the backend's `GET /api/reviews`. Deliberately minimal — no design
system, no auth, no component library.

```bash
npm install
npm run dev          # http://localhost:3000
```

`BACKEND_URL` (default `http://localhost:8000`) points at the read API
(`uvicorn api.app:build_default_app --factory` in `../backend`). If the backend
is down the page still renders with an empty table.
