# Troubleshooting

## The container doesn't stay up

`./run.py up` detects this rather than printing a false "is up", and
dumps the last 50 log lines itself. You can also pull logs any time:

```sh
./run.py logs
```

For more detail, raise agentgateway's own log level with `RUST_LOG`
(Rust `env_logger` syntax — `trace`, `debug`, `info`, `warn`, `error`,
or a per-module filter like `agentgateway=debug`):

```sh
RUST_LOG=debug ./run.py up
RUST_LOG=debug ./run.py restart
```

## Ports accept connections but requests hang

Under Apple's `container` backend on macOS this is almost always Local
Network privacy, not this project's config — see
[Backends](backends.md#ports-accept-connections-but-every-request-hangs).

## `run.py` exits complaining about the Python version

`run.py` parses `service.toml` with the standard-library `tomllib`
module, which needs Python 3.11+ — stock macOS/Xcode Command Line Tools
`python3` is often older. The script uses a plain `#!/usr/bin/env
python3` shebang and checks the version at startup, so it exits with a
clear error naming the interpreter and version it found, rather than a
raw `ModuleNotFoundError`.

Install a newer Python (Homebrew, MacPorts, pyenv) and either put it
first on `PATH` or invoke it directly:

```sh
python3.12 run.py up
```

(An earlier version used a shebang that tried to auto-detect a
new-enough interpreter. It got mangled by an autoformatter and, because
a failed `exec` doesn't stop `sh` from continuing, `sh` fell through to
interpreting the rest of the Python file as a shell script —
backtick-quoted inline code like `` `./run.py` `` in the docstrings was
read as command substitution, which recursively re-ran the script and
forked hundreds of processes. Not worth the convenience.)

## The UI rejects my password

`config/htpasswd` is regenerated from `pass` on every `up`, so a stale
file isn't usually the cause — but a hand-edited one would be
overwritten anyway. Check you're using the username `admin` (or whatever
`GATEWAY_UI_USER` is set to) and the current output of `./run.py
ui-password`, then `./run.py restart` to re-hash it.

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
credentials` in `./run.py logs`. A UI save in `-w` mode is one way to
end up back in that state.

Note that the htpasswd file is read at startup only — editing it (or
regenerating it) needs a `./run.py restart` to take effect. Touching
`config/config.yaml` isn't enough.

## `git diff config/` is enormous after using the UI

A UI save in `-w` mode strips every comment from `config/config.yaml`
and reformats it. See [Admin UI → Treat `-w` as a transient
mode](admin-ui.md#treat--w-as-a-transient-mode-not-a-default).
