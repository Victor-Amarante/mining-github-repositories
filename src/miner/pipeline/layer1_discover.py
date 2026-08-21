"""LAYER 1 — Fetch + Validate (fail-fast).

Busca por tier, deduplica candidatos e valida a dependência FastAPI *antes*
de qualquer download de código. Repositórios que apareceram na busca textual
mas não declaram fastapi morrem aqui, que é o desperdício número um em
mining desse tipo.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from .. import state
from ..config import RAW_DIR, SEARCH_QUERIES, RuntimeOptions
from ..github.client import GitHubClient, GitHubError
from ..github.graphql import chunked, validate_batch
from ..models import RepoCandidate, ValidatedRepo
from ..reporting import progress_bar

log = logging.getLogger("miner.layer1")

_PER_PAGE = 100
_MAX_PAGES = 10  # a Search API não devolve além de 1.000 resultados por query


async def _search_query(
    client: GitHubClient, query: str, tier: int, target: int, label: str
) -> list[RepoCandidate]:
    """Pagina uma query até atingir a meta de repositórios."""
    found: list[RepoCandidate] = []
    for page in range(1, _MAX_PAGES + 1):
        if len(found) >= target:
            break
        try:
            payload = await client.search_repositories(query, page=page, per_page=_PER_PAGE)
        except GitHubError as exc:
            log.warning("[tier %d/%s] busca falhou na página %d: %s", tier, label, page, exc)
            break

        items = payload.get("items") or []
        if not items:
            break

        for item in items:
            if len(found) >= target:
                break
            found.append(
                RepoCandidate(
                    full_name=item["full_name"],
                    url=item["html_url"],
                    stars=item.get("stargazers_count", 0),
                    license=(item.get("license") or {}).get("spdx_id"),
                    last_updated=item.get("pushed_at") or item.get("updated_at") or "",
                    default_branch=item.get("default_branch") or "main",
                    description=item.get("description"),
                    tier=tier,
                    search_query=query,
                )
            )

        if len(items) < _PER_PAGE:
            break

    return found


def _dedupe(batches: Iterable[list[RepoCandidate]]) -> list[RepoCandidate]:
    """Um repo pode casar com várias queries. O tier mais baixo (melhor) vence."""
    best: dict[str, RepoCandidate] = {}
    for batch in batches:
        for candidate in batch:
            current = best.get(candidate.full_name)
            if current is None or candidate.tier < current.tier:
                best[candidate.full_name] = candidate
    return sorted(best.values(), key=lambda c: (c.tier, -c.stars))


async def search_candidates(
    client: GitHubClient, options: RuntimeOptions
) -> list[RepoCandidate]:
    queries = [q for q in SEARCH_QUERIES if q.tier in options.tiers]
    batches: list[list[RepoCandidate]] = []

    # A Search API é serial por natureza (30 req/min, concorrência 1), então
    # não há ganho em paralelizar aqui — só ruído no rate limiter.
    with progress_bar("Buscando repositórios", total=len(queries)) as advance:
        for query in queries:
            batch = await _search_query(
                client, query.query, query.tier, query.target, query.label
            )
            batches.append(batch)
            log.info("[tier %d/%s] %d candidatos", query.tier, query.label, len(batch))
            advance(1, f"tier {query.tier}/{query.label}: {len(batch)}")

    candidates = _dedupe(batches)
    if options.limit:
        candidates = _stratified_limit(candidates, options.limit)
    return candidates


def _stratified_limit(candidates: list[RepoCandidate], limit: int) -> list[RepoCandidate]:
    """Corta mantendo a proporção entre os tiers.

    Um corte simples nos primeiros N elimina os tiers baixos por completo (a
    lista vem ordenada por tier), o que faria uma execução limitada testar
    apenas o Tier 1 e esconder problemas nas outras queries.
    """
    by_tier: dict[int, list[RepoCandidate]] = {}
    for candidate in candidates:
        by_tier.setdefault(candidate.tier, []).append(candidate)

    total = len(candidates)
    picked: list[RepoCandidate] = []
    for tier in sorted(by_tier):
        group = by_tier[tier]
        share = max(1, round(limit * len(group) / total))
        picked.extend(group[:share])

    return sorted(picked, key=lambda c: (c.tier, -c.stars))[:limit]


async def validate_candidates(
    client: GitHubClient, candidates: list[RepoCandidate]
) -> tuple[list[ValidatedRepo], list[tuple[str, str]]]:
    batches = list(chunked(candidates))
    validated: list[ValidatedRepo] = []
    rejected: list[tuple[str, str]] = []

    semaphore = asyncio.Semaphore(4)

    async def run(batch: list[RepoCandidate], advance) -> None:
        async with semaphore:
            ok, bad = await validate_batch(client, batch)
        validated.extend(ok)
        rejected.extend(bad)
        advance(len(batch), f"{len(validated)} validados")

    with progress_bar("Validando FastAPI (GraphQL)", total=len(candidates)) as advance:
        await asyncio.gather(*(run(batch, advance) for batch in batches))

    validated.sort(key=lambda v: (v.candidate.tier, -v.candidate.stars))
    return validated, rejected


def persist(validated: list[ValidatedRepo]) -> None:
    """Grava manifest.json e dependencies.json por repo (camada de auditoria)."""
    for repo in validated:
        repo_dir = RAW_DIR / repo.repo_id
        repo_dir.mkdir(parents=True, exist_ok=True)
        state.write_json(repo_dir / "manifest.json", repo.manifest())
        state.write_json(repo_dir / "dependencies.json", repo.dependencies.to_dict())


async def run(client: GitHubClient, options: RuntimeOptions) -> list[ValidatedRepo]:
    if options.resume and state.exists("layer1_validated"):
        cached = state.load("layer1_validated", [])
        repos = [ValidatedRepo.from_manifest(r["manifest"], r["dependencies"]) for r in cached]
        log.info("Layer 1: reaproveitando %d repositórios validados", len(repos))
        return repos

    candidates = await search_candidates(client, options)
    log.info("Layer 1: %d candidatos únicos após dedupe", len(candidates))

    validated, rejected = await validate_candidates(client, candidates)
    persist(validated)

    state.save(
        "layer1_validated",
        [{"manifest": r.manifest(), "dependencies": r.dependencies.to_dict()} for r in validated],
    )
    state.save(
        "layer1_rejected",
        {"total": len(rejected), "reasons": _reason_counts(rejected), "items": rejected[:200]},
    )

    log.info(
        "Layer 1: %d validados, %d descartados (%.0f%% de aproveitamento)",
        len(validated),
        len(rejected),
        100 * len(validated) / max(1, len(candidates)),
    )
    return validated


def _reason_counts(rejected: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, reason in rejected:
        key = reason.split(":", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return counts
