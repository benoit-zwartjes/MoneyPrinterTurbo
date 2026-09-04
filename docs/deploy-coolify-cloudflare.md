# Deploying the WebUI on Coolify behind Cloudflare Access

Private, single-user deployment of the MoneyPrinterTurbo WebUI. The app itself has
**no login of any kind** — every visitor who can reach port 8501 gets full control of
your API keys and can spend your credits. The access control therefore lives entirely
at Cloudflare's edge, in front of the tunnel.

```
browser
  → Cloudflare edge        ← Access policy (only your email gets through)
  → Cloudflare Tunnel      ← cloudflared on the Coolify server
  → coolify-proxy :80      ← Traefik, routes by Host header
  → moneyprinterturbo :8501
```

Only the WebUI is deployed. The FastAPI service in `main.py` is **not** needed:
`webui/Main.py` imports `app.services.task` directly rather than calling the HTTP API,
so running the API container would only add a second unauthenticated surface.

---

## 1. Coolify — deploy the WebUI

**New Resource → Private Repository (with GitHub App)** — or Public Repository if the
fork is public.

| Setting | Value |
| --- | --- |
| Repository | `https://github.com/benoit-zwartjes/MoneyPrinterTurbo` |
| Branch | `main` |
| Build Pack | **Dockerfile** |
| Dockerfile location | `/Dockerfile` |
| Ports Exposes | `8501` |
| Domain | `http://mpt.example.com` — **http, not https** |

### Build arguments (do not skip)

`Dockerfile` defaults to Chinese package mirrors, which are slow or fail outright from a
European or US server. Under **Environment Variables**, add both as *build-time* variables:

```
DOCKER_BUILD_MIRROR=default
PIP_USE_OFFICIAL=1
```

Without these the build retries Aliyun then Tsinghua on every `apt-get` and `pip install`
before falling back, turning a ~5 minute build into a very long one.

### Domain must be `http://`

Cloudflare terminates TLS at the edge and the tunnel carries traffic to Traefik over
plain HTTP. Setting an `https://` domain in Coolify makes Traefik try to issue a Let's
Encrypt certificate for a hostname that has no public A record, and adds an HTTP→HTTPS
redirect that turns into a loop behind the tunnel.

### Health check

| Setting | Value |
| --- | --- |
| Path | `/_stcore/health` |
| Port | `8501` |

This is Streamlit's own readiness endpoint (`_stcore/health`, returns `ok`).

### Persistent storage

Both entries matter — without them a redeploy silently wipes your API keys.

**Volume mount** — generated videos, downloaded stock footage, caches:

| Field | Value |
| --- | --- |
| Name | `mpt-storage` |
| Destination Path | `/MoneyPrinterTurbo/storage` |

**File mount** — the config file the app writes your API keys back into:

| Field | Value |
| --- | --- |
| Destination Path | `/MoneyPrinterTurbo/config.toml` |
| Content | paste the whole of `config.example.toml` |

Get the content onto your clipboard locally with:

```bash
pbcopy < config.example.toml
```

`app/config/config.py` writes this file on every settings change. It normally does an
atomic `os.replace()`, which fails with `EBUSY` on a Docker single-file bind mount — the
code already catches exactly that and falls back to an in-place write, so a file mount is
safe here.

Deploy. The build takes a while: the image installs `ffmpeg` and the full Python
dependency set.

---

## 2. Cloudflare — confirm the tunnel covers this hostname

The tunnel already exists, so this is a check rather than a setup. Open
**Zero Trust → Networks → Tunnels →** your tunnel **→ Public Hostname** and read the
*Service* column of the existing rows. It decides whether Coolify's domain field routes
anything at all.

**A · A wildcard row already points at the proxy** — `*.example.com → http://coolify-proxy:80`.
Nothing to do. Traefik picks the new container up from the domain set in step 1 as soon as
it deploys.

**B · One row per app, all pointing at the proxy.** Add one more in the same shape:

| Field | Value |
| --- | --- |
| Subdomain | `mpt` |
| Domain | `example.com` |
| Service | `HTTP` → whatever the neighbouring rows use |

**C · Rows point at individual host ports** (`localhost:3000`, `localhost:8080`, …). The
tunnel is bypassing Traefik and talking to published container ports directly. Either add
a row for `mpt` pointing at `coolify-proxy:80` — preferred, because Coolify's domain field
then does the routing and later apps need no tunnel changes — or stay with the existing
pattern by publishing a host port on the resource (`127.0.0.1:8501:8501` under Ports
Mappings) and pointing the row at that. In the second case the Coolify domain field is
cosmetic and Traefik is not involved.

`coolify-proxy:80` only resolves when `cloudflared` runs as a container attached to the
`coolify` Docker network. Use `localhost:80` when it runs on the host or with host
networking — the rows already there will show which applies.

---

## 3. Cloudflare Access — the actual password gate

**Zero Trust → Access → Applications → Add an application → Self-hosted.**

| Field | Value |
| --- | --- |
| Application name | `MoneyPrinterTurbo` |
| Session Duration | `24 hours` (or `1 week` — you will re-auth this often) |
| Public hostname | `mpt.example.com` |

Then add one policy:

| Field | Value |
| --- | --- |
| Policy name | `only-me` |
| Action | **Allow** |
| Include | **Emails** → `benoit.zwartjes@icloud.com` |

For login method, **One-time PIN** needs no identity provider — Cloudflare emails you a
code. Add Google/GitHub as well if you prefer a click instead of a code. The Zero Trust
free plan covers up to 50 users, so a single-user setup costs nothing.

Streamlit is a WebSocket app, and both Cloudflare Tunnel and the Cloudflare proxy support
WebSockets with no extra configuration. The `CF_Authorization` cookie is sent on the
WebSocket handshake, so Access applies to it too.

### Close the bypass

Access only protects traffic that arrives **through Cloudflare**. If the Coolify server's
ports 80/443 are reachable from the internet and anything resolves to its IP, that path
skips the policy entirely. Confirm no DNS record points at the server's raw IP, and
firewall 80/443 to the tunnel only.

---

## 4. Verify

Before logging in, the request should be bounced to Cloudflare's login page, not answered
by the app:

```bash
curl -sI https://mpt.example.com | head -n 5
```

Expect a `302` to `https://<your-team>.cloudflareaccess.com/...`. A `200` with Streamlit
HTML means the Access policy is not attached to this hostname — fix that before entering
any API keys.

Then open the site in a browser, complete the login, and check that the UI reaches
**"Running"** rather than sitting on a *Connecting…* spinner. A stuck spinner means the
WebSocket is not getting through — see below.

---

## Notes and gotchas

**The WebSocket origin check is fine as shipped.** Most "Streamlit behind a reverse proxy"
guides tell you to set `--server.enableCORS=false --server.enableXsrfProtection=false`.
You do not need that here, and should not do it. Streamlit 1.59's `_is_origin_allowed`
permits any request whose `Origin` host matches the `Host` header, and both cloudflared
and Traefik preserve the original Host — so same-origin traffic is allowed regardless of
`--browser.serverAddress=127.0.0.1` in the Dockerfile `CMD`.

**Uploads are capped at 100 MB** on Cloudflare's free plan. That limits locally-uploaded
background music and video material; generated output is unaffected.

**Long renders are safe.** Cloudflare's 100-second HTTP timeout does not apply to an open
WebSocket, and video generation progress flows over the Streamlit socket rather than a
blocking HTTP request. Downloading a very large finished video over a slow link is the one
place the HTTP limit can bite.

**Sizing.** Rendering is ffmpeg-bound and memory-hungry. Give the server real CPU and at
least a few GB of RAM headroom, and keep an eye on the `mpt-storage` volume — generated
video accumulates quickly.

**Upstream merges.** Nothing here patches application code, so `git merge upstream/main`
stays clean. The two build arguments and the Streamlit `CMD` are upstream defaults being
overridden from Coolify, not edited in the repo.
