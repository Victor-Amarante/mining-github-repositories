"""Testes das heurísticas de análise.

Estas são as partes do pipeline que regridem em silêncio: um ajuste de peso
no tagging ou um padrão de path a mais não quebram nada visivelmente — só
degradam a qualidade do dataset algumas centenas de exemplos depois. Cada
teste aqui corresponde a um defeito real encontrado durante a construção.
"""

from __future__ import annotations

import pytest

from miner.analysis.deps import build_dependencies, parse_pyproject, parse_requirements
from miner.analysis.paths import classify_role, select_files
from miner.analysis.snippets import extract_from_file, normalize_for_dedup
from miner.models import ExtractedFile

ROUTER_SOURCE = '''
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, field_validator
from .db import get_session

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    """Payload de criação."""
    email: str = Field(..., max_length=255)
    age: int

    @field_validator("age")
    @classmethod
    def check_age(cls, v: int) -> int:
        if v < 0:
            raise ValueError("negativa")
        return v


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    """Valida o token."""
    user = await decode(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=UserRead)
async def create_user(
    payload: UserCreate,
    session: AsyncSession = Depends(get_session),
) -> UserRead:
    """Cria um usuario."""
    user = User(**payload.model_dump())
    session.add(user)
    await session.commit()
    return user
'''

# Arquivo de CLI num repo que declara FastAPI — o falso positivo que motivou
# o portão de relevância da Layer 3.
CLI_SOURCE = '''
import click


def get_version() -> str:
    """Get the current version."""
    try:
        from app._version import __version__
        return __version__
    except ImportError:
        return "unknown"


def _apply_help_aliases(command: click.Command) -> None:
    """Ensure -? works everywhere."""
    settings = dict(command.context_settings or {})
    settings["help_option_names"] = ["--help", "-?"]
    command.context_settings = settings
'''


def _extract(source: str, path: str, role: str, stars: int = 5000, tier: int = 1):
    return extract_from_file(
        ExtractedFile(repo_id="o__r", path=path, role=role, content=source, blob_sha="x"),
        stars=stars,
        tier=tier,
    )


def _by_symbol(snippets):
    return {s.symbol: s for s in snippets}


# ---------------------------------------------------------------------------
# Manifestos
# ---------------------------------------------------------------------------


def test_parse_pep621_and_optional_deps():
    deps = parse_pyproject(
        '[project]\nname = "x"\n'
        'dependencies = ["fastapi[all]>=0.115.0", "uvicorn"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n'
    )
    assert deps["fastapi"] == ">=0.115.0"  # o extra `[all]` não vira parte do nome
    assert deps["uvicorn"] == "*"
    assert "pytest" in deps


def test_parse_poetry_dict_spec():
    deps = parse_pyproject(
        '[tool.poetry.dependencies]\npython = "^3.12"\n'
        'fastapi = "^0.115.0"\npydantic = {version = "^2.9", extras = ["email"]}\n'
    )
    assert deps["fastapi"] == "^0.115.0"
    assert deps["pydantic"] == "^2.9"  # forma de dict resolvida para a versão
    assert "python" not in deps


def test_parse_requirements_ignores_directives_and_markers():
    deps = parse_requirements(
        "fastapi==0.111.0  # comentário\n-r base.txt\n"
        "SQLAlchemy>=2.0.0 ; python_version>='3.9'\n\n"
    )
    assert deps == {"fastapi": "==0.111.0", "sqlalchemy": ">=2.0.0"}


def test_build_dependencies_rejects_repo_without_fastapi():
    assert build_dependencies({"requirements.txt": "flask==3.0\nrequests\n"}) is None


def test_build_dependencies_prefers_pyproject_as_source():
    result = build_dependencies(
        {"requirements.txt": "fastapi==0.111.0\n", "pyproject.toml": '[project]\ndependencies = ["fastapi>=0.115"]\n'}
    )
    assert result is not None
    assert result.source_file == "pyproject.toml"


def test_ecosystem_tags_derived_from_deps():
    result = build_dependencies(
        {"pyproject.toml": '[project]\ndependencies = ["fastapi", "sqlalchemy", "pytest"]\n'}
    )
    assert result is not None
    assert set(result.ecosystem_tags) >= {"sqlalchemy", "pytest"}


# ---------------------------------------------------------------------------
# Classificação de paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("app/main.py", "entrypoint"),
        ("app/routers/users.py", "router"),
        ("app/schemas/user.py", "schema"),
        ("app/dependencies.py", "dependency"),
        ("app/core/config.py", "config"),
        ("app/db/session.py", "database"),
        ("app/middleware.py", "middleware"),
        # `tests/` vence sobre `routers/`: é um teste, não um router.
        ("tests/routers/test_users.py", "test"),
        ("node_modules/x/y.py", None),
        ("alembic/versions/abc_init.py", None),
        ("app/__init__.py", None),
        ("README.md", None),
    ],
)
def test_classify_role(path, expected):
    assert classify_role(path) == expected


def test_select_files_drops_oversized_blobs():
    tree = [
        {"type": "blob", "path": "app/main.py", "sha": "a", "size": 1000},
        {"type": "blob", "path": "app/routers/big.py", "sha": "b", "size": 999_999},
        {"type": "tree", "path": "app", "sha": "t"},
    ]
    assert [e.path for e in select_files(tree)] == ["app/main.py"]


def test_select_files_enforces_role_quota():
    """Um repo só de testes não deve gastar o teto todo em testes."""
    tree = [
        {"type": "blob", "path": f"tests/test_{i}.py", "sha": str(i), "size": 500}
        for i in range(50)
    ]
    selected = select_files(tree)
    assert len(selected) == 6  # ROLE_FILE_QUOTA["test"]


# ---------------------------------------------------------------------------
# Extração de snippets
# ---------------------------------------------------------------------------


def test_snippet_cut_includes_decorator_and_full_body():
    """O corte vai do decorator ao fim do nó — nunca truncado no meio."""
    snippet = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/users.py", "router"))["create_user"]
    assert snippet.code.startswith("@router.post(")
    assert snippet.code.rstrip().endswith("return user")


def test_every_snippet_is_valid_python():
    import ast

    for snippet in _extract(ROUTER_SOURCE, "app/routers/users.py", "router"):
        ast.parse(snippet.code)  # não levanta


def test_context_imports_only_include_referenced_modules():
    snippet = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/users.py", "router"))["UserCreate"]
    joined = " ".join(snippet.context_imports)
    assert "pydantic" in joined
    assert "sqlalchemy" not in joined  # UserCreate não usa AsyncSession


def test_route_handler_is_routing_not_dependency_injection():
    """Um endpoint que consome `Depends()` é uma rota, não uma dependência."""
    snippet = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/users.py", "router"))["create_user"]
    assert snippet.tag == "routing"
    assert "dependency_injection" in snippet.secondary_tags


def test_provider_without_route_decorator_is_dependency_injection():
    snippets = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/users.py", "router"))
    assert snippets["get_current_user"].tag == "dependency_injection"
    assert "auth" in snippets["get_current_user"].secondary_tags


def test_pydantic_model_is_validation():
    snippet = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/users.py", "router"))["UserCreate"]
    assert snippet.tag == "validation"


def test_irrelevant_file_is_dropped_entirely():
    """Repo valida FastAPI, mas este arquivo é um CLI Click — não entra."""
    assert _extract(CLI_SOURCE, "app/cli/main.py", "entrypoint") == []


def test_no_tag_is_invented_for_patternless_code():
    source = "import fastapi\n\n\ndef add(a, b):\n    total = a + b\n    return total\n"
    assert _extract(source, "app/helpers.py", "router") == []


def test_test_role_forces_testing_tag():
    snippets = _extract(ROUTER_SOURCE, "tests/test_users.py", "test")
    assert snippets and {s.tag for s in snippets} == {"testing"}


def test_dedup_hash_ignores_comments_and_whitespace():
    a = "def f(x):\n    # comentário\n    return x + 1\n"
    b = "def f(x):\n        return x   +   1\n"
    assert normalize_for_dedup(a) == normalize_for_dedup(b)


def test_dedup_hash_distinguishes_different_code():
    a = "def f(x):\n    return x + 1\n"
    b = "def f(x):\n    return x + 2\n"
    assert normalize_for_dedup(a) != normalize_for_dedup(b)


def test_quality_rewards_stars_and_tier():
    high = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/u.py", "router", stars=50_000, tier=1))
    low = _by_symbol(_extract(ROUTER_SOURCE, "app/routers/u.py", "router", stars=30, tier=3))
    assert high["create_user"].quality > low["create_user"].quality
