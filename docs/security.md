# Security

Three listeners, all authenticated:

| Port | What | Auth |
| --- | --- | --- |
| 4000 | LLM data plane (OpenAI- and Anthropic-compatible) | `Authorization: Bearer <gateway key>`, `mode: strict` |
| 3000 | MCP | same key, `mode: strict` (no targets configured yet) |
| 15000 | Admin UI | HTTP basic auth, `mode: strict` (username `admin`) |

Four secrets live in `pass`: the two provider keys, the gateway's own
API key, and the admin UI password. Nothing else on this machine holds
any of them in plaintext.

`checks/auth.sh` is the executable form of the table above — it walks
every listener with a list of real and made-up paths across `GET`,
`POST` and `OPTIONS`, requires 401/403 on all of them, and then proves
the correct credentials *are* accepted, so the negative results can't
pass vacuously against a gateway that's simply broken:

```sh
checks/auth.sh
GATEWAY_HOST=192.168.64.3 checks/auth.sh   # same checks over vmnet
```

Both policies attach to a listener rather than to individual routes, so
there is no route list to keep in sync — which is the property the
script exists to confirm after any policy change.

## The data plane: an API key

The LLM and MCP listeners use agentgateway's `apiKey` policy. Loopback
publishing alone was never enough for those: any local process can reach
`127.0.0.1`, and so can any website you have open, via DNS rebinding
(CORS doesn't stop it — the attacker controls the `Host` header, so the
request is same-origin). Without a key on the data plane that's a free
ride on your paid provider keys.

`config/config.yaml` stores agentgateway's `keyHash` form
(`sha256:<hex>`), resolved from `GATEWAY_API_KEY_SHA256`, so the
gateway's own key isn't recoverable from the config *or* from the
container's environment — only its hash is.

Rotate with `pass edit agentgateway-api-key` (or `pass generate -n
agentgateway-api-key 40`) followed by `./run.py restart`; old clients
stop working immediately.

## The gateway key on the command line

The README's curl example expands the key into curl's arguments:

```sh
curl -H "Authorization: Bearer $(./run.py key)" ...
```

That is a real exposure, and it's worth being precise about which one.
A process's argument vector is readable by other users on the machine
(`ps -ef`), so for as long as that curl runs, the gateway key is visible
to any local account — not just to processes running as you. It does
*not* reach your shell history: history records the literal text you
typed, i.e. the `$(./run.py key)` substitution, not its result. Pasting
a literal key would put it in history; this form doesn't.

This is the same exposure `env_args()` in `run.py` is written to avoid
for the provider keys, which are passed to the container runtime by name
(`-e OPENAI_API_KEY`) rather than as `NAME=value`. So why accept it
here?

Because the two keys aren't worth the same. The provider keys are
issued by a third party, are billed against directly, can't be rotated
without a trip to a vendor dashboard, and are equally valuable to
someone who exfiltrates them off this machine. The gateway key is a
local secret this setup generates itself, is worth nothing away from
this host (the listeners are on loopback), and is rotated in about two
seconds:

```sh
pass generate -n agentgateway-api-key 40 && ./run.py restart
```

It is not worthless, though — it authorizes spending the provider keys
through the gateway. So the honest summary is: on a single-user machine
the `ps` window is a non-issue, since anything running as you can just
run `./run.py key` itself. On a machine with other local accounts, or
one with untrusted local daemons, prefer a form that keeps the key out
of argv. Pipe it to curl as a config file instead:

```sh
printf 'header = "Authorization: Bearer %s"\n' "$(./run.py key)" |
  curl --config - \
    --url http://localhost:4000/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}]}'
```

The key crosses as a pipe between two processes and never appears in
either one's arguments. `checks/auth.sh` uses exactly this form for its
positive controls.

The SDK route has the same property without any ceremony: environment
variables aren't in argv, so `OPENAI_API_KEY=$(./run.py key)` is already
fine as far as `ps` is concerned.

## The admin UI: basic auth

Policies can't attach to agentgateway's admin address, so
`config.adminAddr` is `"off"` and the UI is served from an ordinary
gateway listener instead — agentgateway's documented pattern
(`ui.gateways`), which its docs demo with OIDC but which accepts any
auth policy. Here that's `basicAuth`, because the UI is navigated to in
a browser rather than called from code:

```yaml
gateways:
  ui:
    port: 15000
ui:
  gateways: [ui]
  policies:
    basicAuth:
      mode: strict
      htpasswd:
        file: /config/htpasswd
      realm: agentgateway
```

Note the nested `file:` key. A bare `htpasswd: /config/htpasswd` is
valid YAML and starts cleanly, but agentgateway reads that string as the
htpasswd *contents* — which parse to zero users, so the UI challenges
for credentials and then rejects every one of them. The alternative is
inline contents, where each `$` in the hash has to be doubled to `$$` to
survive agentgateway's variable substitution; the file form avoids that
entirely.

The password lives in `pass` as `agentgateway-ui-password` (generated by
`./run.py setup`, printed by `./run.py ui-password`). `run.py up` hashes
it with `htpasswd -niB` — bcrypt at cost 12, password fed on stdin so it
never reaches `ps` — and writes `config/htpasswd` at mode 0600 on every
start. Only the hash is on disk, and the file is gitignored.

Rotate with `pass edit agentgateway-ui-password` then `./run.py
restart`. Change the username with `GATEWAY_UI_USER`.

This is what makes the two exposures below survivable rather than merely
documented: the UI no longer cares which interface a request arrived on,
so the vmnet address doesn't bypass it, and DNS rebinding doesn't help
an attacker either — browsers scope basic-auth credentials per origin,
and a rebound hostname is a different origin, so yours won't be
replayed. Basic auth over plain HTTP is only reasonable because this is
loopback traffic on a single machine; if you ever widen 15000 to a
Tailscale address, put TLS in front of it.

## Loopback publishing isn't a boundary

Under Apple's `container` runtime every container gets a routable
address on the host-only vmnet subnet (`192.168.64.0/24` by default),
and **all** of its ports are reachable there from any process on the Mac
— including ports you never published, and regardless of the
`127.0.0.1:` prefix in `service.toml`. Verified: with only 4000
published, the admin UI still answered on
`http://192.168.64.x:15000/ui` — back when it was unauthenticated, which
is what prompted the basic auth above. Docker's default bridge network
has the same property between containers.

That's why all three listeners carry their own auth rather than relying
on the bind address.

## Where the keys live

`run.py up` shells out to `pass show`, sets the results as
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in its own environment just long
enough to start the container, then exits. It passes them to the runtime
*by name* (`-e OPENAI_API_KEY`, no value), so the keys never appear on
the `docker run` / `container run` command line where another local
process could read them out of `ps`. agentgateway resolves the `$VAR`
references in `config/config.yaml` from the container's environment at
runtime. Nothing secret lands in a `.env` file or in
`config/config.yaml`, and this repo has never tracked one.

What that does *not* cover: the provider keys do end up in the
container's environment, which is part of its runtime metadata for as
long as the container exists. `docker inspect agentgateway` (or
`container inspect agentgateway`) prints them, and they persist in the
runtime's own state — inside Docker Desktop's VM, or under `container`'s
state directory — until the container is removed. That's inherent to
env-var secrets and is the trade-off this setup accepts; `./run.py down`
is what clears it.

The two local secrets are the exception. Only the gateway key's SHA-256
and the UI password's bcrypt hash ever leave `pass`, so neither is
recoverable from the container at all.

No restart policy is set, deliberately. Docker's `unless-stopped` would
have brought the container back after every reboot, which quietly turns
"until the container is removed" into "indefinitely" — `down` would
almost never run, so the window in which the provider keys sit in
runtime metadata would be permanent rather than the length of a session.
Apple's `container` CLI has no restart-policy equivalent anyway, so
setting one would also have made the two backends differ. Both now
behave the same: `./run.py up` is what starts agentgateway, after a
reboot as at any other time, and `./run.py down` genuinely ends the
exposure.

## See also

- [Admin UI](admin-ui.md) — why `-w` deserves care, and the one way a
  real key can end up in a tracked file.
- [Request logs](request-logs.md) — prompts and completions in plaintext
  on disk, and how to age them out.
- [Docker deployment docs](https://agentgateway.dev/docs/standalone/latest/deployment/docker/)
  — upstream, on the read-only vs read-write mount patterns.
