# Contributing to Super Codex

Contributions are welcome when they preserve the project's narrow purpose: safe, transparent selection of native coding-agent profiles without credential swapping.

## Development setup

Use Python 3.9 or newer. The runtime must remain dependency-free.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
./sc --help
```

To test the installed package without touching normal user state, set a temporary state root:

```bash
SUPER_AGENT_HOME=/tmp/super-codex-test sc status
```

Never use a real credential directory in automated tests. Tests must mock provider protocol responses and native authentication status where appropriate.

## Change guidelines

- Preserve Codex as the default and Claude as an explicit fallback.
- Never read, copy, decode, export, log, or swap provider credential files.
- Never add automatic permission-bypass flags.
- Keep native provider arguments visible and explicit.
- Keep state schema-versioned and writes atomic.
- Add tests for behavior and failure paths.
- Document experimental provider interfaces honestly.
- Do not add a runtime dependency without discussing the security and packaging trade-offs first.
- Do not implement automatic account rotation for bypassing provider limits.

See [AGENTS.md](AGENTS.md) for the repository's agent-specific working rules and [SECURITY.md](SECURITY.md) for security reports.

## Pull requests

Keep pull requests focused. Include:

- the user problem being solved;
- security or compatibility implications;
- tests added or updated;
- exact verification commands and results;
- documentation changes for user-visible behavior.

## Releases

Releases use annotated Semantic Versioning tags and GitHub Releases. They are not
published to PyPI. Before tagging from `main`:

1. Move the relevant changelog entries from `Unreleased` into a dated version
   section.
2. Set the same version in `pyproject.toml` and `src/super_agent/__init__.py`.
3. Run the required unit, compile, and installed-entry-point checks.
4. Run `python3 scripts/check_release_version.py vX.Y.Z`.
5. Push the release commit and wait for normal CI to pass.
6. Create and push an annotated tag with `git tag -a vX.Y.Z -m "vX.Y.Z"`.

The tag workflow independently checks the annotation, version, changelog, tests,
standalone build, and both command names. It creates the GitHub Release and
attaches `sc`, `release.json`, and `install.py` only after every gate succeeds. A
lightweight tag or mismatched version fails without publishing a release.

## License

By contributing, you agree that your contributions will be licensed under the repository's [MIT License](LICENSE).
