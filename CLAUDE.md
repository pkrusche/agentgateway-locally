# Notes for Claude

## Don't add comments to `config/config.yaml`

`-w` (writable) mode is a feature we use, not an edge case. When
agentgateway saves config from the admin UI it round-trips the file
through a YAML parser, which strips every comment — so any explanation
put there is lost the first time the UI saves, and shows up as noise in
the next `git diff`.

Annotate `docs/configuration.md` instead; it's structured to mirror the
config field by field. The only comment `config/config.yaml` carries is
the header pointing there (and the `yaml-language-server` schema line,
which is load-bearing for editor validation).

This does not apply to `service.toml` — nothing rewrites that file, so
comments there are fine and are where the container setup is explained.
