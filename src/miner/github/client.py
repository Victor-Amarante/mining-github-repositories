"""Cliente assíncrono do GitHub com rate limiting orientado por header.

O ponto central: a GitHub expõe *cotas separadas* para search, REST core e
GraphQL, e o CDN de conteúdo bruto (raw.githubusercontent.com) não consome
nenhuma delas. Tratar as quatro como um pool só é o que faz um miner desses
passar horas dormindo em backoff. Aqui cada classe tem seu próprio budget,
com concorrência e intervalo próprios.

Em vez de retry cego, cada resposta alimenta o budget com
`x-ratelimit-remaining` / `x-ratelimit-reset`, e a próxima requisição espera
o reset quando a cota chega perto do fim.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import (
    API_BASE,
    BACKOFF_BASE,
    GITHUB_TOKEN,
    GRAPHQL_LIMITS,
    GRAPHQL_URL,
    MAX_RETRIES,
    RAW_BASE,
    RAW_LIMITS,
    REQUEST_TIMEOUT,
    REST_LIMITS,
    SEARCH_LIMITS,
    USER_AGENT,
    LimitProfile,
)

log = logging.getLogger("miner.github")


class GitHubError(RuntimeError):
    """Falha não recuperável de uma chamada à API."""


class NotFound(GitHubError):
    """Recurso ausente (404). Esperado e tratado como 'pule este repo'."""


class RetriesExhausted(GitHubError):
    """Todas as tentativas falharam — quase sempre rate limit persistente.

    Distinta de um erro de query: dividir o trabalho e tentar de novo (o que
    faz sentido para isolar um repositório problemático) só multiplicaria a
    pressão sobre o limite que acabou de estourar.
    """


@dataclass
class Budget:
    """Controla ritmo e concorrência de uma classe de recurso da API."""

    profile: LimitProfile
    _sem: asyncio.Semaphore = field(init=False)
    _gate: asyncio.Lock = field(init=False)
    _next_slot: float = field(default=0.0, init=False)
    interval: float = field(default=0.0, init=False)
    remaining: int | None = field(default=None, init=False)
    reset_at: float | None = field(default=None, init=False)
    used: int = field(default=0, init=False)
    waited: float = field(default=0.0, init=False)
    penalties: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(max(1, self.profile.concurrency))
        self._gate = asyncio.Lock()
        self.interval = self.profile.min_interval

    def penalize(self) -> None:
        """Desacelera permanentemente após um rate limit secundário.

        O limite secundário não expõe cota em header — é uma heurística de
        abuso do GitHub sobre ritmo e complexidade. Voltar no mesmo ritmo
        depois de esperar 5 minutos só garante o próximo 403, então cada
        incidente deixa este recurso mais lento pelo resto da execução.
        """
        self.penalties += 1
        self.interval = min(10.0, max(self.interval, 0.5) * 2)
        log.warning(
            "[%s] limite secundário nº%d — intervalo mínimo agora %.1fs",
            self.profile.name,
            self.penalties,
            self.interval,
        )

    async def acquire_slot(self) -> None:
        """Serializa a decisão de quando a próxima requisição pode sair."""
        if self.interval <= 0 and not self._needs_reset_wait():
            return
        async with self._gate:
            now = time.monotonic()
            # Cota quase no fim: dorme até o reset em vez de bater no 403.
            if self._needs_reset_wait() and self.reset_at is not None:
                sleep_for = max(0.0, self.reset_at - time.time()) + 1.0
                if sleep_for > 0:
                    log.warning(
                        "[%s] cota em %s, aguardando %.0fs até o reset",
                        self.profile.name,
                        self.remaining,
                        sleep_for,
                    )
                    self.waited += sleep_for
                    await asyncio.sleep(sleep_for)
                    self.remaining = None
                    self.reset_at = None
                    now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                self.waited += wait
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_slot = now + self.interval

    def _needs_reset_wait(self) -> bool:
        return (
            self.remaining is not None
            and self.remaining <= self.profile.reserve
            and self.reset_at is not None
            and self.reset_at > time.time()
        )

    def observe(self, response: httpx.Response) -> None:
        """Atualiza o budget a partir dos headers da resposta."""
        self.used += 1
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None and remaining.isdigit():
            self.remaining = int(remaining)
        if reset is not None and reset.isdigit():
            self.reset_at = float(reset)

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.used,
            "remaining": self.remaining,
            "waited_seconds": round(self.waited, 1),
            "penalties": self.penalties,
        }


class GitHubClient:
    """Fachada única para Search, REST, GraphQL e CDN bruto."""

    def __init__(self, token: str | None = None, scale: float = 1.0) -> None:
        self.token = (token or GITHUB_TOKEN).strip()
        if not self.token:
            raise GitHubError(
                "GITHUB_TOKEN ausente. Defina no .env — sem token o rate limit "
                "torna este mining inviável (60 req/h na REST API)."
            )
        self.budgets = {
            "search": Budget(_scaled(SEARCH_LIMITS, scale)),
            "rest": Budget(_scaled(REST_LIMITS, scale)),
            "graphql": Budget(_scaled(GRAPHQL_LIMITS, scale)),
            "raw": Budget(_scaled(RAW_LIMITS, scale)),
        }
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            limits=limits,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Núcleo de requisição
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        budget_key: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        expect_text: bool = False,
    ) -> Any:
        budget = self.budgets[budget_key]
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            await budget.acquire_slot()
            async with budget._sem:
                try:
                    response = await self._client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers={"Accept": accept},
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    await self._backoff(attempt, budget_key, f"{type(exc).__name__}")
                    continue

            budget.observe(response)

            if response.status_code == 404:
                raise NotFound(f"404 em {url}")

            if response.status_code in (403, 429):
                # 403 aqui é quase sempre rate limit (primário ou secundário),
                # não permissão. O header diz qual.
                delay = self._rate_limit_delay(response, attempt)
                if self._is_secondary_limit(response):
                    budget.penalize()
                log.warning(
                    "[%s] rate limit (%s), aguardando %.0fs",
                    budget_key,
                    response.status_code,
                    delay,
                )
                budget.waited += delay
                await asyncio.sleep(delay)
                last_error = GitHubError(f"{response.status_code} em {url}")
                continue

            if response.status_code >= 500:
                last_error = GitHubError(f"{response.status_code} em {url}")
                await self._backoff(attempt, budget_key, f"HTTP {response.status_code}")
                continue

            if response.status_code >= 400:
                raise GitHubError(
                    f"HTTP {response.status_code} em {url}: {response.text[:200]}"
                )

            return response.text if expect_text else response.json()

        raise RetriesExhausted(
            f"Esgotadas {MAX_RETRIES} tentativas em {url}"
        ) from last_error

    async def _backoff(self, attempt: int, key: str, reason: str) -> None:
        delay = (BACKOFF_BASE**attempt) + random.uniform(0, 0.5)
        log.debug("[%s] retry %d (%s) em %.1fs", key, attempt + 1, reason, delay)
        self.budgets[key].waited += delay
        await asyncio.sleep(delay)

    @staticmethod
    def _is_secondary_limit(response: httpx.Response) -> bool:
        """Distingue limite secundário (abuso) do primário (cota esgotada).

        No primário, `x-ratelimit-remaining` chega a 0. No secundário a cota
        ainda tem saldo — o que estourou foi o ritmo.
        """
        remaining = response.headers.get("x-ratelimit-remaining")
        return remaining is None or (remaining.isdigit() and int(remaining) > 0)

    @staticmethod
    def _rate_limit_delay(response: httpx.Response, attempt: int) -> float:
        """Quanto esperar num 403/429 — respeitando o que o GitHub pediu."""
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return float(retry_after) + 1.0
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset and reset.isdigit():
            return max(1.0, float(reset) - time.time() + 1.0)
        # Secondary rate limit: sem header, só backoff exponencial.
        return min(60.0, (BACKOFF_BASE ** (attempt + 2)) + random.uniform(0, 1.0))

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def search_repositories(
        self, query: str, *, page: int = 1, per_page: int = 100, sort: str = "stars"
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE}/search/repositories",
            "search",
            params={
                "q": query,
                "page": page,
                "per_page": per_page,
                "sort": sort,
                "order": "desc",
            },
        )

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            GRAPHQL_URL,
            "graphql",
            json_body={"query": query, "variables": variables or {}},
        )
        # A GraphQL devolve 200 com `errors` para falhas parciais. Erros de
        # nó individual são esperados (repo removido) e não derrubam o batch.
        if "errors" in payload and not payload.get("data"):
            raise GitHubError(f"GraphQL: {payload['errors'][:2]}")
        return payload

    async def get_tree(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        """Árvore recursiva completa em UMA chamada — base da Layer 2."""
        return await self._request(
            "GET",
            f"{API_BASE}/repos/{owner}/{repo}/git/trees/{ref}",
            "rest",
            params={"recursive": "1"},
        )

    async def get_raw_file(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Conteúdo via CDN — não consome a cota de 5.000/h da REST API."""
        return await self._request(
            "GET",
            f"{RAW_BASE}/{owner}/{repo}/{ref}/{path}",
            "raw",
            accept="text/plain",
            expect_text=True,
        )

    async def get_blob(self, owner: str, repo: str, sha: str) -> str:
        """Fallback content-addressed para quando o CDN falha."""
        import base64

        data = await self._request(
            "GET", f"{API_BASE}/repos/{owner}/{repo}/git/blobs/{sha}", "rest"
        )
        if data.get("encoding") != "base64":
            return data.get("content", "")
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    async def rate_limit_status(self) -> dict[str, Any]:
        return await self._request("GET", f"{API_BASE}/rate_limit", "rest")

    def budget_report(self) -> dict[str, Any]:
        return {name: b.snapshot() for name, b in self.budgets.items()}


def _scaled(profile: LimitProfile, scale: float) -> LimitProfile:
    if scale == 1.0:
        return profile
    return LimitProfile(
        name=profile.name,
        concurrency=max(1, int(profile.concurrency * scale)),
        min_interval=profile.min_interval,
        reserve=profile.reserve,
    )
