# Configuration

Two files describe this setup, and they don't overlap:
`config/config.yaml` is agentgateway's own config (what the proxy does),
`service.toml` is how the container is run (image, ports, mounts). This
page annotates both, plus the rest of the repo layout.

`config/config.yaml` carries no comments of its own on purpose:
agentgateway rewrites the file when the admin UI saves in `-w` mode, and
that rewrite round-trips it through a YAML parser, which strips every
comment. Anything explanatory kept in the file would silently disappear
the first time someone clicks Save — hence this page.

## `config/config.yaml`

Safe to commit: it only ever contains `$OPENAI_API_KEY` /
`$ANTHROPIC_API_KEY` placeholders, never resolved values. See
[Admin UI → Don't type a real API key into the UI](admin-ui.md#dont-type-a-real-api-key-into-the-ui)
for the one way that stops being true.

It lives in its own directory so the whole directory can be
bind-mounted — read-only by default, read-write with `-w`.

### `config.adminAddr: "off"`

Switches off agentgateway's built-in admin listener entirely. The UI is
served from the `ui` gateway below instead, because policies can't
attach to the admin address — leaving it on would be a second,
unauthenticated way to the same UI. The field also accepts `ip:port`,
`localhost:port` and `unix:/path/to/socket`.

### `config.logging.database.url`

`sqlite:///data/request-logs.db` — the request-log database backing the
admin UI's Logs page. Without it the UI errors with "request log
database is not configured". `/data` is the container-side path of the
`./data` mount. See [Request logs](request-logs.md), which is also where
the privacy implications live.

### `config.modelCatalog`

Points at `/config/base-costs.json`, the per-model input/output/cache
token rates agentgateway prices requests against. With no `modelCatalog`
configured at all, agentgateway falls back to a built-in default path
(`/base-costs.json`, the container's filesystem root), which isn't
writable — so a cost-catalog refresh from the UI fails there regardless
of `-w`.

That file is **not in the repo** — it's generated data that the UI
rewrites in full on every refresh, so tracking it meant 174KB of churn
per refresh. A fresh clone therefore starts without it, which is
harmless: agentgateway logs

```
warn llm::cost  model catalog load failed; will load when the files
                become valid: ... missing files: /config/base-costs.json
```

and serves normally, just without cost figures on the Logs page. To
populate it, start with `./run.py up -w` and refresh the cost catalog
from the admin UI. The file is watched, so it's picked up as soon as it
appears — no restart needed.

### `llm.policies.apiKey`

Every request to the LLM data plane (`:4000`) must present the gateway's
own API key as `Authorization: Bearer <key>`. Without it, any local
process — or any website open in a browser on this machine, via DNS
rebinding — could spend the provider keys.

Only the SHA-256 reaches the gateway, via `$GATEWAY_API_KEY_SHA256` in
the container environment; the key itself stays in `pass`. `mode:
strict` rejects unauthenticated requests outright rather than passing
them through unauthenticated. See [Security](security.md).

### `llm.policies.cors`

For the admin UI's playground, which calls `:4000` from the browser.
`allowHeaders` deliberately lists headers rather than including `'*'` —
a wildcard alongside a list makes the list meaningless, and
`authorization` is the header that matters here. `x-api-key` is
deliberately *not* listed: nothing here authenticates by that header
(Anthropic-format clients must send `Authorization: Bearer` too), so
allowing it would only advertise a way in that doesn't work.

### `llm.providers` / `llm.models`

Two upstream providers (`openai`, `anthropic`) resolving their
credentials from `$OPENAI_API_KEY` / `$ANTHROPIC_API_KEY`, and the
client-facing model names that map onto them. A client asks for
`claude-sonnet-5` or `gpt-5-mini` and agentgateway routes it to the
right provider with the right upstream key.

### `mcp`

Published on `:3000` with the same `apiKey` policy as the LLM listener.
`targets` is empty today — the policy goes in ahead of the first target,
since tool calls are a bigger blast radius than chat proxying, and it's
easier to add a target to an authenticated listener than to remember to
lock one down afterwards.

### `gateways.ui` and `ui`

The `ui` gateway exists purely to give the admin UI a listener that
policies can attach to, on the same port 15000 the admin address used to
use — so nothing changes for `service.toml` or for bookmarks. The
top-level `ui` section attaches the UI to that gateway and applies
`basicAuth` to all UI traffic.

`/config/htpasswd` is written by `run.py up` from the
`agentgateway-ui-password` entry in `pass`. Full reasoning in
[Security → The admin UI](security.md#the-admin-ui-basic-auth).

## `service.toml`

The single definition of the container — image, ports, env, volumes,
command. It replaced a duplicated
`docker-compose.yml`/`docker-compose.rw.yml` pair plus hardcoded bits of
the run script; `run.py` reads it with the stdlib `tomllib` and builds
the equivalent `docker run` or `container run` invocation directly.
There's no compose file and no per-backend config to keep in sync.

- **`image`** — pinned by digest as well as tag, so the registry can't
  re-point the tag under you. Bump both halves together on upgrade; read
  the new digest with `docker buildx imagetools inspect <ref>` or
  `container image inspect <ref>`.
- **No `restart_policy`** — deliberately unset, so both backends behave
  the same and the provider keys in container runtime metadata don't
  survive a reboot. See [Backends](backends.md) and [Security → Where
  the keys live](security.md#where-the-keys-live).
- **`ports`** — all three bound to loopback. That is *not* a security
  boundary on its own, which is why each listener carries its own auth;
  see [Security](security.md#loopback-publishing-isnt-a-boundary).
- **`env`** — bare `NAME` entries are passed to the runtime by name only
  (`-e NAME`), so values are inherited from `run.py`'s environment
  instead of appearing on the command line where `ps` would show them.
  `NAME=default` entries are non-secret and passed literally.
- **`config_volume` / `volumes`** — `./config` read-only by default,
  `./data` always read-write.
- **`command`** — `-f /config/config.yaml`, appended only outside `-w`
  mode so agentgateway loads exactly that file rather than managing the
  directory.

## Repo layout

| Path | What |
| --- | --- |
| `config/config.yaml` | agentgateway config (above). Tracked. |
| `config/base-costs.json` | Model cost catalog — pricing data, no secrets. Gitignored: it's generated, large, and rewritten wholesale by each UI refresh. |
| `config/htpasswd` | Admin UI's bcrypt basic-auth credentials, rewritten by every `up`, mode 0600. Gitignored; never edit by hand. |
| `config/config-jaeger.yaml` | Runtime-only copy generated by `up --jaeger`, with tracing pointed at the Jaeger container. Gitignored. |
| `data/` | Runtime state, created on first `up` at mode 0700 (re-applied every `up`). Holds `request-logs.db`. Gitignored. |
| `service.toml` | Container definition (above). Tracked. |
| `run.py` | Setup and lifecycle wrapper. Stdlib-only Python, no `pip install`. |
| `checks/auth.sh` | Asserts every route on all three listeners rejects unauthenticated requests. Tracked. |

`data/` is bind-mounted read-write regardless of `-w`, because logging
always needs to write — unlike config edits and cost-catalog refreshes,
which are what `-w` gates.
