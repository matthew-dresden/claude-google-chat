.PHONY: install lint format typecheck test build all

install:
	uv sync --all-extras

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src

test:
	uv run pytest

build:
	uv build

all: lint typecheck test build
