"""Classificação de paths do Git Tree em papéis (Layer 2).

Decide, sem baixar um byte, quais arquivos da árvore valem o download.
Toda a seleção acontece localmente sobre a resposta única da Trees API.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..config import (
    EXCLUDE_PATTERNS,
    MAX_FILE_BYTES,
    MAX_FILES_PER_REPO,
    ROLE_FILE_QUOTA,
    ROLE_PATTERNS,
)
from ..models import TreeEntry

# Ordem de avaliação: papéis mais específicos primeiro. `test` vem antes de
# tudo porque `tests/routers/test_user.py` é um teste, não um router.
_ROLE_ORDER = (
    "test",
    "entrypoint",
    "router",
    "dependency",
    "schema",
    "middleware",
    "database",
    "config",
)

# Prioridade de download quando o repo estoura MAX_FILES_PER_REPO. Reflete o
# valor de sinal para finetuning: rotas e schemas ensinam mais que config.
_ROLE_PRIORITY = {
    "router": 0,
    "entrypoint": 1,
    "schema": 2,
    "dependency": 3,
    "database": 4,
    "middleware": 5,
    "test": 6,
    "config": 7,
}


@lru_cache(maxsize=1)
def _compiled_roles() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    return tuple(
        (role, tuple(re.compile(p, re.IGNORECASE) for p in ROLE_PATTERNS[role]))
        for role in _ROLE_ORDER
    )


@lru_cache(maxsize=1)
def _compiled_excludes() -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in EXCLUDE_PATTERNS)


def is_excluded(path: str) -> bool:
    return any(p.search(path) for p in _compiled_excludes())


def classify_role(path: str) -> str | None:
    """Devolve o papel do arquivo, ou None se não for de alto sinal."""
    if not path.endswith(".py") or is_excluded(path):
        return None
    for role, patterns in _compiled_roles():
        if any(p.search(path) for p in patterns):
            return role
    return None


def select_files(tree: list[dict], *, max_files: int = MAX_FILES_PER_REPO) -> list[TreeEntry]:
    """Filtra a árvore recursiva para os arquivos que merecem download.

    `tree` é o array `tree` cru da Git Trees API. Blobs grandes demais são
    descartados aqui — o `size` vem na própria resposta, então nem chegamos
    a pedir o conteúdo.
    """
    candidates: list[TreeEntry] = []
    for node in tree:
        if node.get("type") != "blob":
            continue
        path = node.get("path", "")
        size = int(node.get("size") or 0)
        if size == 0 or size > MAX_FILE_BYTES:
            continue
        role = classify_role(path)
        if role is None:
            continue
        candidates.append(
            TreeEntry(path=path, blob_sha=node.get("sha", ""), size=size, role=role)
        )

    # Ordena por valor de sinal e, dentro do papel, prefere os arquivos mais
    # rasos: `app/routers/user.py` tende a ser mais canônico que
    # `app/v1/legacy/internal/routers/user.py`.
    candidates.sort(
        key=lambda e: (_ROLE_PRIORITY.get(e.role, 9), e.path.count("/"), -e.size)
    )

    # A quota por papel é o critério de seleção — sem recomposição até o teto.
    # Preencher as vagas sobrando com o papel mais abundante seria desfazer o
    # balanceamento: um repo só de testes deve contribuir 8 testes, não 40.
    selected: list[TreeEntry] = []
    used: dict[str, int] = {}
    for entry in candidates:
        quota = ROLE_FILE_QUOTA.get(entry.role, 4)
        if used.get(entry.role, 0) >= quota:
            continue
        used[entry.role] = used.get(entry.role, 0) + 1
        selected.append(entry)
        if len(selected) >= max_files:
            break
    return selected


def role_distribution(entries: list[TreeEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.role] = counts.get(entry.role, 0) + 1
    return counts
