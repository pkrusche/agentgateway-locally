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
`$ANTHROPIC_API_KEY` / `$GEMINI_API_KEY` placeholders, never resolved values. See
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

Points at `/config/base-costs.json`, per-model input/output/cache token
rates layered over the catalog built into agentgateway (since 1.6), with
later entries taking precedence. The built-in catalog already prices
common public models, so this file is an overlay: it's what a cost-catalog
refresh from the UI writes to, and it needs a path under `/config` for
that refresh to be saveable in `-w` mode.

That file is **not in the repo** — it's generated data that the UI
rewrites in full on every refresh, so tracking it meant 174KB of churn
per refresh. A fresh clone therefore starts without it, which is
harmless: agentgateway logs

```
warn llm::cost  model catalog load failed; will load when the files
                become valid: ... missing files: /config/base-costs.json
```

and serves normally, pricing requests from its built-in catalog. To
populate it, run `uv run agw costs`. It downloads
[agentgateway's upstream `catalog/model-catalog.json`](https://github.com/agentgateway/agentgateway/blob/main/catalog/model-catalog.json)
into `config/model-catalog.json` (also gitignored) and writes
`base-costs.json` from it. Only each model's `rates` and its
`contextOver` tiers are kept. `cacheWriteDetails`, `perAudioMinute` and
service-tier tiers (`fast`, `flex`, `priority`, …) are dropped, as are
names, limits and capabilities. `--from FILE` uses a local catalog
instead of downloading. No container or `-w` is needed: the command
writes on the host.

Refreshing from the admin UI does the same job in principle, but
agentgateway 1.7.0's refresh can't read the current upstream catalog,
whose schema has gained fields since. Either way the file is watched, so
it's picked up as soon as it changes — no restart needed.

### `config.standardAttributes.session`

```yaml
session: 'default(request.headers["x-project-sandbox-session-id"], request.agent.session)'
```

Tags each request with the client session it belongs to, so cost and
traces can be followed per session — `uv run agw sessions`, see
[Request logs → Per-session usage](request-logs.md#per-session-usage).
agentgateway stores the value as `agentgateway.session` in each
request-log row's `attributes_json`, and exposes it to CEL as
`request.agent.session`.

[project-sandbox](https://github.com/pkrusche/project-sandbox) sends
its own session ID in `x-project-sandbox-session-id`. Without this
field, agentgateway would still pick that header up — it auto-detects
any `x-<vendor>-session-id` header, plus `x-session-id` and Codex's
`session_id`. But when a request carries two of them — the sandbox's
and a sandboxed agent's own conversation header — it takes whichever
comes first. So the request would land in the agent's conversation rather than the
sandbox session, depending on header order. The expression makes the
sandbox header win, and falls back to that auto-detection
(`request.agent.session`) for every other client. A request with
neither gets no session.

Two forms that look equivalent don't work in 1.7.0:
`default(…, "")` turns the auto-detection off entirely, and
`default(…, default(request.agent.session, ""))` loses the fallback
too. Checked against the pinned image with test requests, not just
`--validate-only`.

The header is attribution, not authentication: any client holding the
gateway key can send any value.

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

Three upstream providers (`openai`, `anthropic`, `gemini`) resolving
their credentials from `$OPENAI_API_KEY` / `$ANTHROPIC_API_KEY` /
`$GEMINI_API_KEY`, and the
client-facing model names that map onto them. A client asks for
`claude-sonnet-5-5` or `gpt-6-sol` and agentgateway routes it to the
right provider with the right upstream key.

Each model can also define request defaults beside `params`. These are
request-body fields, rather than upstream connection settings. The
OpenAI models have no gateway reasoning defaults, so the same names work
with both `/v1/chat/completions` and `/v1/responses`. Without a
client-supplied effort, OpenAI uses the model's default.

To request high effort, set the field in the client request for its API:

- Chat Completions: `"reasoning_effort": "high"`.
- Responses: `"reasoning": {"effort": "high"}`.

For pi's Responses client, set `api: "openai-responses"` and
`baseUrl: "http://localhost:4000/v1"`.

Gateway defaults are injected as request-body fields, not translated
between these APIs. Adding `defaults.reasoning` to a shared OpenAI model
makes Chat Completions fail with `Unknown parameter: 'reasoning'`;
adding `defaults.reasoning_effort` instead breaks Responses. Do not set
both field formats on the same model. If gateway-supplied effort is
needed, use separate model aliases dedicated to each API.

The Claude models use Anthropic's adaptive-thinking fields instead:

```yaml
defaults:
  thinking:
    type: adaptive
  output_config:
    effort: high
```

The Gemini models go to the Gemini API (Google AI Studio,
`generativelanguage.googleapis.com`) with the key from the
`gemini-api-key` `pass` entry — not Vertex AI, which would be a separate
`vertex` provider with Google Cloud credentials instead of an API key.
agentgateway talks to Gemini through its OpenAI-compatible endpoint, so
call these models via `/v1/chat/completions`; since agentgateway 1.5,
Gemini SDKs can also use the native `generateContent` API.

The configured set is the Gemini 4 family (`gemini-4-argon`, `-carbon`,
`-hydrogen`, `-neon`), all thinking models. They carry no `defaults`, so
thinking runs at Google's default level; a client that wants a specific
level can send Chat Completions' `reasoning_effort` itself. To see what
else the key can reach, list `GET /v1beta/models` on the Gemini API with
the key in an `x-goog-api-key` header, and add entries the same way.
Models newer than `base-costs.json` show no cost on the Logs page until
the catalog is refreshed (or agentgateway's built-in catalog covers
them).

These are defaults, so a client-provided value takes precedence. Use a
model's `overrides` map when the gateway must force a value on every
request. Default field names must match the upstream request API; do not
assume agentgateway translates fields added by these defaults.

### `mcp`

Published on `:3000` with the same `apiKey` policy as the LLM listener.
`targets` is empty today — the policy goes in ahead of the first target,
since tool calls are a bigger blast radius than chat proxying, and it's
easier to add a target to an authenticated listener than to remember to
lock one down afterwards.

MCP session IDs are encrypted with the key in `$SESSION_KEY`, which
`uv run agw up` takes from the `agentgateway-session-key` entry in `pass`
(generating it on first use). It's set through the environment rather
than as `config.session.key` here on purpose: with no key configured,
agentgateway (since 1.7) generates one and writes it into this file
whenever the file is writable — i.e. under `-w`, putting a secret into a
tracked file — and otherwise runs with unencrypted sessions.

### `gateways.ui` and `ui`

The `ui` gateway exists purely to give the admin UI a listener that
policies can attach to, on the same port 15000 the admin address used to
use — so nothing changes for `service.toml` or for bookmarks. The
top-level `ui` section attaches the UI to that gateway and applies
`basicAuth` to all UI traffic.

`/config/htpasswd` is written by `uv run agw up` from the
`agentgateway-ui-password` entry in `pass`. Full reasoning in
[Security → The admin UI](security.md#the-admin-ui-basic-auth).

## `service.toml`

The single definition of the container — image, ports, env, volumes,
command. It replaced a duplicated
`docker-compose.yml`/`docker-compose.rw.yml` pair plus hardcoded bits of
the run script; `agw` reads it with the stdlib `tomllib` and builds
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
  (`-e NAME`), so values are inherited from `agw`'s environment
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
| `config/base-costs.json` | Model cost catalog — pricing data, no secrets. Gitignored: it's generated, large, and rewritten wholesale by each `agw costs` (or UI refresh). |
| `config/model-catalog.json` | agentgateway's upstream model catalog, downloaded by `agw costs`. Gitignored. |
| `config/htpasswd` | Admin UI's bcrypt basic-auth credentials, rewritten by every `up`, mode 0600. Gitignored; never edit by hand. |
| `config/config-jaeger.yaml` | Runtime-only copy generated by `up --jaeger`, with tracing pointed at the Jaeger container and each request span tagged with `agentgateway.session`. Gitignored. |
| `data/` | Runtime state, created on first `up` at mode 0700 (re-applied every `up`). Holds `request-logs.db`. Gitignored. |
| `service.toml` | Container definition (above). Tracked. |
| `pyproject.toml`, `uv.lock` | Makes the repo a uv project whose one package, `agw`, provides the `agw` command. No dependencies. Tracked. |
| `src/agw/cli.py` | The `agw` setup and lifecycle wrapper (`uv run agw …`). Stdlib-only Python. |
| `tests/` | Unit tests: `uv run python -m unittest discover -s tests`. |
| `.venv/` | Created by `uv run`. Gitignored. |
| `checks/auth.sh` | Asserts every route on all three listeners rejects unauthenticated requests. Tracked. |

`data/` is bind-mounted read-write regardless of `-w`, because logging
always needs to write — unlike config edits and cost-catalog refreshes,
which are what `-w` gates.
