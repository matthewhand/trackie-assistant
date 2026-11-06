# Test scope

The routine Linux suite tests shipped behavior and known regressions. Its
purpose is to catch meaningful failures, not maximize the case count.

Unintegrated research-model checks live in research_checks/ and run only when
explicitly requested. Production download, producer identity, media preservation,
publication/recovery and migration checks remain in the normal release gate.

Browser interactions run at representative phone, tablet and desktop sizes.
Additional phone and wide-desktop sizes retain the complete layout assertions
without repeating every behavior at equivalent layouts.

    .venv/bin/pytest -q --cov=suno_dl --cov-fail-under=70

Optional research checks:

    .venv/bin/pytest -q research_checks

No live Suno traffic or real music library is needed for either suite.
