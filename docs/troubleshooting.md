# Troubleshooting

## The container doesn't stay up

`uv run agw up` detects this rather than printing a false "is up", and
dumps the last 50 log lines itself. You can also pull logs any time:

```sh
uv run agw logs
```

For more detail, raise agentgateway's own log level with `RUST_LOG`
(Rust `env_logger` syntax — `trace`, `debug`, `info`, `warn`, `error`,
or a per-module filter like `agentgateway=debug`):

```sh
RUST_LOG=debug uv run agw up
RUST_LOG=debug uv run agw restart
```

## Ports accept connections but requests hang

Under Apple's `container` backend on macOS this is almost always Local
Network privacy, not this project's config — see
[Backends](backends.md#ports-accept-connections-but-every-request-hangs).

## `uv run agw` can't find the project or `service.toml`

`agw` locates `service.toml` — and resolves its `./config` and `./data`
mounts — relative to its own source file in the checkout, which works
because `uv run` installs the project in editable mode. So:

- **"Failed to spawn: `agw`"** — `uv run` was started outside the
  checkout. `cd` into it, or pass `uv run --project /path/to/checkout
  agw …`.
- **"service.toml not found"** — `agw` is installed non-editably (e.g.
  `uv tool install` or `uv pip install .` without `-e`), so it runs from
  a copy in `site-packages`. Use `uv run agw` from the checkout instead.

Python itself is uv's job: `pyproject.toml` requires 3.11+ (for
`tomllib`), and uv picks a matching interpreter or downloads one, so the
old macOS-`python3`-is-3.9 problem doesn't apply any more.

## The UI rejects my password

`config/htpasswd` is regenerated from `pass` on every `up`, so a stale
file isn't usually the cause — but a hand-edited one would be
overwritten anyway. Check you're using the username `admin` (or whatever
`GATEWAY_UI_USER` is set to) and the current output of `uv run agw
ui-password`, then `uv run agw restart` to re-hash it.

If *every* credential is rejected — including a freshly generated one —
check that the policy still reads:

```yaml
      htpasswd:
        file: /config/htpasswd
```

Flattening that to `htpasswd: /config/htpasswd` makes agentgateway treat
the path as the htpasswd file's literal contents. It starts without
complaint and logs nothing unusual; you just get a login prompt that
never accepts anything, with `basic authentication failure: invalid
credentials` in `uv run agw logs`. A UI save in `-w` mode is one way to
end up back in that state.

Note that the htpasswd file is read at startup only — editing it (or
regenerating it) needs a `uv run agw restart` to take effect. Touching
`config/config.yaml` isn't enough.

## `git diff config/` is enormous after using the UI

A UI save in `-w` mode strips every comment from `config/config.yaml`
and reformats it. See [Admin UI → Treat `-w` as a transient
mode](admin-ui.md#treat--w-as-a-transient-mode-not-a-default).
