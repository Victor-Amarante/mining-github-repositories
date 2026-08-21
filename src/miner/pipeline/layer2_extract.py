"""LAYER 2 — Extract Essentials (seletivo, sem full clone).

Duas decisões carregam esta layer:

1. **Uma chamada de árvore por repo.** `git/trees/{sha}?recursive=1` devolve
   a listagem completa com path *e tamanho* de cada blob. A seleção dos
   arquivos de alto sinal vira computação local — zero chamadas de descoberta.

2. **Conteúdo pelo CDN.** `raw.githubusercontent.com` não consome a cota de
   5.000/h da REST API. Como o download de arquivos é o passo de maior volume
   (~20 arquivos × centenas de repos), tirá-lo da cota é a diferença entre o
   mining caber numa janela de rate limit ou não. A Blobs API fica como
   fallback quando o CDN falha.

A árvore é pedida pelo `commit_sha` fixado na Layer 1, não pelo nome do
branch — o que torna a extração reproduzível mesmo se o repo receber push
no meio da execução.
"""

from __future__ import annotations

import asyncio
import logging

from .. import state
from ..analysis.paths import select_files
from ..config import RAW_DIR, RuntimeOptions
from ..github.client import GitHubClient, GitHubError, NotFound, RetriesExhausted
from ..models import ExtractedFile, TreeEntry, ValidatedRepo
from ..reporting import progress_bar
from . import layer1_discover

log = logging.getLogger("miner.layer2")


async def _fetch_tree(client: GitHubClient, repo: ValidatedRepo) -> list[TreeEntry] | None:
    """Devolve as entradas selecionadas, `[]` se o repo não tem nada de útil,
    ou `None` se a falha foi transitória (para não marcar o repo como feito)."""
    owner, name = repo.candidate.owner, repo.candidate.name
    try:
        payload = await client.get_tree(owner, name, repo.commit_sha)
    except NotFound:
        log.warning("árvore ausente para %s", repo.candidate.full_name)
        return []
    except RetriesExhausted as exc:
        # Rate limit ou indisponibilidade: gravar 0 arquivos no checkpoint
        # faria o rerun pular este repositório para sempre.
        log.warning("falha transitória em %s: %s", repo.candidate.full_name, exc)
        return None
    except GitHubError as exc:
        log.warning("falha na árvore de %s: %s", repo.candidate.full_name, exc)
        return []

    if payload.get("truncated"):
        # Monorepos gigantes: a árvore veio cortada. Trabalhamos com o que veio
        # em vez de paginar subárvores — o sinal útil está na raiz do projeto.
        log.debug("árvore truncada em %s", repo.candidate.full_name)

    return select_files(payload.get("tree") or [])


async def _fetch_content(
    client: GitHubClient, repo: ValidatedRepo, entry: TreeEntry
) -> ExtractedFile | None:
    owner, name = repo.candidate.owner, repo.candidate.name
    try:
        text = await client.get_raw_file(owner, name, repo.commit_sha, entry.path)
    except (GitHubError, NotFound):
        try:
            text = await client.get_blob(owner, name, entry.blob_sha)
        except (GitHubError, NotFound):
            return None

    if not text.strip():
        return None

    return ExtractedFile(
        repo_id=repo.repo_id,
        path=entry.path,
        role=entry.role,
        content=text,
        blob_sha=entry.blob_sha,
    )


def _write_files(repo_id: str, files: list[ExtractedFile]) -> None:
    """Cópia literal em `raw/{repo_id}/files/`, preservando o path original."""
    base = RAW_DIR / repo_id / "files"
    for file in files:
        target = base / file.path
        # Defesa contra path traversal vindo de um nome de arquivo hostil.
        try:
            target.resolve().relative_to(base.resolve())
        except ValueError:
            log.warning("path suspeito ignorado: %s", file.path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")


async def _process_repo(
    client: GitHubClient, repo: ValidatedRepo, semaphore: asyncio.Semaphore
) -> tuple[str, int | None, dict[str, int]]:
    async with semaphore:
        entries = await _fetch_tree(client, repo)
        if entries is None:
            return repo.repo_id, None, {}  # transitório: não marcar como feito
        if not entries:
            return repo.repo_id, 0, {}

        results = await asyncio.gather(
            *(_fetch_content(client, repo, entry) for entry in entries)
        )
        files = [f for f in results if f is not None]

    _write_files(repo.repo_id, files)

    roles: dict[str, int] = {}
    for file in files:
        roles[file.role] = roles.get(file.role, 0) + 1
    state.write_json(
        RAW_DIR / repo.repo_id / "files_index.json",
        {
            "repo_id": repo.repo_id,
            "commit_sha": repo.commit_sha,
            "files": [
                {"path": f.path, "role": f.role, "blob_sha": f.blob_sha} for f in files
            ],
            "roles": roles,
        },
    )
    return repo.repo_id, len(files), roles


async def run(
    client: GitHubClient, repos: list[ValidatedRepo], options: RuntimeOptions
) -> dict[str, int]:
    done: dict[str, int] = state.load("layer2_extracted", {}) if options.resume else {}
    pending = [r for r in repos if r.repo_id not in done]

    # Garante que `raw/{repo_id}/` tenha manifest.json mesmo se a Layer 1 rodou
    # em outra sessão ou o diretório foi limpo: a Layer 3 lê o manifest para
    # saber stars/tier, e sem ele o repositório inteiro seria ignorado em
    # silêncio. Regravar é local e gratuito.
    layer1_discover.persist(pending)

    if not pending:
        log.info("Layer 2: nada pendente (%d repositórios já extraídos)", len(done))
        return done

    # Concorrência no nível do repositório; os arquivos de cada repo já saem
    # em paralelo dentro do budget do CDN.
    semaphore = asyncio.Semaphore(max(1, int(6 * options.concurrency_scale)))
    role_totals: dict[str, int] = {}
    failed = 0

    with progress_bar("Extraindo arquivos essenciais", total=len(pending)) as advance:
        tasks = [
            asyncio.create_task(_process_repo(client, repo, semaphore)) for repo in pending
        ]
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            repo_id, count, roles = await task
            if count is not None:
                done[repo_id] = count
            else:
                failed += 1
            for role, n in roles.items():
                role_totals[role] = role_totals.get(role, 0) + n
            advance(1, f"{sum(done.values())} arquivos")
            if index % 25 == 0:
                state.save("layer2_extracted", done)  # checkpoint incremental

    state.save("layer2_extracted", done)
    state.save("layer2_roles", role_totals)

    total = sum(done.values())
    log.info(
        "Layer 2: %d arquivos de %d repositórios (%.1f por repo) | papéis: %s",
        total,
        len(done),
        total / max(1, len(done)),
        role_totals,
    )
    if failed:
        log.warning(
            "Layer 2: %d repositórios com falha transitória — rode `mine extract` "
            "de novo para recuperá-los",
            failed,
        )
    return done
