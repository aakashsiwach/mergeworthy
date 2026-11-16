"""Command-line interface."""

import typer

from mergeworthy import __version__

app = typer.Typer(
    name="mergeworthy",
    help="An autonomous open-source contributor that only submits work a human has approved.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """mergeworthy command-line interface."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"mergeworthy {__version__}")
