// ─────────────────────────────────────────────────────────────────────────
// SpyRobot — Cloudflare TURN credential minter
//
// Returns short-lived Cloudflare TURN ICE servers to the dashboard so the
// credentials never have to be pasted into index.html (and the long-term
// secret stays server-side). The browser's getIceServers() in index.html
// already understands the { "iceServers": [...] } shape this returns.
//
// Secrets (set with `wrangler secret put NAME`, or in the dashboard UI):
//   TURN_KEY_ID         your Cloudflare TURN key ID
//   TURN_KEY_API_TOKEN  the TURN key's API token
// Optional plain vars (wrangler.toml [vars]):
//   TTL                 credential lifetime in seconds (default 86400 = 24h)
//   ALLOWED_ORIGIN      if set (not "*"), only this Origin may call the worker
// ─────────────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const lock   = env.ALLOWED_ORIGIN && env.ALLOWED_ORIGIN !== "*";
    const cors = {
      "Access-Control-Allow-Origin": lock ? env.ALLOWED_ORIGIN : "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Vary": "Origin",
    };

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    // Optional (light) origin lock — note: Origin is spoofable by non-browser
    // clients, so add a shared token if you need real access control.
    if (lock && origin !== env.ALLOWED_ORIGIN) {
      return json({ error: "forbidden origin" }, 403, cors);
    }

    if (!env.TURN_KEY_ID || !env.TURN_KEY_API_TOKEN) {
      return json({ error: "worker missing TURN_KEY_ID / TURN_KEY_API_TOKEN" }, 500, cors);
    }

    const ttl = parseInt(env.TTL || "86400", 10);

    try {
      const r = await fetch(
        `https://rtc.live.cloudflare.com/v1/turn/keys/${env.TURN_KEY_ID}/credentials/generate-ice-servers`,
        {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${env.TURN_KEY_API_TOKEN}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ ttl }),
        },
      );
      if (!r.ok) {
        return json({ error: `cloudflare turn api ${r.status}`, detail: await r.text() }, 502, cors);
      }
      // { "iceServers": [ {urls:[stun...]}, {urls:[turn...], username, credential} ] }
      return json(await r.json(), 200, cors);
    } catch (e) {
      return json({ error: String(e) }, 502, cors);
    }
  },
};

function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...cors },
  });
}
