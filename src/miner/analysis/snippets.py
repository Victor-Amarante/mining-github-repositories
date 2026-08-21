"""Extração de snippets a partir da AST (Layer 3).

Requisito do design: "contexto mínimo: imports + função/classe completa, não
corte cego". Por isso o corte é feito por nó da AST — `node.lineno` até
`node.end_lineno`, incluindo decorators — e os imports anexados são apenas
os que o snippet realmente referencia.
"""

from __future__ import annotations

import ast
import hashlib
import io
import math
import tokenize
import warnings
from typing import Iterator

from ..config import MAX_SNIPPET_LINES, MIN_SNIPPET_LINES
from ..models import ExtractedFile, Snippet
from .tagging import TagResult, analyze

# Nomes que, atribuídos em nível de módulo, valem um snippet próprio:
# são a criação da aplicação/router, padrão canônico de entrypoint.
_MODULE_LEVEL_FACTORIES = {"FastAPI", "APIRouter", "Settings", "create_engine"}

# Validar FastAPI no nível do *repositório* não garante relevância no nível do
# *arquivo*: um repo que declara fastapi também tem CLI, scripts e utilitários.
# Sem este portão, `cli/main.py` casa com o padrão de entrypoint e entra no
# dataset como se fosse uma aplicação — rotulado com um padrão que não usa.
_ECOSYSTEM_ROOTS = frozenset(
    {
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic_settings",
        "sqlmodel",
        "sqlalchemy",
        "strawberry",
        "beanie",
        "tortoise",
        "motor",
    }
)
# Testes de app FastAPI costumam usar httpx.AsyncClient + ASGITransport sem
# importar fastapi diretamente.
_TEST_EXTRA_ROOTS = frozenset({"httpx", "pytest_asyncio", "asgi_lifespan"})


def _import_roots(tree: ast.Module) -> set[str]:
    """Módulos-raiz importados em nível de arquivo."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def is_relevant(tree: ast.Module, role: str) -> bool:
    """O arquivo usa de fato o ecossistema FastAPI?"""
    roots = _import_roots(tree)
    allowed = _ECOSYSTEM_ROOTS | (_TEST_EXTRA_ROOTS if role == "test" else frozenset())
    return bool(roots & allowed) or any(r.startswith("fastapi") for r in roots)


def _import_alias_map(tree: ast.Module, lines: list[str]) -> dict[str, str]:
    """Mapeia cada nome importado para a linha de import que o trouxe."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        stmt = "\n".join(lines[node.lineno - 1 : (node.end_lineno or node.lineno)]).strip()
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name.split(".", 1)[0]
            aliases[local] = stmt
    return aliases


def _referenced_imports(node: ast.AST, aliases: dict[str, str]) -> list[str]:
    """Só os imports que o snippet de fato usa — sem poluir o contexto."""
    used: list[str] = []
    seen: set[str] = set()
    for sub in ast.walk(node):
        name = sub.id if isinstance(sub, ast.Name) else None
        if name is None and isinstance(sub, ast.Attribute):
            base = sub.value
            name = base.id if isinstance(base, ast.Name) else None
        if name and name in aliases and aliases[name] not in seen:
            seen.add(aliases[name])
            used.append(aliases[name])
    return used


def _node_span(node: ast.stmt) -> tuple[int, int]:
    """Linha inicial real, contando decorators (que ficam acima do `def`)."""
    start = node.lineno
    decorators = getattr(node, "decorator_list", [])
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    return start, node.end_lineno or node.lineno


def normalize_for_dedup(code: str) -> str:
    """Normaliza o código para dedupe estrutural.

    Remove comentários e colapsa espaçamento via tokenizer — assim dois forks
    do mesmo boilerplate com indentação/comentários diferentes colidem no
    mesmo hash, que é exatamente o caso que mais polui esse tipo de dataset.
    """
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        parts = [
            tok.string
            for tok in tokens
            if tok.type
            not in (
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
            )
        ]
        return " ".join(p.strip() for p in parts if p.strip())
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return " ".join(code.split())


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _quality(
    *, stars: int, tier: int, has_docstring: bool, annotated: bool, n_lines: int
) -> float:
    """Score usado para escolher os melhores snippets no balanceamento.

    Estrelas entram em log: a diferença entre 50 e 500 estrelas importa muito
    mais que entre 50.000 e 60.000.
    """
    score = math.log10(max(stars, 1) + 1) / 5.0  # ~0..1
    score += {1: 0.30, 2: 0.20, 3: 0.10}.get(tier, 0.0)
    score += 0.20 if has_docstring else 0.0
    score += 0.15 if annotated else 0.0
    # Snippets muito curtos ou muito longos ensinam menos.
    if 8 <= n_lines <= 60:
        score += 0.15
    return score


def _is_annotated(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = sub.args
            if sub.returns is not None:
                return True
            if any(a.annotation is not None for a in args.args + args.kwonlyargs):
                return True
        if isinstance(sub, ast.AnnAssign):
            return True
    return False


def _candidate_nodes(tree: ast.Module) -> Iterator[ast.stmt]:
    """Nós de nível de módulo que valem virar snippet."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _MODULE_LEVEL_FACTORIES:
                yield node


def extract_from_file(
    file: ExtractedFile, *, stars: int, tier: int
) -> list[Snippet]:
    """Converte um arquivo baixado em zero ou mais snippets tagueados."""
    try:
        # Código de terceiros dispara SyntaxWarning (escape inválido em regex,
        # `is` com literal). São avisos sobre o código minerado, não sobre o
        # nosso — silenciá-los evita poluir o log com centenas de linhas.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            tree = ast.parse(file.content)
    except (SyntaxError, ValueError, RecursionError):
        return []  # Python 2, template Jinja com .py, arquivo truncado

    if not is_relevant(tree, file.role):
        return []

    lines = file.content.splitlines()
    aliases = _import_alias_map(tree, lines)
    snippets: list[Snippet] = []

    for node in _candidate_nodes(tree):
        start, end = _node_span(node)
        n_lines = end - start + 1
        if n_lines < MIN_SNIPPET_LINES or n_lines > MAX_SNIPPET_LINES:
            continue

        code = "\n".join(lines[start - 1 : end]).rstrip()
        if not code.strip():
            continue

        tags = analyze(node, code)
        if tags is None:
            # Arquivo relevante, mas este trecho não exibe padrão algum
            # (helper puro, constante, utilitário). Num arquivo de teste ele
            # ainda ensina estrutura de teste; fora disso, não entra.
            if file.role != "test":
                continue
            tags = TagResult(primary="testing", secondary=[], is_async=False)

        has_docstring = (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and ast.get_docstring(node) is not None
        )
        # Arquivo de teste ensina testing, independente do que mais tenha dentro.
        primary = "testing" if file.role == "test" else tags.primary

        dedup = _hash(normalize_for_dedup(code))
        snippets.append(
            Snippet(
                snippet_id=_hash(f"{file.repo_id}:{file.path}:{start}:{dedup}"),
                repo_id=file.repo_id,
                file_path=file.path,
                role=file.role,
                tag=primary,
                secondary_tags=[t for t in tags.secondary if t != primary],
                code=code,
                context_imports=_referenced_imports(node, aliases),
                line_start=start,
                line_end=end,
                repo_stars=stars,
                tier=tier,
                dedup_hash=dedup,
                symbol=getattr(node, "name", "") or _assign_target(node),
                is_async=tags.is_async,
                has_docstring=has_docstring,
                quality=_quality(
                    stars=stars,
                    tier=tier,
                    has_docstring=has_docstring,
                    annotated=_is_annotated(node),
                    n_lines=n_lines,
                ),
            )
        )

    return snippets


def _assign_target(node: ast.stmt) -> str:
    if isinstance(node, ast.Assign) and node.targets:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return ""
