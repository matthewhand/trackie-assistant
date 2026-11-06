# Optional prototype checks

These checks exercise historical, unintegrated research models. They are not
evidence that the shipped application implements those models. They are
preserved for reference and excluded from the routine Linux release suite.

Run explicitly from the repository root:

    .venv/bin/pytest -q research_checks

The default command remains:

    .venv/bin/pytest -q

Production producer verification, lossless tagging, prepared publication,
real process-death recovery, migration, library ownership and UI regressions
remain in tests/. Shared model helpers and captured fixtures remain there so
the optional experiments can still be reproduced. External HTTP remains blocked.
