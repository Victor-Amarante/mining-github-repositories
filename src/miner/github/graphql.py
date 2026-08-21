"""Validação de dependência em lote via GraphQL (Layer 1).

Esta é a otimização estrutural do pipeline. A abordagem óbvia — Contents API
por arquivo de manifesto — custa 3 a 5 chamadas REST *por repositório*, o que
para ~800 candidatos significa até 4.000 requisições contra uma cota de
5.000/hora, antes de baixar uma única linha de código útil.

A GraphQL resolve o mesmo problema com aliases: uma requisição pede, para 40
repositórios de uma vez, o SHA do commit HEAD, a licença, as estrelas *e o
texto integral de todos os manifestos*. 800 candidatos passam a custar ~20
requisições — e o `commit_sha` que fixa a versão minerada já vem junto,
eliminando também a chamada extra que seria necessária para obtê-lo.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from ..analysis.deps import build_dependencies
from ..config import GRAPHQL_BATCH_SIZE, MANIFEST_FILES
from ..models import RepoCandidate, ValidatedRepo
from .client import GitHubClient, GitHubError, RetriesExhausted

log = logging.getLogger("miner.graphql")

# Owner/name aceitam ponto e hífen (`fastapi-users`, `socket.io`).
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
# Aliases GraphQL, não: só `[_A-Za-z][_0-9A-Za-z]*`.
_ALIAS_UNSAFE = re.compile(r"[^A-Za-z0-9_]")

_MANIFEST_ALIASES = {
    path: "m" + _ALIAS_UNSAFE.sub("_", path) for path in MANIFEST_FILES
}

_BLOB_FRAGMENT = "{ ... on Blob { text isTruncated byteSize } }"


def _repo_block(index: int, owner: str, name: str) -> str:
    """Um nó `repository` aliasado, com todos os manifestos de uma vez."""
    files = "\n".join(
        f'      {alias}: object(expression: "HEAD:{path}") {_BLOB_FRAGMENT}'
        for path, alias in _MANIFEST_ALIASES.items()
    )
    return f"""  r{index}: repository(owner: "{owner}", name: "{name}") {{
      nameWithOwner
      stargazerCount
      pushedAt
      isArchived
      isFork
      licenseInfo {{ spdxId }}
      defaultBranchRef {{ name target {{ ... on Commit {{ oid }} }} }}
{files}
    }}"""


def _build_query(batch: list[RepoCandidate]) -> str:
    blocks = "\n".join(
        _repo_block(i, _SAFE.sub("", c.owner), _SAFE.sub("", c.name))
        for i, c in enumerate(batch)
    )
    return f"query ValidateBatch {{\n{blocks}\n}}"


def _extract_manifests(node: dict) -> dict[str, str]:
    manifests: dict[str, str] = {}
    for path, alias in _MANIFEST_ALIASES.items():
        blob = node.get(alias)
        if not blob:
            continue
        text = blob.get("text")
        # `text` é null para binário; truncado não serve para parsing confiável.
        if text and not blob.get("isTruncated"):
            manifests[path] = text
    return manifests


async def validate_batch(
    client: GitHubClient, batch: list[RepoCandidate]
) -> tuple[list[ValidatedRepo], list[tuple[str, str]]]:
    """Valida até `GRAPHQL_BATCH_SIZE` repositórios em uma requisição.

    Devolve `(validados, rejeitados)` — rejeitados carregam o motivo, para
    o relatório final saber por que a taxa de aproveitamento foi a que foi.
    """
    if not batch:
        return [], []

    try:
        payload = await client.graphql(_build_query(batch))
    except RetriesExhausted:
        # Rate limit persistente: dividir dobraria as requisições contra o
        # limite que acabou de estourar. Propaga para o operador tratar —
        # os checkpoints já gravados preservam o progresso.
        raise
    except GitHubError as exc:
        # Erro de query (repo removido, nome inválido): divide e tenta de novo
        # para isolar o repositório problemático em vez de descartar o batch.
        if len(batch) > 1:
            mid = len(batch) // 2
            left = await validate_batch(client, batch[:mid])
            right = await validate_batch(client, batch[mid:])
            return left[0] + right[0], left[1] + right[1]
        log.warning("batch de 1 falhou (%s): %s", batch[0].full_name, exc)
        return [], [(batch[0].full_name, f"graphql_error: {exc}")]

    data = payload.get("data") or {}
    validated: list[ValidatedRepo] = []
    rejected: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc).isoformat()

    for index, candidate in enumerate(batch):
        node = data.get(f"r{index}")
        if not node:
            rejected.append((candidate.full_name, "inacessivel"))
            continue

        branch_ref = node.get("defaultBranchRef") or {}
        target = branch_ref.get("target") or {}
        commit_sha = target.get("oid")
        if not commit_sha:
            rejected.append((candidate.full_name, "sem_commit_head"))
            continue

        manifests = _extract_manifests(node)
        if not manifests:
            rejected.append((candidate.full_name, "sem_manifesto"))
            continue

        deps = build_dependencies(manifests)
        if deps is None:
            # É aqui que o fail-fast paga: o repo apareceu na busca textual
            # mas não declara fastapi. Descartado antes de qualquer download.
            rejected.append((candidate.full_name, "fastapi_nao_declarado"))
            continue

        # Metadados da GraphQL são mais frescos que os da Search API.
        enriched = RepoCandidate(
            full_name=node.get("nameWithOwner") or candidate.full_name,
            url=candidate.url,
            stars=node.get("stargazerCount", candidate.stars),
            license=(node.get("licenseInfo") or {}).get("spdxId") or candidate.license,
            last_updated=node.get("pushedAt") or candidate.last_updated,
            default_branch=branch_ref.get("name") or candidate.default_branch,
            description=candidate.description,
            tier=candidate.tier,
            search_query=candidate.search_query,
        )
        validated.append(
            ValidatedRepo(
                candidate=enriched,
                commit_sha=commit_sha,
                dependencies=deps,
                fetched_at=now,
            )
        )

    return validated, rejected


def chunked(items: list[RepoCandidate], size: int = GRAPHQL_BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]
