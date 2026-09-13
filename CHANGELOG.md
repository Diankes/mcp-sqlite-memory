# Changelog

## v0.2.0 (2026-09-13)

- `get_resume_context` gains `exclude_kinds`, a comma-separated list of event kinds to hide,
  so hook-written raw events (prompts, compaction and session markers) can be kept out of the
  resume summary.
- `get_resume_context` now drops the oldest events when they overflow the byte cap, and says how
  many it dropped. Previously the newest were the ones lost.
- `search_text` marks newlines inside a value with `⏎` instead of a backslash-n, which collided
  with LaTeX commands such as `\nabla` and `\neq` in both the search and the snippet.
- README: recommended read caps for long-form notes; VS Code setup section.

## v0.1.0 (2026-09-13)

First release: twelve tools, authorizer-enforced statement policy, hash-chained memory,
automatic audit log, timeouts, result caps, snapshots before destructive statements.
