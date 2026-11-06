.PHONY: help install lint format type test cov build manifest clean ci

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install dev dependencies
	pip install -e ".[dev]"

lint:  ## Run linter
	ruff check src tests

format:  ## Auto-format and fix
	ruff check --fix src tests
	ruff format src tests

type:  ## Run type checker
	mypy src

test:  ## Run tests
	pytest -q

cov:  ## Run tests with coverage
	pytest -q --cov=suno_dl --cov-report=term-missing --cov-fail-under=70

build:  ## Build sdist and wheel
	python -m build --no-isolation

manifest:  ## Pin a reproducible wheel to HEAD (scripts/release_manifest.py)
	python scripts/release_manifest.py --ref $(or $(REF),HEAD) --out dist/release-manifest.json

ci:  ## Reproduce the full CI gate locally
	bash scripts/ci-local.sh $(REF)

clean:  ## Remove build artifacts and caches
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
