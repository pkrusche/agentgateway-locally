#!/usr/bin/env python3
"""Setup and lifecycle wrapper for the agentgateway container.

Reads the OpenAI and Anthropic API keys from `pass` and passes them
into the container just long enough to start it: config/config.yaml
only contains $VAR_NAME placeholders that agentgateway resolves from
the container's environment at runtime, so this setup writes no key to
disk itself. The provider keys do live in the container's runtime
metadata for as long as the container exists, though — `docker inspect
agentgateway` / `container inspect agentgateway` will print them, and
`run.py down` is what clears that.

The gateway's own API key — the one clients send to agentgateway, also
kept in `pass` — is the exception: only its SHA-256 is handed to the
container, so that one isn't recoverable from the runtime at all. The
admin UI's basic-auth password (a fourth `pass` entry) works the same
way: only its bcrypt hash is written, to config/htpasswd.

The container itself (image, ports, env, volumes) is described once in
service.toml, not duplicated here. Runs via `docker`, or Apple's
`container` CLI (https://github.com/apple/container) with
--backend container — on macOS that's the default whenever the
`container` binary is installed, otherwise `docker` is used.

Stdlib only — no pip install required. Needs Python 3.11+ for tomllib
(see the version check right below) — if the `python3` on PATH is
older, run this with a newer interpreter directly, e.g.
`python3.12 run.py ...`.
"""

import argparse
import hashlib
import json
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which

if sys.version_info < (3, 11):
    sys.exit(
        f"error: run.py needs Python 3.11+ for tomllib (found {platform.python_version()} "
        f"at {sys.executable}). Install a newer python3 (e.g. via Homebrew or pyenv) and "
        "either put it first on PATH or invoke it directly, e.g. `python3.12 run.py ...`."
    )
import tomllib

PROG = os.path.basename(sys.argv[0])

SCRIPT_DIR = Path(__file__).resolve().parent
SERVICE_FILE = SCRIPT_DIR / "service.toml"
JAEGER_NAME = "agentgateway-jaeger"
JAEGER_IMAGE = "jaegertracing/all-in-one:latest"
JAEGER_CONFIG_FILE = "config-jaeger.yaml"

OPENAI_PASS_ENTRY = os.environ.get("OPENAI_PASS_ENTRY", "openai-api-key")
ANTHROPIC_PASS_ENTRY = os.environ.get("ANTHROPIC_PASS_ENTRY", "anthropic-api-key")
# The gateway's own API key: what clients send to agentgateway, as
# opposed to the provider keys agentgateway sends upstream.
GATEWAY_PASS_ENTRY = os.environ.get("GATEWAY_PASS_ENTRY", "agentgateway-api-key")
# The admin UI's basic-auth password. Separate from the API key above
# because it's used by a browser, not by code.
UI_PASS_ENTRY = os.environ.get("UI_PASS_ENTRY", "agentgateway-ui-password")
UI_USER = os.environ.get("GATEWAY_UI_USER", "admin")

DEFAULT_PRUNE_DAYS = 30
# How long `up` waits for the container to reach "running" before
# calling it a failed start, and how often it re-checks meanwhile.
STARTUP_TIMEOUT = 15.0
STARTUP_POLL_INTERVAL = 0.25
# Cost 12 rather than htpasswd's default of 5 — this runs once per `up`,
# so a few hundred milliseconds is free, and the hash sits in a file.
BCRYPT_COST = 12


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_service():
    if not SERVICE_FILE.exists():
        die(f"{SERVICE_FILE} not found")
    with SERVICE_FILE.open("rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            die(f"{SERVICE_FILE}: {e}")


def resolve_volume(raw, readonly=False):
    """'./config:/config' -> '<abs-path>/config:/config[:ro]'."""
    host, _, container = raw.partition(":")
    if host == ".":
        host = str(SCRIPT_DIR)
    elif host.startswith("./"):
        host = str(SCRIPT_DIR / host[2:])
    spec = f"{host}:{container}"
    return f"{spec}:ro" if readonly else spec


def host_path(raw):
    """'./data' -> '<script-dir>/data'; absolute paths pass through."""
    if raw == ".":
        return SCRIPT_DIR
    if raw.startswith("./"):
        return SCRIPT_DIR / raw[2:]
    return Path(raw)


def volume_host_path(raw):
    """Host side of a 'host:container' volume spec, resolved."""
    return host_path(raw.partition(":")[0])


# --- pass (password-store) integration ---------------------------------


def require_pass_cli():
    """Fail with an explanation rather than a FileNotFoundError traceback.

    Called from pass_show/pass_has rather than from each command, so a
    new command that reads pass can't forget the check.
    """
    if which("pass") is None:
        die("pass not found (https://www.passwordstore.org)")


def pass_show(entry):
    require_pass_cli()
    r = subprocess.run(
        ["pass", "show", entry],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(f"'pass show {entry}' failed — run '{PROG} setup' first")
    # pass's convention is that the secret is the first line, so only
    # that line is considered. Strip it: a stray trailing space in a
    # stored key turns into an upstream 401 with nothing in the local
    # output to explain it. An entry with no first line at all would
    # otherwise be an IndexError.
    lines = r.stdout.splitlines()
    secret = lines[0].strip() if lines else ""
    if not secret:
        die(f"pass entry '{entry}' is empty — fix it with: pass edit {entry}")
    return secret


def pass_has(entry):
    require_pass_cli()
    check = subprocess.run(
        ["pass", "show", entry],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return check.returncode == 0


def read_reply(entry):
    """input(), but a clean message instead of an EOFError traceback.

    These prompts are reached by non-interactive callers too — a check
    script, a cron'd `up` — where stdin is closed and there's no answer
    to be had.
    """
    try:
        return input()
    except EOFError:
        die(
            f"no pass entry at '{entry}' and stdin isn't a terminal, so it "
            f"can't be created here — run '{PROG} setup' interactively, or: "
            f"pass insert {entry}"
        )


def require_pass_entry(entry, label):
    if pass_has(entry):
        return
    print(f"No pass entry at '{entry}' for your {label}.")
    print(f"Store it now with 'pass insert {entry}'? [y/N] ", end="", flush=True)
    if read_reply(entry).strip().lower().startswith("y"):
        subprocess.run(["pass", "insert", entry], check=True)
    else:
        die(f"aborting — create it yourself with: pass insert {entry}")


def pass_show_or_dummy(entry, label, dummy):
    """Like pass_show(), but a missing entry gets a dummy value and a
    warning instead of blocking `up`.

    Unlike the gateway key or UI password, a provider key with nothing
    behind it isn't fatal to starting agentgateway — e.g. only one of
    OpenAI/Anthropic might be in use. Requests routed to the
    unconfigured provider will just fail upstream with a 401 instead of
    never being attempted.
    """
    if pass_has(entry):
        return pass_show(entry)
    print(
        f"warning: no pass entry at '{entry}' for your {label} — "
        f"using a dummy key, so requests routed to it will fail upstream. "
        f"Fix with: pass insert {entry}",
        file=sys.stderr,
    )
    return dummy


def require_gateway_key(entry):
    """Ensure a gateway API key exists in pass, offering to generate one.

    Unlike the provider keys there's nothing to copy from a vendor
    dashboard — it's just a local secret shared with whatever clients
    are allowed through the gateway, so we can make it up ourselves.
    """
    if pass_has(entry):
        return
    # All of this goes to stderr (and `pass insert`'s own chatter to
    # /dev/null) so that `$(run.py key)` only ever captures the key.
    print(
        f"No pass entry at '{entry}' for agentgateway's own API key —\n"
        "the key clients must present when they call the gateway.\n"
        "Without it the LLM and MCP listeners can't be authenticated.\n"
        f"Generate one now and store it at '{entry}'? [Y/n] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    if read_reply(entry).strip().lower().startswith("n"):
        die(f"aborting — create it yourself with: pass insert {entry}")
    key = "agw-" + secrets.token_urlsafe(32)
    r = subprocess.run(
        ["pass", "insert", "-m", entry],
        input=key + "\n",
        stdout=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(f"'pass insert -m {entry}' failed")
    print(
        f"Stored a new gateway API key at '{entry}' — '{PROG} key' prints it.",
        file=sys.stderr,
    )


def require_ui_password(entry):
    """Ensure a password for the admin UI's basic auth exists in pass."""
    if pass_has(entry):
        return
    print(
        f"No pass entry at '{entry}' for the admin UI's password —\n"
        f"what you type into the browser prompt (username '{UI_USER}').\n"
        f"Generate one now and store it at '{entry}'? [Y/n] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    if read_reply(entry).strip().lower().startswith("n"):
        die(f"aborting — create it yourself with: pass insert {entry}")
    password = secrets.token_urlsafe(24)
    r = subprocess.run(
        ["pass", "insert", "-m", entry],
        input=password + "\n",
        stdout=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(f"'pass insert -m {entry}' failed")
    print(
        f"Stored a new admin UI password at '{entry}' — "
        f"'{PROG} ui-password' prints it.",
        file=sys.stderr,
    )


def write_htpasswd(path, user, password):
    """Write an htpasswd file for agentgateway's basicAuth policy.

    Shells out to `htpasswd` (ships with macOS at /usr/sbin/htpasswd)
    rather than hashing here, because bcrypt isn't in the stdlib and
    this script takes no dependencies. `-i` reads the password from
    stdin: `-b` would put it on the command line, where any local
    process could read it out of `ps` — the same reason env_args()
    passes the provider keys by name only.
    """
    if which("htpasswd") is None:
        die(
            "htpasswd not found — it's needed to hash the admin UI password. "
            "macOS ships it at /usr/sbin/htpasswd (check that /usr/sbin is on "
            "PATH); elsewhere install apache2-utils."
        )
    r = subprocess.run(
        ["htpasswd", "-niB", "-C", str(BCRYPT_COST), user],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    line = r.stdout.strip()
    if r.returncode != 0 or ":" not in line:
        die(f"'htpasswd' failed to hash the UI password:\n{r.stderr.strip()}")
    # Create it 0600 up front rather than writing and then chmod'ing,
    # which would leave the hash briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(line + "\n")
    os.chmod(path, 0o600)


def gateway_key_hash(key):
    """agentgateway's `keyHash` form: 'sha256:<hex>'."""
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def env_args(spec, flag):
    """Build the -e/--env arguments from service.toml's env list.

    Bare NAME entries become just [-e, 'NAME'], which both `docker run`
    and `container run` read as "inherit this variable from my own
    environment" — that keeps API keys off the command line, where any
    other local process could read them out of `ps` while the container
    starts. They must therefore already be in os.environ (the caller is
    responsible for populating them from pass first). NAME=default
    entries are non-secret, so they're passed through as literal values.
    """
    args = []
    for entry in spec["env"]:
        name, sep, default = entry.partition("=")
        if not sep:
            if name not in os.environ:
                die(f"service.toml lists env '{name}', but it isn't set")
            args += [flag, name]
        else:
            args += [flag, f"{name}={os.environ.get(name, default)}"]
    return args


# --- backends -------------------------------------------------------------


class DockerBackend:
    label = "docker"

    def check_prereqs(self):
        if not which("docker"):
            die("docker not found")
        r = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            die(f"docker daemon unreachable:\n{r.stderr.strip()}")

    def _exists(self, name):
        return bool(self.state(name))

    def state(self, name):
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else ""

    def start_jaeger(self):
        if self._exists(JAEGER_NAME):
            self.down(JAEGER_NAME)
        r = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                JAEGER_NAME,
                "-p",
                "127.0.0.1:16686:16686",
                "-e",
                "COLLECTOR_OTLP_ENABLED=true",
                JAEGER_IMAGE,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            die(f"'docker run' failed to start Jaeger:\n{r.stdout}")
        state = wait_until_running(self, JAEGER_NAME)
        if state != "running":
            die(f"Jaeger did not stay up (state: {state or 'unknown'})")

    def jaeger_endpoint(self):
        # Reaching "running" doesn't guarantee the network endpoint has
        # been attached yet, so poll rather than inspecting once.
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            r = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    JAEGER_NAME,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if r.returncode != 0:
                die("could not determine Jaeger's container address")
            address = r.stdout.strip()
            if address:
                return f"http://{address}:4317"
            if time.monotonic() >= deadline:
                die("could not determine Jaeger's container address")
            time.sleep(STARTUP_POLL_INTERVAL)

    def up(self, spec, writable, config_file="config.yaml"):
        if self._exists(spec["name"]):
            subprocess.run(
                ["docker", "rm", "-f", spec["name"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            spec["name"],
            "-u",
            f"{os.getuid()}:{os.getgid()}",
        ]
        for p in spec["ports"]:
            cmd += ["-p", p]
        cmd += env_args(spec, "-e")
        cmd += ["-v", resolve_volume(spec["config_volume"], readonly=not writable)]
        for v in spec["volumes"]:
            cmd += ["-v", resolve_volume(v)]
        cmd.append(spec["image"])
        if not writable:
            cmd += (
                spec["command"]
                if config_file == "config.yaml"
                else ["-f", f"/config/{config_file}"]
            )

        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            die(f"'docker run' failed:\n{r.stdout}")

    def down(self, name):
        subprocess.run(
            ["docker", "stop", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["docker", "rm", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def logs_follow(self, name):
        os.execvp("docker", ["docker", "logs", "-f", name])

    def logs_tail(self, name, n=50):
        subprocess.run(["docker", "logs", "--tail", str(n), name], check=False)

    def status_line(self, name):
        return f"{name}: {self.state(name) or 'not created'}"


class ContainerBackend:
    """Apple's `container` CLI (https://github.com/apple/container).

    No compose/recreate concept, and no restart policy either — which is
    why this setup doesn't set one under docker: a crash or reboot won't
    bring agentgateway back on its own under either backend, so `up` is
    always what starts it. `container run --name` also collides with a
    leftover stopped container, so `up` always deletes any existing
    container first rather than reusing it.

    If published ports accept connections but every request just hangs
    or resets, it's very likely not this code: macOS's Local Network
    privacy silently blocks container-runtime-linux (the container
    CLI's runtime handler) from reaching the container's own subnet
    unless it's been granted access. Fix it in System Settings ->
    Privacy & Security -> Local Network by enabling access for your
    terminal app *and* for container-runtime-linux, then `container
    system stop && container system start`.
    """

    label = "container"

    def check_prereqs(self):
        if not which("container"):
            die("'container' CLI not found (https://github.com/apple/container)")
        r = subprocess.run(
            ["container", "system", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode != 0:
            die(
                "container services aren't running — run 'container system start' first"
            )

    def _list(self):
        r = subprocess.run(
            ["container", "list", "-a", "--format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return []
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return []

    def state(self, name):
        for item in self._list():
            if item.get("configuration", {}).get("id") == name:
                return item.get("status", {}).get("state", "")
        return ""

    def start_jaeger(self):
        self.down(JAEGER_NAME)
        r = subprocess.run(
            [
                "container",
                "run",
                "-d",
                "--name",
                JAEGER_NAME,
                "-p",
                "127.0.0.1:16686:16686",
                "-e",
                "COLLECTOR_OTLP_ENABLED=true",
                JAEGER_IMAGE,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            die(f"'container run' failed to start Jaeger:\n{r.stdout}")
        state = wait_until_running(self, JAEGER_NAME)
        if state != "running":
            die(f"Jaeger did not stay up (state: {state or 'unknown'})")

    def jaeger_endpoint(self):
        # The optional host-side DNS domain is not guaranteed to have been
        # configured, so use the address assigned on the shared default
        # network instead — reported under status.networks (configuration
        # .networks only has the requested network name, not an address).
        # Reaching "running" doesn't guarantee that address has been
        # assigned yet, so poll rather than checking once.
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            for item in self._list():
                if item.get("configuration", {}).get("id") != JAEGER_NAME:
                    continue
                for network in item.get("status", {}).get("networks", []):
                    address = network.get("ipv4Address") or network.get(
                        "ipv6Address", ""
                    )
                    address = address.partition("/")[0]
                    if address:
                        if ":" in address:
                            address = f"[{address}]"
                        return f"http://{address}:4317"
            if time.monotonic() >= deadline:
                die("could not determine Jaeger's container address")
            time.sleep(STARTUP_POLL_INTERVAL)

    def up(self, spec, writable, config_file="config.yaml"):
        subprocess.run(
            ["container", "delete", spec["name"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        cmd = [
            "container",
            "run",
            "-d",
            "--name",
            spec["name"],
            "--uid",
            str(os.getuid()),
            "--gid",
            str(os.getgid()),
        ]
        for p in spec["ports"]:
            cmd += ["-p", p]
        cmd += env_args(spec, "-e")
        cmd += ["-v", resolve_volume(spec["config_volume"], readonly=not writable)]
        for v in spec["volumes"]:
            cmd += ["-v", resolve_volume(v)]
        cmd.append(spec["image"])
        if not writable:
            cmd += (
                spec["command"]
                if config_file == "config.yaml"
                else ["-f", f"/config/{config_file}"]
            )

        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            die(f"'container run' failed:\n{r.stdout}")

    def down(self, name):
        subprocess.run(
            ["container", "stop", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["container", "delete", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def logs_follow(self, name):
        os.execvp("container", ["container", "logs", "-f", name])

    def logs_tail(self, name, n=50):
        subprocess.run(["container", "logs", "-n", str(n), name], check=False)

    def status_line(self, name):
        return f"{name}: {self.state(name) or 'not created'}"


def default_backend_name():
    """'container' on macOS if the container CLI is installed, else 'docker'."""
    if platform.system() == "Darwin" and which("container"):
        return "container"
    return "docker"


def get_backend(name):
    if name == "docker":
        return DockerBackend()
    if name == "container":
        return ContainerBackend()
    die(f"unknown backend '{name}' (expected 'docker' or 'container')")


# --- commands ---------------------------------------------------------


def cmd_setup(backend):
    backend.check_prereqs()
    require_pass_entry(OPENAI_PASS_ENTRY, "OpenAI API key")
    require_pass_entry(ANTHROPIC_PASS_ENTRY, "Anthropic API key")
    require_gateway_key(GATEWAY_PASS_ENTRY)
    require_ui_password(UI_PASS_ENTRY)
    print(f"Setup complete. Run '{PROG} up' to start agentgateway.")


def wait_until_running(backend, name):
    """Poll until the container is running, or until it clearly isn't.

    A fixed sleep raced a slow start: `up` would report "did not stay
    up" and exit 1 on a container that was merely still coming up.
    Returns the last state seen, so the caller can report it.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT
    state = ""
    while True:
        state = backend.state(name)
        if state == "running":
            return state
        # Terminal states — the container is gone or has already
        # given up, so there's nothing to wait for.
        if state in ("exited", "dead", "stopped"):
            return state
        if time.monotonic() >= deadline:
            return state
        time.sleep(STARTUP_POLL_INTERVAL)


def write_jaeger_config(config_dir, endpoint):
    source = config_dir / "config.yaml"
    try:
        contents = source.read_text()
    except OSError as e:
        die(f"cannot read {source}: {e}")
    marker = "config:\n"
    if marker not in contents:
        die(f"{source} has no top-level config section")
    tracing = (
        "config:\n"
        "  tracing:\n"
        f"    otlpEndpoint: {endpoint}\n"
        "    randomSampling: true\n"
    )
    path = config_dir / JAEGER_CONFIG_FILE
    path.write_text(contents.replace(marker, tracing, 1))
    return path.name


def cmd_up(backend, spec, writable, jaeger=False):
    if jaeger and writable:
        die(
            "--jaeger cannot be combined with --writable; "
            "Jaeger uses a generated config file"
        )
    backend.check_prereqs()
    require_gateway_key(GATEWAY_PASS_ENTRY)
    require_ui_password(UI_PASS_ENTRY)

    if writable:
        print(
            "Starting in writable mode — config/ is mounted read-write so the admin UI can save."
        )
    config_dir = volume_host_path(spec["config_volume"])
    config_dir.mkdir(parents=True, exist_ok=True)
    # Regenerated on every up, so rotating the password is just
    # `pass edit` + `restart`. config/ is mounted read-only by default,
    # so this has to be written host-side before the container starts.
    write_htpasswd(config_dir / "htpasswd", UI_USER, pass_show(UI_PASS_ENTRY))
    for v in spec["volumes"]:
        data_dir = volume_host_path(v)
        data_dir.mkdir(parents=True, exist_ok=True)
        # Runtime state, not config: the request-log database under here
        # holds full prompts and responses in plaintext. mkdir alone
        # honours the umask (usually 022, i.e. world-readable), so set
        # the mode explicitly — and on every up, in case the directory
        # predates this.
        data_dir.chmod(0o700)

    os.environ["OPENAI_API_KEY"] = pass_show_or_dummy(
        OPENAI_PASS_ENTRY, "OpenAI API key", "sk-dummy-openai-key-not-configured"
    )
    os.environ["ANTHROPIC_API_KEY"] = pass_show_or_dummy(
        ANTHROPIC_PASS_ENTRY, "Anthropic API key", "sk-ant-dummy-key-not-configured"
    )
    # Only the hash crosses into the container: agentgateway's `keyHash`
    # form compares incoming keys against it, so the gateway key itself
    # never shows up in the container's environment.
    os.environ["GATEWAY_API_KEY_SHA256"] = gateway_key_hash(
        pass_show(GATEWAY_PASS_ENTRY)
    )

    config_file = "config.yaml"
    if jaeger:
        backend.start_jaeger()
        try:
            config_file = write_jaeger_config(config_dir, backend.jaeger_endpoint())
        except SystemExit:
            # jaeger_endpoint()/write_jaeger_config() die() on failure —
            # without this, the Jaeger container it just started would be
            # left running with nothing left to point at it.
            backend.down(JAEGER_NAME)
            raise

    backend.up(spec, writable, config_file)

    state = wait_until_running(backend, spec["name"])
    if state != "running":
        print(
            f"error: agentgateway did not stay up (state: {state or 'unknown'}). Last logs:",
            file=sys.stderr,
        )
        backend.logs_tail(spec["name"])
        print(
            f"\nThe container was left in place so the logs above stay readable — "
            f"'{PROG} down' removes it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("agentgateway is up: http://localhost:15000/ui")
    print(
        f"  UI          basic auth, username '{UI_USER}' — '{PROG} ui-password' prints the password."
    )
    print(f"  :4000/:3000 'Authorization: Bearer <key>' — '{PROG} key' prints the key.")
    if jaeger:
        print("  Jaeger      http://localhost:16686/search (all requests sampled)")


def cmd_key():
    require_gateway_key(GATEWAY_PASS_ENTRY)
    print(pass_show(GATEWAY_PASS_ENTRY))


def cmd_ui_password():
    require_ui_password(UI_PASS_ENTRY)
    print(pass_show(UI_PASS_ENTRY))


def cmd_prune(spec, days):
    db = host_path(spec["request_log_db"])
    if not db.exists():
        print(f"{db} doesn't exist yet — nothing to prune.")
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    con = sqlite3.connect(db)
    try:
        try:
            # Payload rows reference request_logs ON DELETE CASCADE,
            # which sqlite only honours with foreign keys switched on.
            con.execute("PRAGMA foreign_keys = ON")
            # Overwrite freed pages instead of just unlinking them, so
            # deleted prompt/response text doesn't stay readable in the
            # file's freelist. Must be set before the DELETE. Some sqlite
            # builds (including the one CPython usually ships) compile
            # SQLITE_SECURE_DELETE on and already behave this way, but
            # that's a property of the interpreter rather than something
            # to rely on, so set it explicitly. Not a substitute for the
            # VACUUM either — see docs/request-logs.md.
            con.execute("PRAGMA secure_delete = ON")
            deleted = con.execute(
                "DELETE FROM request_logs WHERE completed_at < ?", (cutoff,)
            ).rowcount
            # Belt and braces in case the cascade didn't fire.
            con.execute(
                "DELETE FROM request_log_payloads "
                "WHERE log_id NOT IN (SELECT id FROM request_logs)"
            )
            con.commit()
        except sqlite3.DatabaseError as e:
            die(f"{db}: {e}")
        try:
            con.execute("VACUUM")
        except sqlite3.DatabaseError:
            # A running gateway holds the database open; the rows are
            # gone either way, the file just doesn't shrink yet.
            print("(skipped VACUUM — database busy; the rows are deleted regardless)")
    finally:
        con.close()

    print(f"Pruned {deleted} request log rows older than {days} days from {db}.")


def cmd_down(backend, spec):
    backend.down(spec["name"])
    backend.down(JAEGER_NAME)


def cmd_restart(backend, spec, writable, jaeger=False):
    cmd_down(backend, spec)
    cmd_up(backend, spec, writable, jaeger)


def cmd_logs(backend, spec):
    backend.check_prereqs()
    backend.logs_follow(spec["name"])


def cmd_status(backend, spec):
    backend.check_prereqs()
    print(backend.status_line(spec["name"]))
    if backend.state(JAEGER_NAME):
        print(backend.status_line(JAEGER_NAME))


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Setup and lifecycle wrapper for the agentgateway container.",
        epilog=(
            "Env overrides:\n"
            f"  OPENAI_PASS_ENTRY      pass path for the OpenAI key      (default: {OPENAI_PASS_ENTRY})\n"
            f"  ANTHROPIC_PASS_ENTRY   pass path for the Anthropic key   (default: {ANTHROPIC_PASS_ENTRY})\n"
            f"  GATEWAY_PASS_ENTRY     pass path for the gateway's key   (default: {GATEWAY_PASS_ENTRY})\n"
            f"  UI_PASS_ENTRY          pass path for the UI password     (default: {UI_PASS_ENTRY})\n"
            f"  GATEWAY_UI_USER        username for the UI's basic auth  (default: {UI_USER})\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backend",
        choices=["docker", "container"],
        default=None,
        help=(
            "Container runtime to use. Default: 'container' on macOS if "
            "the container CLI is installed (run 'container system start' "
            "first), otherwise 'docker'. Must come before the subcommand."
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "setup",
        help=(
            "Check prerequisites and create any missing pass entries: the "
            "OpenAI/Anthropic keys, plus agentgateway's own API key."
        ),
    )

    up_p = sub.add_parser(
        "up", help="Read keys from pass and start agentgateway (detached)."
    )
    up_p.add_argument(
        "-w",
        "--writable",
        action="store_true",
        help="Mount config/ read-write so the admin UI can save edits.",
    )
    up_p.add_argument(
        "--jaeger",
        action="store_true",
        help="Start Jaeger and export all agentgateway traces to it.",
    )

    sub.add_parser("down", help="Stop and remove the container.")

    restart_p = sub.add_parser(
        "restart", help="Down, then up again (re-reads keys from pass)."
    )
    restart_p.add_argument(
        "-w",
        "--writable",
        action="store_true",
        help="Mount config/ read-write so the admin UI can save edits.",
    )
    restart_p.add_argument(
        "--jaeger",
        action="store_true",
        help="Start Jaeger and export all agentgateway traces to it.",
    )

    sub.add_parser("logs", help="Follow container logs.")
    sub.add_parser("status", help="Show container status.")

    sub.add_parser(
        "key",
        help=(
            "Print agentgateway's own API key — what clients send as "
            "'Authorization: Bearer <key>' (generating it if missing)."
        ),
    )

    sub.add_parser(
        "ui-password",
        help=(
            f"Print the admin UI's basic-auth password (username "
            f"'{UI_USER}'), generating it if missing."
        ),
    )

    prune_p = sub.add_parser(
        "prune",
        help="Delete old rows from the request-log database (prompts and responses).",
    )
    prune_p.add_argument(
        "--days",
        type=int,
        default=DEFAULT_PRUNE_DAYS,
        help=f"Keep entries newer than this many days (default: {DEFAULT_PRUNE_DAYS}).",
    )

    return parser


def main(argv):
    args = build_parser().parse_args(argv)

    backend_name = args.backend or default_backend_name()
    backend = get_backend(backend_name)
    spec = load_service()
    writable = getattr(args, "writable", False)
    jaeger = getattr(args, "jaeger", False)

    if args.command == "setup":
        cmd_setup(backend)
    elif args.command == "up":
        cmd_up(backend, spec, writable, jaeger)
    elif args.command == "down":
        backend.check_prereqs()
        cmd_down(backend, spec)
    elif args.command == "restart":
        backend.check_prereqs()
        cmd_restart(backend, spec, writable, jaeger)
    elif args.command == "logs":
        cmd_logs(backend, spec)
    elif args.command == "status":
        cmd_status(backend, spec)
    elif args.command == "key":
        cmd_key()
    elif args.command == "ui-password":
        cmd_ui_password()
    elif args.command == "prune":
        cmd_prune(spec, args.days)


if __name__ == "__main__":
    main(sys.argv[1:])
