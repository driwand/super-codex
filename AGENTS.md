# Super Codex project rules

- Keep the runtime dependency-free and compatible with Python 3.9 or newer.
- Support macOS and Linux; do not claim Windows support without integration coverage.
- Codex is the default agent; Claude is the explicit fallback and reviewer.
- Never read, copy, decode, print, export, or swap provider credential files.
- Isolate accounts through provider home-directory environment variables and document unstable interfaces honestly.
- Never add automatic dangerous or permission-bypass flags to delegated commands.
- Keep configuration schema-versioned, private, symlink-resistant, and atomically written.
- Do not implement automatic account rotation for bypassing provider limits.
- Run `python3 -m unittest discover -s tests -v` after code changes.
- Run `python3 -m compileall -q src tests` to catch syntax errors.
- Verify both installed entry points when packaging changes: `sc --version` and `super-codex --version`.
