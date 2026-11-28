# agentgateway, locally

Runs [agentgateway](https://agentgateway.dev) in a container as a local
LLM proxy in front of OpenAI, Anthropic and Google Gemini. Provider keys come from
[`pass`](https://www.passwordstore.org) at container start and are never
written to disk. All three listeners are authenticated: the LLM and MCP
ports take an API key of their own, the admin UI takes basic auth.

Runs under Docker, or Apple's
[`container`](https://github.com/apple/container) CLI — the default on
macOS when it's installed. We need `container` >= 1.2.0.

> ⚠️ 🤖 Made with the help of AI.

## Setup

```sh
uv run agw setup
```

Checks prerequisites and walks you through the five `pass` entries it
needs: your `openai-api-key`, `anthropic-api-key` and `gemini-api-key`
(a [Gemini API](https://aistudio.google.com/apikey) key), plus two local
secrets it offers to generate for you — `agentgateway-api-key` (what
clients present to the gateway) and `agentgateway-ui-password` (the
admin UI login, username `admin`). A sixth, `agentgateway-session-key`
(agentgateway's MCP session-encryption key), is generated without
asking, since nobody ever needs to type it.

Needs [uv](https://docs.astral.sh/uv/). `agw` is a stdlib-only Python
package with no dependencies to install; the first `uv run` creates
`.venv/` and installs it in editable mode, using a Python 3.11+ it finds
on the machine or downloads (`tomllib`, which parses `service.toml`,
needs 3.11). Run the commands from the repo checkout, or from elsewhere
with `uv run --project /path/to/checkout agw …`.

## Run it

```sh
uv run agw up          # reads the keys from pass, starts the container
uv run agw up -w       # same, but config/ is read-write so the UI can save
uv run agw up --jaeger # also start Jaeger and export traces for every request
uv run agw status      # container state (running / not created / ...)
uv run agw logs        # follow logs
uv run agw restart     # re-reads keys from pass, restarts
uv run agw down        # stop and remove the container
uv run agw key         # print the gateway's own API key (for clients)
uv run agw ui-password # print the admin UI's basic-auth password
uv run agw prune       # drop request-log rows older than 30 days
uv run agw sessions    # requests, tokens and cost per client session
uv run agw costs       # refresh config/base-costs.json from agentgateway's catalog
```

Once it's up:

- **Admin UI** — <http://localhost:15000/ui/>, username `admin`, password
  from `uv run agw ui-password`.
- **LLM data plane** — `http://localhost:4000`, OpenAI- and
  Anthropic-compatible (`/v1/chat/completions`, `/v1/messages`).
- **MCP** — `http://localhost:3000` (no targets configured yet).

With `--jaeger`, Jaeger's trace search UI is at
<http://localhost:16686/search>. The switch generates a runtime-only tracing
config and starts Jaeger alongside agentgateway; `down` removes both
containers. Because tracing uses that generated config, `--jaeger` and `-w`
cannot be combined. Only Jaeger's UI is published on the host; its OTLP port is
used directly between the two containers.

## Calling it

Both LLM endpoints (and MCP) want the gateway's own key in an
`Authorization: Bearer` header — including the Anthropic-format ones,
which take it there rather than in `x-api-key`:

```sh
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $(uv run agw key)" \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5-5","messages":[{"role":"user","content":"hi"}]}'
```

That `$(...)` puts the gateway key into curl's arguments, where `ps`
shows it to other local accounts for the life of the request. It's a
deliberate trade for readability, and it doesn't apply to the SDK form
below — see [Security → The gateway key on the command
line](docs/security.md#the-gateway-key-on-the-command-line) for when to
care and the form that avoids it.

For SDKs, point the base URL at the gateway and pass that key as the
SDK's credential — e.g. `OPENAI_BASE_URL=http://localhost:4000/v1` with
`OPENAI_API_KEY=$(uv run agw key)`. For the Anthropic SDKs use the
auth-token option (`ANTHROPIC_AUTH_TOKEN` / `auth_token`), which sends
`Authorization: Bearer`, rather than `ANTHROPIC_API_KEY`, which sends
`x-api-key` and won't authenticate here.

The real provider keys stay in the container; clients never see them.

## Docs

- [Configuration](docs/configuration.md) — annotated `config.yaml` and
  `service.toml`, and the repo layout.
- [Security](docs/security.md) — what each listener requires, where the
  keys live, and what loopback publishing does and doesn't buy you.
- [Admin UI](docs/admin-ui.md) — the UI, `-w` writable mode, and how to
  use it without leaking a key into a tracked file.
- [Backends](docs/backends.md) — Apple `container` vs Docker, and the
  macOS Local Network privacy gotcha.
- [Request logs](docs/request-logs.md) — prompts and completions on
  disk, per-session cost, and pruning them.
- [Troubleshooting](docs/troubleshooting.md).

`checks/auth.sh` asserts that every route on all three listeners rejects
unauthenticated requests (and that correct credentials still work). Run
it against a live gateway after changing any policy.

## License

[MIT](LICENSE).
