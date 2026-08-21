"""LAYER 3 — Normalize & Dedupe.

Puramente local: lê `raw/`, corta snippets pela AST, tagueia e deduplica.
Nenhuma chamada de rede — o que significa que reprocessar o dataset inteiro
com heurísticas novas custa segundos, não uma nova janela de rate limit.

O dedupe é o passo de maior impacto na qualidade: forks e templates fazem
com que o mesmo `main.py` boilerplate apareça dezenas de vezes. Colidir
esses casos no mesmo hash normalizado evita ensinar o modelo a repetir um
único arquivo que ele viu 40 vezes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import state
from ..analysis.snippets import extract_from_file
from ..config import (
    PROCESSED_DIR,
    RAW_DIR,
    SNIPPETS_FILE,
    SNIPPETS_INDEX_FILE,
)
from ..models import ExtractedFile, Snippet
from ..reporting import progress_bar

log = logging.getLogger("miner.layer3")


def _load_repo(repo_dir: Path) -> tuple[dict, dict] | None:
    manifest_path = repo_dir / "manifest.json"
    index_path = repo_dir / "files_index.json"
    if not manifest_path.exists() or not index_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return manifest, index


def _iter_files(repo_dir: Path, index: dict) -> list[ExtractedFile]:
    base = repo_dir / "files"
    files: list[ExtractedFile] = []
    for entry in index.get("files", []):
        path = base / entry["path"]
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files.append(
            ExtractedFile(
                repo_id=index["repo_id"],
                path=entry["path"],
                role=entry["role"],
                content=content,
                blob_sha=entry.get("blob_sha", ""),
            )
        )
    return files


def _dedupe(snippets: list[Snippet]) -> tuple[list[Snippet], int]:
    """Mantém, entre duplicatas, a de maior qualidade (repo mais forte)."""
    best: dict[str, Snippet] = {}
    duplicates = 0
    for snippet in snippets:
        current = best.get(snippet.dedup_hash)
        if current is None:
            best[snippet.dedup_hash] = snippet
        else:
            duplicates += 1
            if snippet.quality > current.quality:
                best[snippet.dedup_hash] = snippet
    return list(best.values()), duplicates


def run() -> list[Snippet]:
    repo_dirs = sorted(d for d in RAW_DIR.iterdir() if d.is_dir()) if RAW_DIR.exists() else []
    if not repo_dirs:
        log.warning("Layer 3: nenhum repositório em %s — rode as layers 1 e 2 antes", RAW_DIR)
        return []

    collected: list[Snippet] = []
    parsed_files = 0
    useful_files = 0

    with progress_bar("Normalizando e tagueando", total=len(repo_dirs)) as advance:
        for repo_dir in repo_dirs:
            loaded = _load_repo(repo_dir)
            if loaded is None:
                advance(1)
                continue
            manifest, index = loaded
            stars = manifest.get("stars", 0)
            tier = manifest.get("tier", 3)

            for file in _iter_files(repo_dir, index):
                parsed_files += 1
                produced = extract_from_file(file, stars=stars, tier=tier)
                if produced:
                    useful_files += 1
                collected.extend(produced)

            advance(1, f"{len(collected)} snippets")

    unique, duplicates = _dedupe(collected)
    unique.sort(key=lambda s: -s.quality)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    state.write_jsonl(SNIPPETS_FILE, [s.to_dict() for s in unique])
    state.write_json(SNIPPETS_INDEX_FILE, {s.snippet_id: s.repo_id for s in unique})

    tag_counts: dict[str, int] = {}
    for snippet in unique:
        tag_counts[snippet.tag] = tag_counts.get(snippet.tag, 0) + 1
    state.save("layer3_tags", tag_counts)

    log.info(
        "Layer 3: %d arquivos (%d relevantes, %d sem uso do ecossistema FastAPI) "
        "-> %d snippets brutos -> %d únicos (%d duplicatas removidas, %.0f%%)",
        parsed_files,
        useful_files,
        parsed_files - useful_files,
        len(collected),
        len(unique),
        duplicates,
        100 * duplicates / max(1, len(collected)),
    )
    return unique


def load_snippets() -> list[Snippet]:
    """Relê `snippets.jsonl` sem reprocessar `raw/`."""
    return [Snippet.from_dict(d) for d in state.read_jsonl(SNIPPETS_FILE)]
