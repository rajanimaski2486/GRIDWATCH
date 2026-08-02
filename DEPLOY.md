# Deploying GridWatch

Three pieces. Only one holds state, and none of them run a model.

| Piece | Where | Free? |
|---|---|---|
| Dashboard (`index.html`) | Vercel | yes, no card |
| FastAPI + NAT runtime | Hugging Face Spaces (Docker) | yes, no card |
| Incidents + subscriptions | Turso (libSQL) | yes, no card |
| Reasoning | build.nvidia.com — called, never deployed | free credits |

Everything below has been verified locally against the real image except the
hosted steps themselves, which need your accounts.

---

## 0. Verify locally first

```bash
docker build -t gridwatch .
docker run -p 7860:7860 --env-file .env -e ALERTS_ENABLED=false gridwatch
```

Then open http://localhost:7860 and check:

```bash
curl -s localhost:7860/api/agent/status | python3 -m json.tool
```

You want `"state": "ready"` and `"tools": 29`. If `state` is `degraded`, the
`detail` field says exactly why and the server still serves the map and the
incident API in read-only mode — that is by design, not a partial failure.

Measured on this image: healthy in ~10 s, 3.25 GB, NAT builds all 29 tools.

---

## 1. Backend → Hugging Face Spaces

Free CPU tier is 2 vCPU / 16 GB, public HTTPS, no credit card. Docker SDK, port
7860 (already the image default).

```bash
pip install huggingface_hub
huggingface-cli login
huggingface-cli repo create gridwatch --type space --space_sdk docker
git remote add hf https://huggingface.co/spaces/<you>/gridwatch
git push hf nat-migration:main
```

A Space reads its config from a YAML header in `README.md`. Add this as the
**very first lines** of the file you push:

```yaml
---
title: GridWatch
emoji: 🌊
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---
```

Then in **Settings → Variables and secrets** add:

| Name | Kind | Notes |
|---|---|---|
| `NVIDIA_API_KEY` | secret | the only one needed to think |
| `DATABASE_URL` | secret | from step 2 |
| `DATABASE_AUTH_TOKEN` | secret | from step 2 |
| `GRIDWATCH_TOKEN` | secret | see step 4 — set this |
| `ALLOWED_ORIGINS` | variable | your Vercel URL, after step 3 |
| `ALERTS_ENABLED` | variable | **`false` until you mean it** |
| `TWILIO_*`, `DISCORD_TOKEN` | secret | only if you enable those channels |

> Free Spaces sleep after inactivity and the filesystem resets on rebuild.
> The first wake includes a NAT workflow build (~10 s here). Step 2 is what
> keeps your data across that.

---

## 2. State → Turso

Without this, every incident and subscription disappears on restart.

```bash
curl -sSfL https://get.tur.so/install.sh | bash
turso auth signup
turso db create gridwatch
turso db show gridwatch --url          # -> DATABASE_URL
turso db tokens create gridwatch       # -> DATABASE_AUTH_TOKEN
```

`db.py` switches automatically when `DATABASE_URL` is set; tables are created on
first connect. libSQL speaks the SQLite dialect, so no queries change. Install
the client with the extra:

```bash
pip install '.[remote-db]'
```

Leave `DATABASE_URL` unset for local development and you stay on plain SQLite.

---

## 3. Frontend → Vercel

```bash
npm i -g vercel
vercel --prod
```

`vercel.json` at the repo root copies `index.html` into a build output dir and
proxies `/api/*` and `/generate` to the Space. **Edit the two `REPLACE-ME.hf.space`
entries first.**

Because the rewrite makes API calls same-origin, `index.html` needs no API base
URL and CORS never enters the picture. Set `ALLOWED_ORIGINS` on the Space to
your Vercel domain anyway — it is the backstop if someone hits the Space
directly.

Also set a real Mapbox token at [index.html:809](src/hackathon_nyc/frontend/index.html:809).
It is public by nature; restrict it by URL in the Mapbox dashboard.

---

## 4. Before you make the URL public

- [ ] **`GRIDWATCH_TOKEN`** — without it, anyone who finds the deployment can
      create, edit and delete incidents. Set it, and send
      `X-GridWatch-Token: <value>` from any admin client. Reads stay open so the
      public map still works.
- [ ] **`ALERTS_ENABLED=false`** until you have verified the confirmation flow
      with real subscribers. This is the difference between a bug and a bug that
      texts strangers at 3am.
- [ ] `ALLOWED_ORIGINS` set to your Vercel domain, not `*`.
- [ ] Twilio webhook signature validation, if you enable the phone channel.
- [ ] A rate limit on `/generate` — each call costs NIM credits and there is
      nothing stopping a script from looping it.

---

## What is deliberately not deployed

**The background monitor.** It stays off behind `GRIDWATCH_MONITOR=1` because it
creates incidents directly rather than through the policy gate. Turning it on
before that refactor reintroduces the alerting problem from a different
direction. It also assumes a long-lived process, which a scale-to-zero host
breaks.

**Any model.** No GPU, no CUDA, no weights in the image. That is what makes a
free CPU tier viable.

---

## Other hosts

| Host | Notes |
|---|---|
| **Google Cloud Run** | Real free tier, needs a card. Scale-to-zero kills the monitor; pair with Cloud Scheduler if you enable it. |
| **Render** | Free, no card, but 512 MB RAM is tight against a 3.25 GB image with ChromaDB and onnxruntime resident. Spins down after 15 min. |
| **Fly.io / Railway** | Trial credit only. |
| **Vercel** | Frontend only — serverless, 60 s cap, no persistent disk, no background loop, 250 MB bundle limit. |
