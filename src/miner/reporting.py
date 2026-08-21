"""Saída de console: progresso e sumários."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

console = Console()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # httpx loga cada requisição em INFO — inutilizável com milhares de calls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def progress_bar(description: str, total: int) -> Iterator[Callable[..., None]]:
    """Barra de progresso que entrega um `advance(n, suffix)` ao chamador."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("[dim]{task.fields[note]}"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(description, total=total, note="")

        def advance(step: int = 1, note: str = "") -> None:
            progress.update(task, advance=step, note=note)

        yield advance


def summary_table(title: str, rows: dict[str, Any], *, key_label: str = "Métrica") -> None:
    table = Table(title=title, title_style="bold cyan", show_lines=False)
    table.add_column(key_label, style="cyan")
    table.add_column("Valor", style="white", justify="right")
    for key, value in rows.items():
        table.add_row(str(key), f"{value:,}" if isinstance(value, int) else str(value))
    console.print(table)


def distribution_table(title: str, counts: dict[str, int]) -> None:
    if not counts:
        return
    total = sum(counts.values()) or 1
    table = Table(title=title, title_style="bold cyan")
    table.add_column("Tag", style="cyan")
    table.add_column("Qtd", justify="right")
    table.add_column("%", justify="right", style="dim")
    table.add_column("", style="green")
    for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
        pct = 100 * value / total
        table.add_row(key, f"{value:,}", f"{pct:.1f}%", "█" * max(1, round(pct / 2.5)))
    console.print(table)
