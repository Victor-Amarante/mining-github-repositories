"""LAYER 4 — Store & Prepare for Finetuning.

Transforma snippets tagueados em exemplos de treino: instrução, spec SDD e
balanceamento estratificado.

A geração de instrução/spec é **determinística por template**, não via LLM.
Isso mantém o pipeline reprodutível, gratuito e rápido — e, mais importante,
evita injetar alucinação de um modelo nos rótulos que vão treinar outro.
Os templates são preenchidos com fatos extraídos do próprio código (método
HTTP, rota, nome do símbolo, libs em uso), então a instrução descreve o que
o snippet de fato faz.
"""

from __future__ import annotations

import logging
import random
import re
from collections import Counter, defaultdict

from .. import state
from ..config import (
    DATASET_FILE,
    DATASET_STATS_FILE,
    DATASET_TRAIN_FILE,
    DATASET_VAL_FILE,
    MAX_PER_TAG,
    RANDOM_SEED,
    VAL_SPLIT_RATIO,
)
from ..models import DatasetRecord, Snippet
from ..reporting import progress_bar

log = logging.getLogger("miner.layer4")

_ROUTE_DECORATOR = re.compile(
    r"@(?P<obj>\w+)\.(?P<method>get|post|put|delete|patch|options|head)\(\s*"
    r"[\"'](?P<path>[^\"']*)[\"']"
)

_TAG_PT = {
    "routing": "roteamento",
    "validation": "validação de dados",
    "dependency_injection": "injeção de dependência",
    "error_handling": "tratamento de erros",
    "async": "execução assíncrona",
    "config": "configuração",
    "testing": "testes",
    "database": "persistência",
}


def _route_info(code: str) -> tuple[str, str] | None:
    match = _ROUTE_DECORATOR.search(code)
    if not match:
        return None
    return match.group("method").upper(), match.group("path") or "/"


def _instruction(snippet: Snippet) -> str:
    """Instrução específica, derivada de fatos do código."""
    symbol = snippet.symbol or "o componente"
    route = _route_info(snippet.code)
    # Concordância: o adjetivo acompanha o substantivo de cada template.
    masc = " assíncrono" if snippet.is_async else ""
    fem = " assíncrona" if snippet.is_async else ""

    if snippet.tag == "routing" and route:
        method, path = route
        return (
            f"Implemente um endpoint {method}{masc} em `{path}` usando FastAPI, "
            f"na função `{symbol}`."
        )
    if snippet.tag == "routing":
        return f"Implemente o roteamento FastAPI em `{symbol}` usando APIRouter."
    if snippet.tag == "validation":
        return f"Defina o schema Pydantic `{symbol}` com as validações necessárias."
    if snippet.tag == "dependency_injection":
        return (
            f"Implemente a dependência{fem} `{symbol}` para ser injetada via "
            f"`Depends()` em rotas FastAPI."
        )
    # Instrução e spec precisam concordar com o que o código realmente usa —
    # ver `_spec_sdd`. Prometer `HTTPException` num `try/except` comum, ou
    # `pydantic-settings` num getter de env, é rótulo errado.
    if snippet.tag == "error_handling":
        mecanismo = (
            "`HTTPException`" if "HTTPException" in snippet.code else "tratamento de exceções"
        )
        return f"Implemente o tratamento de erros em `{symbol}` usando {mecanismo}."
    if snippet.tag == "config":
        origem = (
            "com pydantic-settings"
            if "BaseSettings" in snippet.code
            else "a partir de variáveis de ambiente"
        )
        return f"Implemente a configuração em `{symbol}` {origem}."
    if snippet.tag == "database":
        return f"Implemente o acesso a dados{masc} em `{symbol}` com SQLAlchemy."
    if snippet.tag == "testing":
        return f"Escreva o teste `{symbol}` para uma aplicação FastAPI."
    if snippet.tag == "async":
        return f"Implemente `{symbol}` de forma assíncrona em uma aplicação FastAPI."
    return f"Implemente `{symbol}` seguindo as boas práticas de FastAPI."


def _spec_sdd(snippet: Snippet) -> dict[str, str]:
    """Spec no formato do design: objetivo, padrão aplicado, quando usar."""
    symbol = snippet.symbol or "o componente"
    route = _route_info(snippet.code)
    tag_pt = _TAG_PT.get(snippet.tag, snippet.tag)

    if snippet.tag == "routing" and route:
        method, path = route
        objetivo = f"Expor a operação {method} em `{path}` na API."
        padrao = "Decorator de rota do FastAPI com response_model e tipagem explícita"
        quando = f"Quando a API precisar responder {method} em `{path}`."
    elif snippet.tag == "validation":
        objetivo = f"Modelar e validar os dados de `{symbol}` na fronteira da API."
        padrao = "Pydantic BaseModel para validação declarativa de entrada/saída"
        quando = "Em qualquer payload que entre ou saia da API e precise de contrato."
    elif snippet.tag == "dependency_injection":
        objetivo = f"Fornecer `{symbol}` às rotas de forma desacoplada e testável."
        padrao = "`Depends()` para injeção de dependência"
        quando = "Quando várias rotas compartilham a mesma pré-condição ou recurso."
    elif snippet.tag == "error_handling":
        if "HTTPException" in snippet.code:
            objetivo = f"Converter falhas em `{symbol}` em respostas HTTP consistentes."
            padrao = "HTTPException com status code semântico"
            quando = "Sempre que uma falha precisar virar resposta previsível para o cliente."
        else:
            objetivo = f"Tratar as falhas previsíveis de `{symbol}` sem quebrar o fluxo."
            padrao = "Bloco try/except com tratamento explícito por tipo de exceção"
            quando = "Quando a operação pode falhar por causas conhecidas e recuperáveis."
    elif snippet.tag == "config":
        objetivo = "Centralizar configuração da aplicação com validação e origem em env."
        # Só afirma pydantic-settings se o código realmente usar BaseSettings.
        padrao = (
            "pydantic-settings (BaseSettings) para configuração tipada"
            if "BaseSettings" in snippet.code
            else "Configuração centralizada com valores padrão e origem em ambiente"
        )
        quando = "Ao ler variáveis de ambiente que precisam de tipo e valor padrão."
    elif snippet.tag == "database":
        objetivo = f"Implementar a persistência de `{symbol}`."
        padrao = (
            "Sessão SQLAlchemy gerenciada por dependência"
            if "Session" in snippet.code
            else "Acesso a dados via camada de persistência"
        )
        quando = "Em operações que leem ou escrevem no banco dentro de uma request."
    elif snippet.tag == "testing":
        objetivo = f"Verificar o comportamento coberto por `{symbol}`."
        padrao = "pytest com TestClient do FastAPI"
        quando = "Ao garantir contrato de rota e regressão de comportamento."
    elif snippet.tag == "routing":
        objetivo = f"Registrar e organizar rotas em `{symbol}`."
        padrao = "APIRouter para modularizar o roteamento da aplicação"
        quando = "Ao agrupar rotas relacionadas em um módulo próprio."
    elif snippet.tag == "async":
        objetivo = f"Implementar `{symbol}` sem bloquear o event loop."
        padrao = "Corrotina `async def` com I/O aguardado via `await`"
        quando = "Em operações de I/O dentro de uma aplicação assíncrona."
    else:
        objetivo = f"Implementar `{symbol}` aplicando {tag_pt}."
        padrao = f"Padrão de {tag_pt} em aplicações FastAPI"
        quando = f"Quando o componente precisar de {tag_pt}."

    if snippet.is_async and snippet.tag != "async":
        padrao += " (handler assíncrono)"

    return {"objetivo": objetivo, "padrao_aplicado": padrao, "quando_usar": quando}


def _to_record(snippet: Snippet, index: int, repo_full_name: str) -> DatasetRecord:
    imports = "\n".join(snippet.context_imports)
    return DatasetRecord(
        id=f"ft_{index:06d}",
        instruction=_instruction(snippet),
        # `input` carrega o contexto de imports disponível — é o campo de
        # contexto opcional do formato instruction/input/output.
        input=f"# Imports disponíveis:\n{imports}" if imports else "",
        output=snippet.code,
        spec_sdd=_spec_sdd(snippet),
        tags=[snippet.tag, *snippet.secondary_tags],
        tier=snippet.tier,
        source_repo=repo_full_name,
        source_snippet_id=snippet.snippet_id,
        metadata={
            "file_path": snippet.file_path,
            "role": snippet.role,
            "repo_stars": snippet.repo_stars,
            "is_async": snippet.is_async,
            "lines": snippet.line_end - snippet.line_start + 1,
            "quality": round(snippet.quality, 4),
        },
    )


def adaptive_cap(counts: dict[str, int], factor: float, ceiling: int) -> int:
    """Teto por tag proporcional ao próprio dataset.

    Um teto fixo não balanceia nada: com poucos repositórios ele nunca é
    atingido pela tag dominante, e com muitos ele corta todas por igual. O
    teto aqui é `factor ×` a mediana das tags relevantes — as residuais
    (abaixo de 5% do pico) ficam fora do cálculo para não puxar a mediana
    para perto de zero.
    """
    values = [c for c in counts.values() if c > 0]
    if not values:
        return ceiling
    peak = max(values)
    core = sorted(c for c in values if c >= peak * 0.05)
    median = core[len(core) // 2] if core else peak
    return max(50, min(ceiling, round(median * factor)))


def balance(
    snippets: list[Snippet],
    max_per_tag: int | None = None,
    *,
    factor: float = 1.5,
) -> list[Snippet]:
    """Balanceamento estratificado por tag.

    Não nivela tudo pelo menor grupo (isso jogaria fora a maior parte do
    dataset); aplica um teto por tag e, dentro de cada uma, fica com os
    snippets de maior qualidade. Também limita a fatia de um único repo por
    tag, para um projeto grande não virar metade de uma categoria.
    """
    by_tag: dict[str, list[Snippet]] = defaultdict(list)
    for snippet in snippets:
        by_tag[snippet.tag].append(snippet)

    if max_per_tag is None:
        max_per_tag = adaptive_cap(
            {tag: len(group) for tag, group in by_tag.items()}, factor, MAX_PER_TAG
        )
        log.info("Layer 4: teto adaptativo por tag = %d", max_per_tag)

    per_repo_cap = max(5, max_per_tag // 10)
    balanced: list[Snippet] = []

    for tag, group in by_tag.items():
        group.sort(key=lambda s: -s.quality)
        repo_counts: Counter[str] = Counter()
        kept: list[Snippet] = []
        overflow: list[Snippet] = []
        for snippet in group:
            if len(kept) >= max_per_tag:
                break
            if repo_counts[snippet.repo_id] >= per_repo_cap:
                overflow.append(snippet)
                continue
            repo_counts[snippet.repo_id] += 1
            kept.append(snippet)
        # Se o cap por repo deixou a tag abaixo do teto, recompõe com o resto.
        if len(kept) < max_per_tag:
            kept.extend(overflow[: max_per_tag - len(kept)])
        log.debug("tag %s: %d -> %d", tag, len(group), len(kept))
        balanced.extend(kept)

    return balanced


def _stratified_split(
    records: list[tuple[DatasetRecord, str]], ratio: float
) -> tuple[list[DatasetRecord], list[DatasetRecord]]:
    """Split estratificado por tag primária — val representa todas as tags."""
    rng = random.Random(RANDOM_SEED)
    by_tag: dict[str, list[DatasetRecord]] = defaultdict(list)
    for record, tag in records:
        by_tag[tag].append(record)

    train: list[DatasetRecord] = []
    val: list[DatasetRecord] = []
    for group in by_tag.values():
        rng.shuffle(group)
        cut = max(1, round(len(group) * ratio)) if len(group) > 1 else 0
        val.extend(group[:cut])
        train.extend(group[cut:])

    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def run(
    snippets: list[Snippet], repo_names: dict[str, str], max_per_tag: int | None = None
) -> dict:
    if not snippets:
        log.warning("Layer 4: sem snippets — rode a Layer 3 antes")
        return {}

    selected = balance(snippets, max_per_tag)
    log.info("Layer 4: %d snippets -> %d após balanceamento", len(snippets), len(selected))

    records: list[tuple[DatasetRecord, str]] = []
    with progress_bar("Gerando dataset de finetuning", total=len(selected)) as advance:
        for index, snippet in enumerate(selected):
            full_name = repo_names.get(snippet.repo_id, snippet.repo_id.replace("__", "/"))
            records.append((_to_record(snippet, index, full_name), snippet.tag))
            advance(1)

    train, val = _stratified_split(records, VAL_SPLIT_RATIO)
    all_records = [r for r, _ in records]

    state.write_jsonl(DATASET_FILE, [r.to_dict() for r in all_records])
    state.write_jsonl(DATASET_TRAIN_FILE, [r.to_dict() for r in train])
    state.write_jsonl(DATASET_VAL_FILE, [r.to_dict() for r in val])

    stats = {
        "total_snippets": len(all_records),
        "by_tag": dict(Counter(tag for _, tag in records).most_common()),
        "by_tier": {str(k): v for k, v in sorted(Counter(r.tier for r in all_records).items())},
        "by_role": dict(Counter(r.metadata["role"] for r in all_records).most_common()),
        "unique_repos": len({r.source_repo for r in all_records}),
        "train_split": len(train),
        "val_split": len(val),
        "snippets_before_balance": len(snippets),
        "avg_lines": round(
            sum(r.metadata["lines"] for r in all_records) / max(1, len(all_records)), 1
        ),
    }
    state.write_json(DATASET_STATS_FILE, stats)
    return stats
