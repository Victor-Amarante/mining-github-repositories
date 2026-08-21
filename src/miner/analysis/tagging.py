"""Detecção de padrões FastAPI em um snippet (Layer 3).

Tagging é feito sobre a AST, não sobre regex de texto: `Depends(` dentro de
uma string ou de um comentário não é injeção de dependência. Cada detector
devolve um peso — quanto mais evidência, maior a confiança de que aquele é
o padrão *principal* do snippet.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}

_PYDANTIC_BASES = {"BaseModel", "RootModel", "GenericModel"}
_SETTINGS_BASES = {"BaseSettings"}
_VALIDATOR_DECORATORS = {
    "validator",
    "field_validator",
    "model_validator",
    "root_validator",
    "computed_field",
}
_DB_NAMES = {
    "Session",
    "AsyncSession",
    "sessionmaker",
    "async_sessionmaker",
    "declarative_base",
    "create_engine",
    "create_async_engine",
    "select",
    "SQLModel",
    "relationship",
    "mapped_column",
}
_TEST_NAMES = {"TestClient", "AsyncClient", "pytest", "fixture", "AsyncTestClient"}
_AUTH_NAMES = {
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "HTTPBearer",
    "HTTPBasic",
    "jwt",
    "CryptContext",
    "APIKeyHeader",
    "SecurityScopes",
}

# Desempate quando duas tags empatam em evidência. Padrões mais específicos
# ganham: um endpoint que usa Depends ensina mais sobre DI do que sobre rota.
_SPECIFICITY = [
    "dependency_injection",
    "validation",
    "error_handling",
    "database",
    "config",
    "testing",
    "routing",
    "async",
]


@dataclass
class TagResult:
    primary: str
    secondary: list[str]
    is_async: bool


def _decorator_name(node: ast.expr) -> tuple[str, str]:
    """Extrai (objeto, atributo) de um decorator. `@router.get(...)` -> (router, get)."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        base = target.value
        obj = base.id if isinstance(base, ast.Name) else ""
        return obj, target.attr
    if isinstance(target, ast.Name):
        return "", target.id
    return "", ""


def _called_names(tree: ast.AST) -> set[str]:
    """Nomes efetivamente *chamados* ou referenciados na AST."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def analyze(node: ast.AST, source: str) -> TagResult | None:
    """Pontua cada padrão presente e escolhe o principal.

    Devolve `None` quando o snippet não apresenta nenhum padrão reconhecível —
    o chamador deve descartá-lo em vez de rotulá-lo por omissão.
    """
    scores: dict[str, int] = {}
    topics: set[str] = set()

    def bump(tag: str, weight: int = 1) -> None:
        scores[tag] = scores.get(tag, 0) + weight

    names = _called_names(node)
    is_async = any(
        isinstance(n, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith))
        for n in ast.walk(node)
    )

    # --- routing --------------------------------------------------------
    decorators: list[ast.expr] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        decorators = list(node.decorator_list)
    has_route_decorator = False
    for dec in decorators:
        obj, attr = _decorator_name(dec)
        if attr in _HTTP_METHODS and obj:
            # Um decorator de rota é decisivo: a função *é* o endpoint. O que
            # ela usa por dentro (Depends, sessão, HTTPException) é consumo de
            # outros padrões, não a definição deles.
            has_route_decorator = True
            bump("routing", 6)
        elif attr in _VALIDATOR_DECORATORS:
            bump("validation", 3)
        elif attr in {"exception_handler", "errorhandler"}:
            bump("error_handling", 3)
        elif attr in {"middleware",}:
            bump("error_handling", 1)
        elif attr in {"fixture", "mark", "parametrize"}:
            bump("testing", 2)
    if {"APIRouter", "include_router"} & names:
        bump("routing", 2)
    if "FastAPI" in names:
        bump("routing", 1)

    # --- dependency injection -------------------------------------------
    di_hits = sum(
        1
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"Depends", "Security"}
    )
    if di_hits:
        # Num handler de rota, `Depends()` é uso idiomático — pontua como
        # padrão secundário. Fora de rota, a função é a própria dependência
        # (um provider), e aí o padrão principal é injeção de dependência.
        bump("dependency_injection", (1 + di_hits) if has_route_decorator else (2 + di_hits))
    if "Annotated" in names and di_hits:
        bump("dependency_injection", 1)

    # --- validation ------------------------------------------------------
    if isinstance(node, ast.ClassDef):
        base_names = {b.id for b in node.bases if isinstance(b, ast.Name)}
        base_names |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
        if base_names & _PYDANTIC_BASES:
            bump("validation", 4)
        if base_names & _SETTINGS_BASES:
            bump("config", 5)
    if {"Field", "constr", "conint", "EmailStr", "ConfigDict"} & names:
        bump("validation", 2)

    # --- error handling ---------------------------------------------------
    if "HTTPException" in names:
        bump("error_handling", 3)
    handlers = sum(1 for n in ast.walk(node) if isinstance(n, ast.ExceptHandler))
    if handlers:
        bump("error_handling", 1 + handlers)
    if {"JSONResponse", "status"} & names and "HTTPException" in names:
        bump("error_handling", 1)

    # --- config ------------------------------------------------------------
    if {"BaseSettings", "SettingsConfigDict", "lru_cache"} & names:
        bump("config", 2)
    if {"getenv", "environ"} & names:
        bump("config", 1)

    # --- database -----------------------------------------------------------
    db_hits = len(_DB_NAMES & names)
    if db_hits:
        bump("database", 1 + db_hits)

    # --- testing -------------------------------------------------------------
    test_hits = len(_TEST_NAMES & names)
    if test_hits:
        bump("testing", 1 + test_hits)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
        bump("testing", 3)
    if any(isinstance(n, ast.Assert) for n in ast.walk(node)):
        bump("testing", 1)

    # --- async ---------------------------------------------------------------
    if is_async:
        bump("async", 2)

    # --- tópicos transversais (viram secondary_tags, nunca primary) ----------
    if _AUTH_NAMES & names or any(
        kw in source.lower() for kw in ("oauth2", "jwt", "access_token")
    ):
        topics.add("auth")
    if {"BackgroundTasks", "WebSocket", "StreamingResponse"} & names:
        topics.add("streaming")
    if {"UploadFile", "File", "Form"} & names:
        topics.add("file_upload")

    if not scores:
        # Sem evidência de padrão, não há tag. Inventar uma (o antigo fallback
        # para "routing"/"async") produzia rótulo falso — um utilitário de CLI
        # descrito como endpoint FastAPI. Para finetuning isso é pior que
        # descartar o exemplo: ensina o modelo a associação errada.
        return None

    def rank(tag: str) -> tuple[int, int]:
        return (-scores[tag], _SPECIFICITY.index(tag) if tag in _SPECIFICITY else 99)

    ordered = sorted(scores, key=rank)
    primary = ordered[0]
    secondary = [t for t in ordered[1:] if scores[t] >= 2] + sorted(topics)

    return TagResult(primary=primary, secondary=secondary[:4], is_async=is_async)
