"""Testes da geração de dataset (Layer 4).

O foco aqui é o que mais degrada um finetuning: rótulo que afirma mais do que
o código faz, e balanceamento que deixa uma tag dominar.
"""

from __future__ import annotations

from miner.models import Snippet
from miner.pipeline.layer4_dataset import (
    _instruction,
    _spec_sdd,
    _stratified_split,
    _to_record,
    adaptive_cap,
    balance,
)


def make_snippet(**overrides) -> Snippet:
    base = dict(
        snippet_id="sha256:abc",
        repo_id="o__r",
        file_path="app/routers/u.py",
        role="router",
        tag="routing",
        secondary_tags=[],
        code='@router.post("/users")\nasync def create_user():\n    return 1',
        context_imports=["from fastapi import APIRouter"],
        line_start=1,
        line_end=3,
        repo_stars=1000,
        tier=1,
        dedup_hash="sha256:abc",
        symbol="create_user",
        is_async=True,
        has_docstring=True,
        quality=1.0,
    )
    base.update(overrides)
    return Snippet(**base)


# ---------------------------------------------------------------------------
# Rótulos precisam refletir o código
# ---------------------------------------------------------------------------


def test_routing_instruction_names_real_method_and_path():
    text = _instruction(make_snippet())
    assert "POST" in text and "/users" in text


def test_error_handling_does_not_claim_httpexception_when_absent():
    snippet = make_snippet(
        tag="error_handling",
        symbol="download",
        is_async=False,
        code="def download():\n    try:\n        go()\n    except OSError:\n        return None",
    )
    assert "HTTPException" not in _instruction(snippet)
    assert "HTTPException" not in _spec_sdd(snippet)["padrao_aplicado"]


def test_error_handling_claims_httpexception_when_present():
    snippet = make_snippet(
        tag="error_handling",
        symbol="get_item",
        is_async=False,
        code="def get_item():\n    raise HTTPException(status_code=404)",
    )
    assert "HTTPException" in _spec_sdd(snippet)["padrao_aplicado"]


def test_config_does_not_claim_pydantic_settings_for_plain_dataclass():
    snippet = make_snippet(
        tag="config",
        symbol="MLConfig",
        is_async=False,
        code="@dataclass\nclass MLConfig:\n    model: str = os.getenv('M', 'x')",
    )
    assert "pydantic-settings" not in _instruction(snippet)
    assert "pydantic-settings" not in _spec_sdd(snippet)["padrao_aplicado"]


def test_config_claims_pydantic_settings_when_basesettings_present():
    snippet = make_snippet(
        tag="config",
        symbol="Settings",
        is_async=False,
        code="class Settings(BaseSettings):\n    debug: bool = False",
    )
    assert "pydantic-settings" in _spec_sdd(snippet)["padrao_aplicado"]


def test_gender_agreement_in_generated_instruction():
    """`dependência` é feminino — o adjetivo precisa concordar."""
    snippet = make_snippet(tag="dependency_injection", symbol="get_db", is_async=True)
    text = _instruction(snippet)
    assert "dependência assíncrona" in text
    assert "dependência assíncrono" not in text


def test_record_keeps_traceability_to_source():
    record = _to_record(make_snippet(), 123, "owner/repo")
    assert record.id == "ft_000123"
    assert record.source_repo == "owner/repo"
    assert record.source_snippet_id == "sha256:abc"
    assert record.metadata["file_path"] == "app/routers/u.py"


def test_record_puts_imports_in_input_not_output():
    record = _to_record(make_snippet(), 0, "owner/repo")
    assert "from fastapi import APIRouter" in record.input
    assert record.output.startswith("@router.post")


# ---------------------------------------------------------------------------
# Balanceamento
# ---------------------------------------------------------------------------


def test_adaptive_cap_ignores_residual_tags():
    """Tags residuais não devem puxar a mediana para perto de zero."""
    cap = adaptive_cap({"testing": 950, "routing": 393, "validation": 131, "config": 1}, 1.5, 900)
    assert cap == round(393 * 1.5)


def test_adaptive_cap_respects_ceiling():
    assert adaptive_cap({"a": 100_000, "b": 100_000}, 1.5, 900) == 900


def test_balance_caps_dominant_tag():
    snippets = [make_snippet(tag="testing", repo_id=f"r{i % 40}") for i in range(2000)]
    snippets += [make_snippet(tag="routing", repo_id=f"r{i % 40}") for i in range(300)]
    result = balance(snippets)
    counts: dict[str, int] = {}
    for s in result:
        counts[s.tag] = counts.get(s.tag, 0) + 1
    assert counts["testing"] < 2000  # a tag dominante foi cortada
    assert counts["routing"] == 300  # a minoritária foi preservada por inteiro


def test_balance_limits_single_repo_share_per_tag():
    """Um projeto grande não deve virar metade de uma categoria."""
    snippets = [make_snippet(tag="routing", repo_id="mega") for _ in range(500)]
    snippets += [make_snippet(tag="routing", repo_id=f"r{i}") for i in range(500)]
    result = balance(snippets, max_per_tag=200)
    from_mega = sum(1 for s in result if s.repo_id == "mega")
    assert from_mega <= 200 // 10


def test_stratified_split_covers_every_tag():
    records = []
    for tag, n in (("routing", 100), ("testing", 60), ("config", 20)):
        for i in range(n):
            records.append((_to_record(make_snippet(tag=tag), i, "o/r"), tag))
    train, val = _stratified_split(records, 0.15)

    assert len(train) + len(val) == 180
    val_tags = {t for r in val for t in r.tags}
    assert {"routing", "testing", "config"} <= val_tags


def test_split_has_no_overlap():
    records = [(_to_record(make_snippet(), i, "o/r"), "routing") for i in range(100)]
    train, val = _stratified_split(records, 0.15)
    assert not ({id(r) for r in train} & {id(r) for r in val})
