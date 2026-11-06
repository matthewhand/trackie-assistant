# Contributing

Thanks for considering a contribution.

## Development setup

```bash
git clone https://github.com/matthewhand/trackie-assistant.git
cd trackie-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# python -m playwright install chromium  # only for manual browser-fallback testing
```

## Before opening a pull request

All of these must pass locally (CI runs the same):

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
```

## Guidelines

- Add or update tests for any behaviour change. The test suite must pass on
  Python 3.13 on Linux. Other platforms may run read-only tooling, but archive
  writes are outside the current release scope.
- Never log signed URLs or credentials. Use `suno_dl.urls.redact_url`.
- Never fetch a URL that is not on the media allow-list
  (`suno_dl.urls.is_allowed_media_url`).
- Keep filenames safe: route all names through `suno_dl.naming`.
- One logical change per pull request.

## Reporting bugs

Include: command run, Python version, OS, and full output (redact any `sig=`
query parameters — the tool does not print them, but copy/paste may).
