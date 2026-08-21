"""CLI do pipeline de mining.

Cada layer é executável isoladamente, o que importa na prática: as layers 1 e 2
custam chamadas de API (caras, sujeitas a rate limit), enquanto 3 e 4 são
locais. Iterar nas heurísticas de tagging ou no balanceamento deve custar
segundos de CPU, nunca uma nova rodada de download.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from . import state
from .config import (
    DATA_DIR,
    DATASET_FILE,
    FINAL_DIR,
    RAW_DIR,
    RuntimeOptions,
    ensure_dirs,
)
from .github.client import GitHubClient, GitHubError
from .models import ValidatedRepo
from .pipeline import layer1_discover, layer2_extract, layer3_normalize, layer4_dataset
from .reporting import console, distribution_table, setup_logging, summary_table

log = logging.getLogger("miner")


def _repo_names() -> dict[str, str]:
    """repo_id -> full_name, lido dos manifests em disco."""
    names: dict[str, str] = {}
    if not RAW_DIR.exists():
        return names
    for manifest_path in RAW_DIR.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            names[data["repo_id"]] = data["full_name"]
        except (json.JSONDecodeError, KeyError):
            continue
    return names


def _load_validated() -> list[ValidatedRepo]:
    cached = state.load("layer1_validated", [])
    return [ValidatedRepo.from_manifest(r["manifest"], r["dependencies"]) for r in cached]


async def _run_remote(args: argparse.Namespace, stages: set[str]) -> None:
    """Executa as layers que precisam de rede, num único cliente/sessão."""
    options = RuntimeOptions(
        tiers=tuple(args.tiers),
        limit=args.limit,
        resume=not args.fresh,
        concurrency_scale=args.scale,
    )

    async with GitHubClient(scale=args.scale) as client:
        repos: list[ValidatedRepo] = []

        if "discover" in stages:
            repos = await layer1_discover.run(client, options)
        elif "extract" in stages:
            repos = _load_validated()
            if not repos:
                console.print("[red]Nenhum repositório validado. Rode `discover` antes.[/red]")
                return

        if "extract" in stages:
            if args.limit:
                repos = repos[: args.limit]
            await layer2_extract.run(client, repos, options)

        console.print()
        summary_table(
            "Consumo de API",
            {
                f"{name}: requisições": data["requests"]
                for name, data in client.budget_report().items()
            },
            key_label="Recurso",
        )
        waited = sum(d["waited_seconds"] for d in client.budget_report().values())
        if waited:
            console.print(f"[dim]Tempo total em rate limit: {waited:.0f}s[/dim]")


def _run_local(stages: set[str], max_per_tag: int | None = None) -> None:
    snippets = []
    if "normalize" in stages:
        snippets = layer3_normalize.run()
    elif "dataset" in stages:
        snippets = layer3_normalize.load_snippets()

    if "dataset" in stages:
        if not snippets:
            console.print("[red]Nenhum snippet. Rode `normalize` antes.[/red]")
            return
        stats = layer4_dataset.run(snippets, _repo_names(), max_per_tag)
        if stats:
            console.print()
            summary_table(
                "Dataset final",
                {
                    "Exemplos": stats["total_snippets"],
                    "Treino": stats["train_split"],
                    "Validação": stats["val_split"],
                    "Repositórios de origem": stats["unique_repos"],
                    "Linhas por exemplo (média)": stats["avg_lines"],
                },
            )
            distribution_table("Distribuição por tag", stats["by_tag"])
            distribution_table(
                "Distribuição por tier", {f"Tier {k}": v for k, v in stats["by_tier"].items()}
            )
            console.print(f"\n[green]Dataset pronto:[/green] {DATASET_FILE}")


def cmd_status(args: argparse.Namespace) -> None:
    repos = _load_validated()
    extracted = state.load("layer2_extracted", {})
    tags = state.load("layer3_tags", {})

    summary_table(
        "Estado do pipeline",
        {
            "Layer 1 — repositórios validados": len(repos),
            "Layer 2 — repositórios extraídos": len(extracted),
            "Layer 2 — arquivos baixados": sum(extracted.values()) if extracted else 0,
            "Layer 3 — snippets únicos": sum(tags.values()) if tags else 0,
            "Layer 4 — dataset gerado": "sim" if DATASET_FILE.exists() else "não",
        },
        key_label="Etapa",
    )
    if tags:
        distribution_table("Snippets por tag (pré-balanceamento)", tags)

    rejected = state.load("layer1_rejected", {})
    if rejected.get("reasons"):
        distribution_table("Descartes na Layer 1", rejected["reasons"])


def _dir_size(path: Path) -> str:
    if not path.exists():
        return "0 B"
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024:
            return f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} TB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mine",
        description="Mining de repositórios FastAPI no GitHub para dataset de finetuning.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log em DEBUG")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, remote: bool = True) -> None:
        if remote:
            p.add_argument(
                "--tiers",
                type=int,
                nargs="+",
                default=[1, 2, 3],
                choices=[1, 2, 3],
                help="tiers de busca a executar (padrão: 1 2 3)",
            )
            p.add_argument("--limit", type=int, help="limita o número de repositórios")
            p.add_argument(
                "--fresh", action="store_true", help="ignora checkpoints e refaz do zero"
            )
            p.add_argument(
                "--scale",
                type=float,
                default=1.0,
                help="multiplicador de concorrência (padrão: 1.0)",
            )

    p_discover = sub.add_parser("discover", help="Layer 1 — busca e valida FastAPI")
    add_common(p_discover)

    p_extract = sub.add_parser("extract", help="Layer 2 — baixa arquivos de alto sinal")
    add_common(p_extract)

    sub.add_parser("normalize", help="Layer 3 — snippets, tags e dedupe (local)")
    p_dataset = sub.add_parser("dataset", help="Layer 4 — balanceia e gera o dataset (local)")

    p_all = sub.add_parser("all", help="executa as 4 layers de ponta a ponta")
    add_common(p_all)

    for p in (p_dataset, p_all):
        p.add_argument(
            "--max-per-tag",
            type=int,
            help="teto fixo de snippets por tag (padrão: adaptativo pela mediana)",
        )

    sub.add_parser("status", help="mostra o estado atual do pipeline")
    return parser


_STAGES = {
    "discover": {"discover"},
    "extract": {"extract"},
    "normalize": {"normalize"},
    "dataset": {"dataset"},
    "all": {"discover", "extract", "normalize", "dataset"},
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    ensure_dirs()

    if args.command == "status":
        cmd_status(args)
        return 0

    stages = _STAGES[args.command]
    started = time.monotonic()

    try:
        if stages & {"discover", "extract"}:
            asyncio.run(_run_remote(args, stages))
        if stages & {"normalize", "dataset"}:
            _run_local(stages, getattr(args, "max_per_tag", None))
    except GitHubError as exc:
        console.print(f"[red]Erro na API do GitHub:[/red] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido. Os checkpoints foram preservados — "
                      "rode o mesmo comando para continuar de onde parou.[/yellow]")
        return 130

    elapsed = time.monotonic() - started
    console.print(
        f"\n[dim]Concluído em {elapsed:.0f}s · dados em {DATA_DIR} "
        f"(raw: {_dir_size(RAW_DIR)}, final: {_dir_size(FINAL_DIR)})[/dim]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
