"""Checkpoints de execução.

Mining é um job longo e falível — token expira, rede cai, o GitHub devolve
502. Cada layer grava seu resultado em disco e relê no rerun, então uma
falha na Layer 3 nunca custa as chamadas de API já pagas nas Layers 1 e 2.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .config import STATE_DIR


def _path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def save(name: str, payload: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = _path(name)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)  # atômico: nunca deixa um checkpoint meio escrito


def load(name: str, default: Any = None) -> Any:
    target = _path(name)
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def exists(name: str) -> bool:
    return _path(name).exists()


def clear(name: str) -> None:
    _path(name).unlink(missing_ok=True)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Streaming linha a linha — o dataset nunca precisa caber em memória."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
