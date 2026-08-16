# agentgateway, locally

Runs [agentgateway](https://agentgateway.dev) in a container as a local
LLM proxy in front of OpenAI and Anthropic. Provider keys come from
[`pass`](https://www.passwordstore.org) at container start and are never
written to disk. All three listeners are authenticated: the LLM and MCP
ports take an API key of their own, the admin UI takes basic auth.

Runs under Docker, or Apple's
[`container`](https://github.com/apple/container) CLI — the default on
macOS when it's installed.

> ⚠️ 🤖 Made with the help of AI.

## Setup

```sh
./run.py setup
```

Checks prerequisites and walks you through the four `pass` entries it
needs: your `openai-api-key` and `anthropic-api-key`, plus two local
secrets it offers to generate for you — `agentgateway-api-key` (what
clients present to the gateway) and `agentgateway-ui-password` (the
admin UI login, username `admin`).

Needs Python 3.11+; no `pip install`, `run.py` is stdlib-only.

On many macOS machines the `python3` on `PATH` is Apple's 3.9, which is
too old for the stdlib `tomllib` `run.py` parses `service.toml` with — so
`./run.py …` exits with a version error and every example below needs an
explicit interpreter instead:

```sh
python3.12 run.py setup    # or python3.13, 3.14, ...
```

## Run it

```sh
./run.py up          # reads the keys from pass, starts the container
./run.py up -w       # same, but config/ is read-write so the UI can save
./run.py up --jaeger # also start Jaeger and export traces for every request
./run.py status      # container state (running / not created / ...)
./run.py logs        # follow logs
./run.py restart     # re-reads keys from pass, restarts
./run.py down        # stop and remove the container
./run.py key         # print the gateway's own API key (for clients)
./run.py ui-password # print the admin UI's basic-auth password
./run.py prune       # drop request-log rows older than 30 days
```

Once it's up:

- **Admin UI** — <http://localhost:15000/ui/>, username `admin`, password
  from `./run.py ui-password`.
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
  -H "Authorization: Bearer $(./run.py key)" \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}]}'
```

That `$(...)` puts the gateway key into curl's arguments, where `ps`
shows it to other local accounts for the life of the request. It's a
deliberate trade for readability, and it doesn't apply to the SDK form
below — see [Security → The gateway key on the command
line](docs/security.md#the-gateway-key-on-the-command-line) for when to
care and the form that avoids it.

For SDKs, point the base URL at the gateway and pass that key as the
SDK's credential — e.g. `OPENAI_BASE_URL=http://localhost:4000/v1` with
`OPENAI_API_KEY=$(./run.py key)`. For the Anthropic SDKs use the
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
  disk, and pruning them.
- [Troubleshooting](docs/troubleshooting.md).

`checks/auth.sh` asserts that every route on all three listeners rejects
unauthenticated requests (and that correct credentials still work). Run
it against a live gateway after changing any policy.

## License

[MIT](LICENSE).
