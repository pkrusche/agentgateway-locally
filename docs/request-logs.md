# Request logs

`data/request-logs.db` is an ordinary unencrypted sqlite file. It holds
one row per proxied request — model, tokens, cost, status — and, for
requests where agentgateway captured payloads, the prompt and the
completion in full.

`run.py` keeps `data/` at mode 0700 and the file is gitignored, but
nothing prunes it on its own, so months of conversation content will
pile up if you let it. It's also what the admin UI's Logs page reads,
which is part of why that UI is [behind basic
auth](security.md#the-admin-ui-basic-auth).

```sh
./run.py prune             # delete rows older than 30 days
./run.py prune --days 7    # or pick your own window
```

Payload rows go with their request row (foreign key cascade), and the
command is safe to run while the gateway is up — it just skips the
`VACUUM` if the database is busy. Worth putting on a cron/launchd timer
if you use the gateway regularly.

## Pruning is retention, not erasure

`prune` sets `PRAGMA secure_delete = ON` before deleting, so pages it
frees are overwritten rather than merely unlinked. (Many sqlite builds,
including the one CPython typically ships, already do this by default —
the pragma makes it independent of which interpreter you run `run.py`
with.)

That is not the whole story, though: because the `VACUUM` is skipped
while the gateway holds the database open — the common case — deleted
content can still remain in pages sqlite doesn't free on this
connection, and in the `-wal` sidecar file, until something reuses that
space.

If the point is to actually get the content off disk, stop the container
first so the `VACUUM` can run:

```sh
./run.py down
./run.py prune
./run.py up
```
