# Admin UI

<http://localhost:15000/ui/>. The browser prompts for username `admin`
and the password from `./run.py ui-password` — see
[Security → The admin UI](security.md#the-admin-ui-basic-auth) for how
that's wired up and how to rotate it.

The UI can view config, browse the request logs, and run requests
through the playground. Note that the playground can't fill the gateway
API key in for you — the config holds only a hash of it — so paste the
output of `./run.py key` into its API-key field.

Saving config edits or refreshing the cost catalog needs `-w`.

## Editing config (or refreshing the cost catalog) from the UI

By default `run.py up` mounts `config/` **read-only** — the admin UI can
view config and the cost catalog, but any attempt to save an edit or
refresh `base-costs.json` fails with "Permission denied" — and that
refresh is the only way to (re)generate the cost catalog, which isn't
tracked in the repo. Pass `-w` (or
`--writable`) to mount `config/` read-write instead, and skip passing
`-f /config/config.yaml` so agentgateway manages the directory itself:

```sh
./run.py up -w
```

That's agentgateway's own documented pattern for UI-editable config. A
plain read-write single-file mount doesn't work, because an in-place
config rewrite needs to replace the file's inode, which a single-file
bind mount can't reflect back to the host.

agentgateway loads the existing `config/config.yaml` from that directory
on start, so your providers/models/CORS policy carry over — but this
project hasn't exhaustively verified that behavior across every
agentgateway version, so check the UI reflects your config on first run.
Either way, `config/config.yaml` on disk is the same file being
read/written: hand edits and UI edits both land there, and `git diff`
shows you what the UI changed.

## Don't type a real API key into the UI

The provider forms will happily accept a literal key, and in `-w` mode
that value is what gets written to `config/config.yaml` — a live
provider key, in plaintext, in a git-tracked file, one `git add -A` away
from being committed.

Saving from the UI is otherwise safe in this respect: it preserves the
`$OPENAI_API_KEY` / `$ANTHROPIC_API_KEY` placeholders as written and
doesn't substitute the resolved values back in. So leave those fields
alone — the keys belong in `pass`, and `run.py` is what gets them to the
gateway. If it happens by accident, rotate the key at the provider;
`git checkout config/config.yaml` only removes it from the working tree,
not from any commit you already made.

## Treat `-w` as a transient mode, not a default

Read-write `config/` plus no `-f` pin means the gateway process can
persist arbitrary config changes: anything that can drive the admin UI
can rewrite `config/config.yaml`. Re-pointing a provider at an
attacker-controlled endpoint would hand it the resolved provider keys on
the next request; weakening or deleting the `apiKey` or `basicAuth`
policy would open the data plane or the UI itself.

A UI save also **strips every comment** from `config/config.yaml` — the
file is round-tripped through a YAML parser. That's why the annotated
walkthrough lives in [docs/configuration.md](configuration.md) instead
of in the file, and why `git diff config/` after a UI save can show a
much larger change than whatever you actually edited.

So start with `-w` only when you intend to edit something, `git diff
config/` afterwards to see exactly what changed, and go back to a plain
`./run.py restart` (read-only, `-f`-pinned) when you're done.
