# SpyRobot TURN credential worker

A tiny Cloudflare Worker that mints short-lived Cloudflare TURN credentials so
the dashboard (`index.html`) never has to hardcode them and the long-term
secret stays server-side.

`index.html` fetches this worker via its `TURN_CREDS_URL` constant and uses the
returned `{ "iceServers": [...] }` directly.

## Prerequisites

A Cloudflare TURN key. Dashboard → **Realtime → TURN Keys → Create**, then copy:
- **TURN Key ID**
- **API Token**

## Deploy (option A — Wrangler CLI)

```bash
npm install -g wrangler
cd turn-worker
wrangler login

# store the secrets (never put these in wrangler.toml)
wrangler secret put TURN_KEY_ID
wrangler secret put TURN_KEY_API_TOKEN

wrangler deploy
```

Wrangler prints the worker URL, e.g. `https://spyrobot-turn.<your-subdomain>.workers.dev`.

## Deploy (option B — dashboard, no CLI)

1. Dashboard → **Workers & Pages → Create → Worker**, name it `spyrobot-turn`.
2. **Edit code**, paste the contents of `worker.js`, **Deploy**.
3. Worker → **Settings → Variables**:
   - Add **Secret** `TURN_KEY_ID`
   - Add **Secret** `TURN_KEY_API_TOKEN`
   - (optional) Add var `TTL` = `86400`, and `ALLOWED_ORIGIN` = your site URL.

## Wire it into the dashboard

In `index.html`, set:

```js
const TURN_CREDS_URL = "https://spyrobot-turn.<your-subdomain>.workers.dev";
```

Now the inline `PASTE_CLOUDFLARE_TURN_*` placeholders are ignored — the browser
pulls fresh credentials on every load. (go2rtc.yaml still needs a pasted pair,
since go2rtc has no fetch hook; regenerate those when they expire, or keep a
long TTL.)

## Test

```bash
curl https://spyrobot-turn.<your-subdomain>.workers.dev
# -> { "iceServers": [ { "urls": ["stun:..."] }, { "urls": ["turn:..."], "username": "...", "credential": "..." } ] }
```

## Notes

- `ALLOWED_ORIGIN` gives a light origin check (Origin headers are spoofable by
  non-browser clients — add a shared token if you need real protection).
- TURN relay egress counts against your Cloudflare Realtime free allowance.
