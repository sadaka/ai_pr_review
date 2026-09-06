# Credentials walkthrough — ai-pr-review

Get these **one at a time, in milestone order** (see `PLAN.md`). You don't need everything up
front — M1 (Tiger Cloud) needs nothing else. Every value below goes into `backend/.env`
(copy `backend/.env.example` to `backend/.env` first — it's already gitignored).

All options below are free-tier / no-credit-card, matched to "not scaling yet."

---

## 1. TIGER_DATABASE_URL — needed for M1

1. Go to the Tiger Cloud signup page (console.cloud.timescale.com/signup) and choose **Sign up for
   Tiger Cloud**.
2. Enter name, work email, a password (≥12 chars). No credit card required — new accounts get
   **$1,000 in credit, valid 30 days**.
3. Create a new Tiger Cloud **service** (Postgres-compatible). Pick the smallest/free-tier size.
4. Once the service is up, open its **Connection Info** panel and copy the full connection string
   (starts with `postgres://...`, `sslmode=require`).
5. Put it in `backend/.env`:
   ```
   TIGER_DATABASE_URL=postgres://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=require
   ```

---

## 2. REDIS_URL — needed for M2

Recommended: **Upstash** (free tier, serverless-friendly, standard Redis protocol works with ARQ).

1. Go to upstash.com, sign up free (GitHub/Google login works, no card needed).
2. Create a new **Redis** database — pick the free tier, any nearby region.
3. On the database page, copy the **Redis Connect URL** (the `rediss://` one, TLS).
4. Put it in `backend/.env`:
   ```
   REDIS_URL=rediss://default:PASSWORD@HOST:PORT
   ```

(Alternative: Redis Cloud's free 30MB tier at redis.io/try-free — same idea, copy the connection
string it gives you.)

---

## 3. GitHub App — needed for M2 (and reused by M5 to post reviews)

You need a **GitHub App**, not a personal access token — it's what lets the bot post reviews with
its own identity and scoped permissions.

1. Go to your GitHub account (or an org you own) → **Settings → Developer settings → GitHub Apps →
   New GitHub App**.
2. Fill in:
   - **GitHub App name**: anything unique, e.g. `ai-pr-review-yourname-dev`
   - **Homepage URL**: any placeholder is fine for dev, e.g. `https://github.com/yourusername`
   - **Webhook URL**: use the smee.io URL from step 4 below (get that first, then come back)
   - **Webhook secret**: generate a random string yourself (e.g. `openssl rand -hex 32`) and save it
     — this becomes `GITHUB_WEBHOOK_SECRET`
3. **Permissions** (Repository permissions):
   - Pull requests: **Read & write**
   - Contents: **Read-only**
   - Metadata: **Read-only** (mandatory, auto-selected)
4. **Subscribe to events**: check **Pull request**.
5. Click **Create GitHub App**. On the app's page:
   - Copy the **App ID** at the top → `GITHUB_APP_ID`
   - Scroll to **Private keys** → **Generate a private key**. This downloads a `.pem` file.
     Move it to `backend/secrets/github-app-private-key.pem` (that directory is gitignored).
6. Click **Install App** (left sidebar) and install it on the specific repo(s) you want the bot to
   review — pick a throwaway/test repo you own first, not something important.
7. Put the three values in `backend/.env`:
   ```
   GITHUB_APP_ID=123456
   GITHUB_WEBHOOK_SECRET=<the random string you generated>
   GITHUB_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem
   ```

---

## 4. smee.io — local webhook forwarding for dev (no account needed)

GitHub can't reach `localhost` directly, so during local development you proxy webhooks through
smee.io (this is GitHub's own recommended dev tool, free, no signup).

1. Go to smee.io and click **Start a new channel**. Copy the URL it gives you
   (e.g. `https://smee.io/aBcD1234`).
2. Use that exact URL as the **Webhook URL** when creating the GitHub App in step 3 above.
3. When you're running the backend locally, forward smee → your local server:
   ```
   npx smee-client --url https://smee.io/aBcD1234 --path /webhook --port 8000
   ```
   (Requires only Node.js, no separate account/signup.)

You only need this for local dev. If/when you deploy the backend somewhere with a public URL,
point the GitHub App's Webhook URL directly at that instead and drop smee.

---

## 5. OPENAI_API_KEY — needed for M4

1. Go to platform.openai.com → sign up / log in.
2. **API keys** (left sidebar) → **Create new secret key**. Copy it immediately (shown once).
3. Note: unlike the others above, this one is **usage-based, not free** — but for dev-scale testing
   (a handful of diffs, small embeddings) the cost is cents, not dollars. Add a few dollars of
   billing credit before M4 so calls don't fail on a zero balance.
4. Put it in `backend/.env`:
   ```
   OPENAI_API_KEY=sk-...
   ```

---

## Order of operations (matches PLAN.md milestone order)

| Milestone | Credential(s) needed | Get it from |
|---|---|---|
| M1 | `TIGER_DATABASE_URL` | Tiger Cloud signup |
| M2 | `REDIS_URL`, `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_PATH`, smee.io URL | Upstash + GitHub App + smee.io |
| M3 | none new | — |
| M4 | `OPENAI_API_KEY` | platform.openai.com |
| M5 | none new (reuses M2's GitHub App) | — |
| M6 | none new | — |
| M7 | none new | — |

Get each credential right before its milestone, not all up front — that way nothing sits unused
and you can verify each one works in isolation via that milestone's demo command.
