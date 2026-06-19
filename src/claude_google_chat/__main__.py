"""Entry point for ``python -m claude_google_chat``."""

from __future__ import annotations

from claude_google_chat.cli import app


def main() -> None:
    """Invoke the Typer CLI application."""
    app()


if __name__ == "__main__":
    main()
