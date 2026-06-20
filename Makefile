.PHONY: install lint format format-check typecheck test build distcheck publish all

install:
	uv sync --all-extras

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest

build:
	uv build

# Validate built artifacts (metadata + archive integrity) before any publish.
distcheck: build
	uvx twine check dist/*

# Manual PyPI publish. Reads the token from the environment (UV_PUBLISH_TOKEN),
# never hardcoded. Use this to validate a manual publish before relying on the
# automated OIDC trusted-publishing workflow.
#   export UV_PUBLISH_TOKEN=pypi-...   # a PyPI API token
#   make publish
publish: distcheck
	uv publish

all: lint typecheck test build
