# Request logs

`data/request-logs.db` is an ordinary unencrypted sqlite file. It holds
one row per proxied request — model, tokens, cost, status — and, for
requests where agentgateway captured payloads, the prompt and the
completion in full.

`agw` keeps `data/` at mode 0700 and the file is gitignored, but
nothing prunes it on its own, so months of conversation content will
pile up if you let it. It's also what the admin UI's Logs page reads,
which is part of why that UI is [behind basic
auth](security.md#the-admin-ui-basic-auth).

```sh
uv run agw prune             # delete rows older than 30 days
uv run agw prune --days 7    # or pick your own window
```

Payload rows go with their request row (foreign key cascade), and the
command is safe to run while the gateway is up — it just skips the
`VACUUM` if the database is busy. Worth putting on a cron/launchd timer
if you use the gateway regularly.

## Per-session usage

Requests that carry a session ID — from the
`x-project-sandbox-session-id` header that project-sandbox sends, or a
session header agentgateway detects itself — are tagged with it as
`agentgateway.session` (see [Configuration →
`config.standardAttributes.session`](configuration.md#configstandardattributessession)).

```sh
uv run agw sessions                 # one line per session, most recent first
uv run agw sessions --days 7        # only requests from the last week
uv run agw sessions <session-id>    # that session, broken down by model
```

Each line shows request and error counts, input/output tokens and cost.
A cost marked `*` is a lower bound: some of its successful requests
used a model the cost catalog doesn't price yet (`uv run agw costs`).
The command opens the database read-only, so it's safe while the
gateway is running. Rows with no session ID aren't counted, and `prune`
removes session history along with everything else.

The same breakdown is available directly in sqlite:

```sql
SELECT json_extract(attributes_json, '$."agentgateway.session"') AS session,
       COUNT(*), SUM(cost)
FROM request_logs GROUP BY session;
```

Under `up --jaeger`, request spans carry the same `agentgateway.session`
attribute, so a session's traces are a tag search in Jaeger
(`agentgateway.session=<id>`). agentgateway also adopts a trace ID
from an incoming W3C `traceparent` header, so a client that sends one
fixed `traceparent` per session gets all of that session's requests in
a single trace.

## Pruning is retention, not erasure

`prune` sets `PRAGMA secure_delete = ON` before deleting, so pages it
frees are overwritten rather than merely unlinked. (Many sqlite builds,
including the one CPython typically ships, already do this by default —
the pragma makes it independent of which interpreter uv runs `agw`
with.)

That is not the whole story, though: because the `VACUUM` is skipped
while the gateway holds the database open — the common case — deleted
content can still remain in pages sqlite doesn't free on this
connection, and in the `-wal` sidecar file, until something reuses that
space.

If the point is to actually get the content off disk, stop the container
first so the `VACUUM` can run:

```sh
uv run agw down
uv run agw prune
uv run agw up
```
