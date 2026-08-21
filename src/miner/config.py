"""Configuração central do pipeline de mining.

Tudo que é "knob" do pipeline mora aqui: caminhos em disco, queries por tier,
limites de rate limit e heurísticas de seleção de arquivo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("MINER_DATA_DIR", PROJECT_ROOT / "data"))

RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
FINAL_DIR = DATA_DIR / "final"
STATE_DIR = DATA_DIR / ".state"

SNIPPETS_FILE = PROCESSED_DIR / "snippets.jsonl"
SNIPPETS_INDEX_FILE = PROCESSED_DIR / "snippets_index.json"
DATASET_FILE = FINAL_DIR / "balanced_dataset.jsonl"
DATASET_TRAIN_FILE = FINAL_DIR / "balanced_dataset_train.jsonl"
DATASET_VAL_FILE = FINAL_DIR / "balanced_dataset_val.jsonl"
DATASET_STATS_FILE = FINAL_DIR / "dataset_stats.json"


def ensure_dirs() -> None:
    for d in (RAW_DIR, PROCESSED_DIR, FINAL_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

API_BASE = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
RAW_BASE = "https://raw.githubusercontent.com"

USER_AGENT = "zup-mining-repos/0.1 (+fastapi-dataset-miner)"


# --------------------------------------------------------------------------
# Estratégia de busca (3 tiers)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchQuery:
    """Uma query de repository-search com sua meta de repositórios."""

    tier: int
    query: str
    target: int
    label: str


# Notas de sintaxe: estes são qualificadores válidos de /search/repositories.
# `fork:false` e `archived:false` cortam clones e projetos mortos na origem —
# é o filtro de qualidade mais barato que existe (custo zero de API).
_BASE_FILTERS = "language:python fork:false archived:false"

SEARCH_QUERIES: tuple[SearchQuery, ...] = (
    # Tier 1 — Core FastAPI, alta qualidade (~50 repos no total)
    SearchQuery(1, f"fastapi {_BASE_FILTERS} stars:>100", 20, "core-stars"),
    SearchQuery(1, f"topic:fastapi {_BASE_FILTERS} stars:>50", 20, "core-topic"),
    SearchQuery(1, f"fastapi template {_BASE_FILTERS} stars:>100", 15, "core-template"),
    # Tier 2 — FastAPI + ecossistema (125 por query)
    SearchQuery(2, f"fastapi pydantic {_BASE_FILTERS} stars:>50", 125, "eco-pydantic"),
    SearchQuery(2, f"fastapi sqlalchemy {_BASE_FILTERS} stars:>50", 125, "eco-sqlalchemy"),
    SearchQuery(2, f"fastapi docker {_BASE_FILTERS} stars:>50", 125, "eco-docker"),
    # Tier 3 — Domain-specific (85 por query)
    SearchQuery(3, f"fastapi api {_BASE_FILTERS} stars:>30", 85, "domain-api"),
    SearchQuery(3, f"fastapi rest {_BASE_FILTERS} stars:>30", 85, "domain-rest"),
    SearchQuery(3, f"fastapi microservice {_BASE_FILTERS} stars:>30", 85, "domain-micro"),
)

# Repos abaixo disso raramente têm código de produção real.
MIN_STARS_ABSOLUTE = 30


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitProfile:
    """Perfil de rate limit para uma classe de recurso da API."""

    name: str
    concurrency: int
    min_interval: float  # segundos entre requisições
    reserve: int  # pausa quando remaining cai abaixo disso


# Search API autenticada: 30 req/min -> 2.0s de intervalo, sem paralelismo.
SEARCH_LIMITS = LimitProfile("search", concurrency=1, min_interval=2.05, reserve=2)

# REST core autenticada: 5.000 req/h. Paralelizamos e deixamos o header ditar
# as pausas em vez de fixar um intervalo conservador.
REST_LIMITS = LimitProfile("rest", concurrency=8, min_interval=0.0, reserve=100)

# GraphQL: 5.000 pontos/h — a cota nunca é o problema aqui. O que dispara o
# rate limit *secundário* (403 + retry-after de 5 min) é complexidade ×
# concorrência: cada query batelada resolve centenas de objetos. Serializar
# com um respiro custa segundos; levar um 403 custa 300s.
GRAPHQL_LIMITS = LimitProfile("graphql", concurrency=2, min_interval=1.0, reserve=50)

# raw.githubusercontent.com é CDN: NÃO consome a cota da REST API.
# É por isso que todo download de conteúdo passa por aqui.
RAW_LIMITS = LimitProfile("raw", concurrency=16, min_interval=0.0, reserve=0)

MAX_RETRIES = 5
BACKOFF_BASE = 1.6
REQUEST_TIMEOUT = 30.0

# Quantos repositórios validar por query GraphQL. O limite prático é a
# complexidade da query (batch × manifestos = objetos resolvidos), não a cota.
# 20 × 15 manifestos = 300 objetos por requisição, abaixo do ponto em que a
# detecção de abuso do GitHub começa a responder 403.
GRAPHQL_BATCH_SIZE = 20


# --------------------------------------------------------------------------
# Layer 1 — validação de dependência
# --------------------------------------------------------------------------

# Monorepos são a maior fonte de falso negativo na validação: o backend Python
# vive em `backend/` ou `server/` e a raiz só tem package.json. Cada path extra
# aqui é mais um alias na *mesma* query GraphQL — custo marginal zero em
# requisições, e recupera repositórios que são FastAPI de verdade.
MANIFEST_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "setup.py",
    "setup.cfg",
    "requirements/base.txt",
    "requirements/prod.txt",
    "backend/pyproject.toml",
    "backend/requirements.txt",
    "server/pyproject.toml",
    "server/requirements.txt",
    "api/pyproject.toml",
    "api/requirements.txt",
    "app/requirements.txt",
    "src/pyproject.toml",
)

# Libs de ecossistema que viram `ecosystem_tags` no dependencies.json.
ECOSYSTEM_LIBS: tuple[str, ...] = (
    "pydantic",
    "pydantic-settings",
    "sqlalchemy",
    "sqlmodel",
    "alembic",
    "uvicorn",
    "gunicorn",
    "celery",
    "redis",
    "httpx",
    "pytest",
    "asyncpg",
    "psycopg2",
    "psycopg",
    "motor",
    "beanie",
    "tortoise-orm",
    "strawberry-graphql",
    "python-jose",
    "passlib",
    "authlib",
)


# --------------------------------------------------------------------------
# Layer 2 — seleção de arquivos de alto sinal
# --------------------------------------------------------------------------

# Cada papel mapeia para os padrões de path que o identificam. A ordem importa:
# o primeiro papel que casar vence (ver analysis/paths.py).
ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "test": (r"(^|/)tests?/", r"(^|/)test_[^/]+\.py$", r"[^/]+_test\.py$"),
    "entrypoint": (r"(^|/)(main|app|asgi|server)\.py$",),
    "router": (
        r"(^|/)(routers?|api|endpoints?|views?|controllers?)/",
        r"(^|/)(routes?|urls)\.py$",
    ),
    "schema": (r"(^|/)(schemas?|models?|dto|entities)/", r"(^|/)(schemas?|models?)\.py$"),
    "dependency": (r"(^|/)(dependencies|deps)(\.py|/)", r"(^|/)security\.py$"),
    "middleware": (
        r"(^|/)middlewares?(\.py|/)",
        r"(^|/)(exception_handlers?|exceptions?|errors?)\.py$",
    ),
    "config": (r"(^|/)(config|settings|core/config)\.py$", r"(^|/)core/"),
    "database": (r"(^|/)(database|db|session|crud|repository)(\.py|/)",),
}

# Paths que nunca valem o download, checados antes de qualquer papel.
EXCLUDE_PATTERNS: tuple[str, ...] = (
    r"(^|/)node_modules/",
    r"(^|/)\.venv/",
    r"(^|/)venv/",
    r"(^|/)site-packages/",
    r"(^|/)migrations?/versions/",  # revisions autogeradas pelo alembic
    r"(^|/)alembic/versions/",
    r"(^|/)vendor(ed)?/",
    r"(^|/)third_party/",
    r"(^|/)\.git/",
    r"(^|/)build/",
    r"(^|/)dist/",
    r"(^|/)docs?/",
    r"(^|/)examples?/vendor/",
    r"(^|/)__pycache__/",
    r"(^|/)static/",
    r"(^|/)assets/",
    r"(^|/)locale/",
    r"conftest\.py$",  # fixtures sem valor de padrão isolado
    r"(^|/)__init__\.py$",  # quase sempre vazio ou só re-exports
    r"(^|/)setup\.py$",
)

MAX_FILE_BYTES = 50 * 1024  # 50KB — acima disso é gerado ou é dump
MAX_FILES_PER_REPO = 40  # teto de download por repo

# Quota por papel *dentro* de cada repositório. Sem isso, um projeto com 200
# arquivos de teste e 5 routers gasta o teto todo em testes — e o dataset final
# herda esse viés. Balancear aqui, na seleção, é mais barato que balancear
# depois: os arquivos descartados nunca chegam a ser baixados.
ROLE_FILE_QUOTA: dict[str, int] = {
    "router": 12,
    "schema": 8,
    "test": 6,
    "dependency": 4,
    "database": 4,
    "entrypoint": 3,
    "middleware": 3,
    "config": 2,
}


# --------------------------------------------------------------------------
# Layer 3/4 — snippets e dataset
# --------------------------------------------------------------------------

MIN_SNIPPET_LINES = 3
MAX_SNIPPET_LINES = 120

PATTERN_TAGS: tuple[str, ...] = (
    "routing",
    "validation",
    "dependency_injection",
    "error_handling",
    "async",
    "config",
    "testing",
    "database",
)

# Balanceamento estratificado: teto por tag no dataset final. Evita que
# `testing` (naturalmente abundante) domine o treino.
MAX_PER_TAG = 900
VAL_SPLIT_RATIO = 0.15
RANDOM_SEED = 1337


@dataclass
class RuntimeOptions:
    """Flags de execução vindas da CLI."""

    tiers: tuple[int, ...] = (1, 2, 3)
    limit: int | None = None
    resume: bool = True
    concurrency_scale: float = 1.0
    extra: dict = field(default_factory=dict)
