"""Modelos de dados que atravessam as 4 layers do pipeline.

Dataclasses puras — cada uma sabe se serializar para o formato em disco
definido na seção 3 do design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def repo_id_of(full_name: str) -> str:
    """`tiangolo/fastapi` -> `tiangolo__fastapi` (seguro como nome de diretório)."""
    return full_name.replace("/", "__")


@dataclass
class RepoCandidate:
    """Saída da Search API, antes de qualquer validação de dependência."""

    full_name: str
    url: str
    stars: int
    license: str | None
    last_updated: str
    default_branch: str
    description: str | None
    tier: int
    search_query: str

    @property
    def repo_id(self) -> str:
        return repo_id_of(self.full_name)

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "url": self.url,
            "stars": self.stars,
            "license": self.license,
            "last_updated": self.last_updated,
            "default_branch": self.default_branch,
            "description": self.description,
            "tier": self.tier,
            "search_query": self.search_query,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepoCandidate:
        return cls(**d)


@dataclass
class Dependencies:
    """Conteúdo de `raw/{repo_id}/dependencies.json`."""

    source_file: str
    fastapi_version: str | None
    dependencies: dict[str, str]
    ecosystem_tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "fastapi_version": self.fastapi_version,
            "dependencies": self.dependencies,
            "ecosystem_tags": self.ecosystem_tags,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Dependencies:
        return cls(**d)


@dataclass
class ValidatedRepo:
    """Candidato que provou declarar FastAPI. Vira `manifest.json`."""

    candidate: RepoCandidate
    commit_sha: str
    dependencies: Dependencies
    fetched_at: str

    @property
    def repo_id(self) -> str:
        return self.candidate.repo_id

    def manifest(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "repo_id": c.repo_id,
            "full_name": c.full_name,
            "url": c.url,
            "stars": c.stars,
            "license": c.license,
            "last_updated": c.last_updated,
            "tier": c.tier,
            "search_query": c.search_query,
            "fetched_at": self.fetched_at,
            "default_branch": c.default_branch,
            "commit_sha": self.commit_sha,
        }

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any], deps: dict[str, Any]) -> ValidatedRepo:
        candidate = RepoCandidate(
            full_name=manifest["full_name"],
            url=manifest["url"],
            stars=manifest["stars"],
            license=manifest.get("license"),
            last_updated=manifest["last_updated"],
            default_branch=manifest["default_branch"],
            description=manifest.get("description"),
            tier=manifest["tier"],
            search_query=manifest["search_query"],
        )
        return cls(
            candidate=candidate,
            commit_sha=manifest["commit_sha"],
            dependencies=Dependencies.from_dict(deps),
            fetched_at=manifest["fetched_at"],
        )


@dataclass
class TreeEntry:
    """Um arquivo no Git Tree, já classificado por papel."""

    path: str
    blob_sha: str
    size: int
    role: str


@dataclass
class ExtractedFile:
    """Arquivo baixado da Layer 2, pronto para gravar em `raw/{repo_id}/files/`."""

    repo_id: str
    path: str
    role: str
    content: str
    blob_sha: str


@dataclass
class Snippet:
    """Uma linha de `processed/snippets.jsonl`."""

    snippet_id: str
    repo_id: str
    file_path: str
    role: str
    tag: str
    secondary_tags: list[str]
    code: str
    context_imports: list[str]
    line_start: int
    line_end: int
    repo_stars: int
    tier: int
    dedup_hash: str
    symbol: str = ""
    is_async: bool = False
    has_docstring: bool = False
    quality: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "snippet_id": self.snippet_id,
            "repo_id": self.repo_id,
            "file_path": self.file_path,
            "role": self.role,
            "tag": self.tag,
            "secondary_tags": self.secondary_tags,
            "code": self.code,
            "context_imports": self.context_imports,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "repo_stars": self.repo_stars,
            "tier": self.tier,
            "dedup_hash": self.dedup_hash,
            "symbol": self.symbol,
            "is_async": self.is_async,
            "has_docstring": self.has_docstring,
            "quality": round(self.quality, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Snippet:
        return cls(**d)


@dataclass
class DatasetRecord:
    """Uma linha de `final/balanced_dataset.jsonl`."""

    id: str
    instruction: str
    input: str
    output: str
    spec_sdd: dict[str, str]
    tags: list[str]
    tier: int
    source_repo: str
    source_snippet_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instruction": self.instruction,
            "input": self.input,
            "output": self.output,
            "spec_sdd": self.spec_sdd,
            "tags": self.tags,
            "tier": self.tier,
            "source_repo": self.source_repo,
            "source_snippet_id": self.source_snippet_id,
            "metadata": self.metadata,
        }
