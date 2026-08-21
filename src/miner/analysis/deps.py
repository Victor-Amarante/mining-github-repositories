"""Parsing de manifestos de dependência (Layer 1).

O objetivo não é resolver dependências com fidelidade de packaging — é
responder rápido a uma pergunta binária: *este repo declara FastAPI?*.
Um `import fastapi` solto num README não conta; uma linha de requirements
ou uma entrada de pyproject conta.
"""

from __future__ import annotations

import re
import tomllib
from typing import Iterable

from ..config import ECOSYSTEM_LIBS
from ..models import Dependencies

# `fastapi[all]>=0.100.0 ; python_version >= "3.9"` -> ("fastapi", ">=0.100.0")
_REQ_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[^\]]*\])?"
    r"\s*(?P<spec>[<>=!~^][^;#]*)?"
)

_PEP508_IN_LIST = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?P<spec>.*)$")


def normalize_name(name: str) -> str:
    """PEP 503: nomes de pacote são case-insensitive e `-`/`_`/`.` equivalem."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_requirements(text: str) -> dict[str, str]:
    deps: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-", "http", "git+", ".")):
            # `-r other.txt`, `-e .` e URLs diretas não declaram nome utilizável
            continue
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        match = _REQ_LINE.match(line)
        if match:
            deps[normalize_name(match.group("name"))] = (match.group("spec") or "*").strip()
    return deps


def parse_pyproject(text: str) -> dict[str, str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return {}

    deps: dict[str, str] = {}
    project = data.get("project", {})

    # PEP 621: project.dependencies = ["fastapi>=0.100"]
    for entry in _as_str_list(project.get("dependencies")):
        match = _PEP508_IN_LIST.match(entry)
        if match:
            deps[normalize_name(match.group("name"))] = (match.group("spec") or "*").strip() or "*"

    # PEP 621 opcionais
    for group in (project.get("optional-dependencies") or {}).values():
        for entry in _as_str_list(group):
            match = _PEP508_IN_LIST.match(entry)
            if match:
                deps.setdefault(
                    normalize_name(match.group("name")),
                    (match.group("spec") or "*").strip() or "*",
                )

    # Poetry: tool.poetry.dependencies = { fastapi = "^0.115" }
    poetry = data.get("tool", {}).get("poetry", {})
    for section in ("dependencies", "dev-dependencies"):
        for name, spec in (poetry.get(section) or {}).items():
            if normalize_name(name) == "python":
                continue
            deps[normalize_name(name)] = _stringify_spec(spec)
    for group in (poetry.get("group") or {}).values():
        for name, spec in (group.get("dependencies") or {}).items():
            if normalize_name(name) == "python":
                continue
            deps.setdefault(normalize_name(name), _stringify_spec(spec))

    # PDM/uv/hatch dev groups
    for name_list in (data.get("dependency-groups") or {}).values():
        for entry in _as_str_list(name_list):
            match = _PEP508_IN_LIST.match(entry)
            if match:
                deps.setdefault(normalize_name(match.group("name")), "*")

    return deps


def parse_pipfile(text: str) -> dict[str, str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    deps: dict[str, str] = {}
    for section in ("packages", "dev-packages"):
        for name, spec in (data.get(section) or {}).items():
            deps[normalize_name(name)] = _stringify_spec(spec)
    return deps


def parse_setup_py(text: str) -> dict[str, str]:
    """Best-effort: extrai a lista literal de `install_requires`."""
    match = re.search(r"install_requires\s*=\s*\[(?P<body>.*?)\]", text, re.DOTALL)
    if not match:
        return {}
    deps: dict[str, str] = {}
    for entry in re.findall(r"['\"]([^'\"]+)['\"]", match.group("body")):
        item = _PEP508_IN_LIST.match(entry)
        if item:
            deps[normalize_name(item.group("name"))] = (item.group("spec") or "*").strip() or "*"
    return deps


def parse_setup_cfg(text: str) -> dict[str, str]:
    match = re.search(r"install_requires\s*=\s*(?P<body>(?:\n\s+.+)+)", text)
    if not match:
        return {}
    return parse_requirements(match.group("body"))


_PARSERS = {
    "pyproject.toml": parse_pyproject,
    "requirements.txt": parse_requirements,
    "Pipfile": parse_pipfile,
    "setup.py": parse_setup_py,
    "setup.cfg": parse_setup_cfg,
}


def parse_manifest(filename: str, text: str) -> dict[str, str]:
    base = filename.rsplit("/", 1)[-1]
    if base.endswith(".txt"):
        return parse_requirements(text)
    parser = _PARSERS.get(base)
    return parser(text) if parser else {}


def build_dependencies(manifests: dict[str, str]) -> Dependencies | None:
    """Consolida todos os manifestos achados. Devolve None se não houver FastAPI.

    Quando mais de um manifesto declara fastapi, o `source_file` reportado é
    o de maior autoridade (pyproject > requirements > Pipfile > setup).
    """
    priority = ["pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "setup.cfg"]

    merged: dict[str, str] = {}
    source_file: str | None = None
    best_rank = len(priority) + 1

    for filename, text in manifests.items():
        if not text:
            continue
        parsed = parse_manifest(filename, text)
        if not parsed:
            continue
        for name, spec in parsed.items():
            merged.setdefault(name, spec)
        if "fastapi" in parsed:
            base = filename.rsplit("/", 1)[-1]
            rank = priority.index(base) if base in priority else len(priority)
            if rank < best_rank:
                best_rank = rank
                source_file = filename

    if source_file is None:
        return None

    return Dependencies(
        source_file=source_file,
        fastapi_version=merged.get("fastapi") or None,
        dependencies=merged,
        ecosystem_tags=sorted(
            {lib for lib in ECOSYSTEM_LIBS if normalize_name(lib) in merged}
        ),
    )


def _stringify_spec(spec: object) -> str:
    """Poetry aceita `"^1.0"` ou `{version = "^1.0", extras = [...]}`."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return str(spec.get("version", "*"))
    return "*"


def _as_str_list(value: object) -> Iterable[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []
