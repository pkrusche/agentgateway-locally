# Backends: Apple `container` vs Docker

`run.py` drives either runtime off the same `service.toml`. On macOS it
picks Apple's [`container`](https://github.com/apple/container)
automatically whenever the binary is on `PATH`, otherwise `docker`.
Force one with `--backend {docker,container}` *before* the subcommand:

```sh
./run.py --backend docker up
```

## Running under Apple's `container` CLI

```sh
container system start          # first time only — starts the container VM services
./run.py setup
./run.py up          # or `up -w` for writable config
./run.py status
./run.py logs
./run.py down
```

`container system status` must report `running`; if not, run `container
system start` first.

There's no compose equivalent for `container`, so `run.py` runs
`container run` directly off the same `service.toml` used for `docker`,
with the same env, volumes and read-only-by-default / `-w` read-write
mount split. Three differences are worth knowing:

- **No restart policy** — under either backend. `container` has no
  equivalent of Docker's `restart: unless-stopped`, and this setup
  deliberately doesn't set one under Docker either, so the two behave
  alike and the provider keys don't outlive a reboot in container
  runtime metadata (see [Security → Where the keys
  live](security.md#where-the-keys-live)). A crash or a reboot won't
  bring agentgateway back on its own: rerun `./run.py up`.
- **Always recreated.** `up`/`restart` delete and recreate the container
  rather than reusing it, since `container run --name` errors on a
  leftover stopped container.
- **Loopback publishing doesn't confine anything.** The `127.0.0.1:`
  prefixes in `service.toml` are not a boundary under this backend — see
  [Security → Loopback publishing isn't a
  boundary](security.md#loopback-publishing-isnt-a-boundary).

## Ports accept connections but every request hangs

**If published ports accept connections but every request hangs or
resets ("empty reply from server"), it's very likely not this project's
config — it's macOS's Local Network privacy.**

Since macOS Sequoia, apps need an explicit grant to reach a machine's
local subnet, and that includes `container-runtime-linux` (the container
CLI's runtime handler) reaching the private subnet (`192.168.64.0/24` by
default) its own containers live on. When the grant is missing, `-p`
publishing looks like it's working — the host side binds and accepts
connections fine — but nothing actually forwards, which reads exactly
like [a `container` networking
bug](https://github.com/apple/container/issues/919) until you check
Privacy settings.

Fix: **System Settings → Privacy & Security → Local Network** — enable
access for your terminal app *and* for `container-runtime-linux`, then:

```sh
container system stop && container system start
./run.py up
```

(An earlier version of this project worked around this with a
hand-rolled `socat`/`nc` port relay in `run.py`, before we realized it
was a permission issue rather than a forwarding bug. Not needed once the
grant is in place, so it isn't in the code.)
