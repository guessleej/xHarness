# Contributing

English | [中文](CONTRIBUTING.zh.md)

Thanks for your interest. xHarness is deliberately small; the bar for a change is "keeps the package readable in an afternoon".

## Ground rules

- **Zero runtime dependencies.** Standard library only. A new runtime dependency needs a written justification in `docs/ssdlc.md` and a supply-chain review.
- **Everything is a plugin.** New capabilities mount through `Plugin.apply(ctx, config)`; nothing gets a private path into the core.
- **Trust boundaries are documented.** If your change adds a tool, a network destination, or a data store, update the threat model in `docs/ssdlc.md` (and `docs/ssdlc.zh.md`) in the same pull request.
- **Tests come with the change.** `python -m pytest -q` must pass; `bandit -r xharness` must be clean, and every `# nosec` carries a justification at the site and in the SSDLC registry.
- **Docs are bilingual.** User-facing changes update both `README.md` (中文) and `README.en.md`.
- No emoji in code, docs, or commit messages.

## Workflow

```sh
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
./.venv/bin/pip install "bandit[toml]" && ./.venv/bin/bandit -r xharness
```

Open a pull request against `main`; CI runs tests on Python 3.11/3.12/3.13 plus bandit, pip-audit, and gitleaks. Security issues go to [SECURITY.md](SECURITY.md), not the issue tracker.
