# Relatório de Mining — Repositórios Python/FastAPI no GitHub

**Projeto:** `zup-mining-repos`
**Objetivo:** construir um dataset de finetuning a partir de código FastAPI real e bem-mantido
**Tempo total de execução:** 177 segundos
**Branch:** `feat/mining-python-repos`

---

## 1. Sumário executivo

| Métrica | Valor |
|---|---|
| Candidatos únicos encontrados na busca | 410 |
| Repositórios validados (declaram FastAPI) | 301 (73%) |
| Arquivos de código baixados | 4.318 |
| Arquivos com uso real do ecossistema | 2.676 (62%) |
| Snippets únicos extraídos | 15.582 |
| **Exemplos no dataset final** | **5.601** |
| Repositórios representados no dataset | 243 |
| Split treino / validação | 4.761 / 840 |
| Linhas por exemplo (média) | 23,8 |

**Consumo de API:** 11 chamadas de search, 301 REST (6% da cota horária de 5.000), 21 GraphQL e 4.318 ao CDN de conteúdo (fora de qualquer cota). Apenas 6 segundos foram gastos aguardando rate limit.

---

## 2. Estratégia de seleção de repositórios

A seleção usou três tiers, cada um respondendo a uma pergunta diferente sobre o que o dataset precisa conter.

### 2.1 As nove queries executadas

Todas compartilham um filtro base: `language:python fork:false archived:false`.

| Tier | Label | Query (após o filtro base) | Meta | Encontrados |
|---|---|---|---|---|
| 1 | `core-stars` | `fastapi stars:>100` | 20 | 20 |
| 1 | `core-topic` | `topic:fastapi stars:>50` | 20 | 20 |
| 1 | `core-template` | `fastapi template stars:>100` | 15 | 15 |
| 2 | `eco-pydantic` | `fastapi pydantic stars:>50` | 125 | 80 |
| 2 | `eco-sqlalchemy` | `fastapi sqlalchemy stars:>50` | 125 | 108 |
| 2 | `eco-docker` | `fastapi docker stars:>50` | 125 | 125 |
| 3 | `domain-api` | `fastapi api stars:>30` | 85 | 85 |
| 3 | `domain-rest` | `fastapi rest stars:>30` | 85 | 70 |
| 3 | `domain-micro` | `fastapi microservice stars:>30` | 85 | 33 |

**Total bruto:** 556 resultados → **410 candidatos únicos** após deduplicação.

### 2.2 Racional de cada tier

**Tier 1 — Core FastAPI (limiar: 100 estrelas).** Codebases estabelecidas e bem-mantidas. Filtra ruído e estabelece a referência de "como se escreve FastAPI corretamente". A query `topic:fastapi` é qualitativamente diferente das outras: `topic:` casa com uma tag que o **mantenedor declarou explicitamente**, não com texto no README — é o sinal de intencionalidade mais forte disponível na Search API.

**Tier 2 — FastAPI + ecossistema (limiar: 50 estrelas).** Entender como FastAPI opera em produção junto de bibliotecas complementares: `pydantic` (validação), `sqlalchemy` (persistência), `docker` (deployment). É o tier de maior volume porque representa a stack profissional real — e de fato dominou o resultado (181 dos 301 repos).

**Tier 3 — Domain-specific (limiar: 30 estrelas).** Cobertura ampla de casos de uso (`api`, `rest`, `microservice`) para o modelo generalizar em vez de decorar um padrão único.

### 2.3 Deduplicação e desempate

Um repositório casa com várias queries — `polarsource/polar` aparece em `eco-sqlalchemy`, `eco-docker` e `domain-api`. A regra de desempate: **o tier mais baixo (mais exigente) vence**. Um repo que apareceu no Tier 1 e no Tier 3 é registrado como Tier 1, porque passou pelo crivo mais rigoroso.

A ordenação final é `(tier ascendente, estrelas descendente)`.

---

## 3. Métricas utilizadas — e as que não foram

Esta seção é deliberadamente explícita sobre a diferença entre **métrica usada como critério**, **métrica capturada para auditoria** e **métrica não utilizada**. A distinção importa para quem for reproduzir ou ajustar o mining.

### 3.1 Métricas usadas como critério de seleção

| Métrica | Onde | Como foi usada |
|---|---|---|
| **Estrelas** (`stargazers_count`) | Search API | Limiar por tier: `>100` (T1), `>50` (T2), `>30` (T3) |
| **Estrelas** | Ordenação | `sort=stars&order=desc` — os melhores primeiro em cada query |
| **Estrelas** | Score de qualidade | `log10(stars+1)/5` no ranking de snippets |
| **É fork?** (`fork:false`) | Search API | **Exclui** repositórios que são fork de outro |
| **Está arquivado?** (`archived:false`) | Search API | **Exclui** projetos descontinuados |
| **Linguagem** (`language:python`) | Search API | Restringe à linguagem-alvo |
| **Tópico declarado** (`topic:fastapi`) | Search API | Tag explícita do mantenedor (query `core-topic`) |
| **Dependência declarada** | Layer 1 | `fastapi` presente no manifesto — critério eliminatório |
| **Tamanho do arquivo** (`size` do blob) | Layer 2 | Descarta blobs > 50 KB sem baixá-los |
| **Papel do arquivo** (path) | Layer 2 | Quota por papel (router: 12, schema: 8, test: 6…) |
| **Tier de origem** | Score de qualidade | Peso +0,30 / +0,20 / +0,10 para T1 / T2 / T3 |
| **Docstring presente** | Score de qualidade | +0,20 |
| **Anotações de tipo** | Score de qualidade | +0,15 |
| **Tamanho do snippet** | Score de qualidade | +0,15 se entre 8 e 60 linhas |

### 3.2 Métricas capturadas mas NÃO usadas como filtro

Estão gravadas em `data/raw/{repo}/manifest.json` e disponíveis para filtragem posterior:

| Métrica | Onde está | Por que não foi usada |
|---|---|---|
| **Licença** (`spdxId`) | `manifest.json` | Capturada para permitir filtro legal posterior. **Nenhum filtro de licença foi aplicado** — ver §7.2 |
| **Última atualização** (`pushedAt`) | `manifest.json` | **Recência não foi critério de seleção.** Não há filtro `pushed:>data` nas queries |
| **Descrição do repositório** | interno | Apenas informativa |
| **`isArchived` / `isFork`** | query GraphQL | Solicitados na validação mas **nunca verificados** no código — redundantes, pois `fork:false` e `archived:false` já filtram na origem. É dado buscado e descartado |

### 3.3 Métricas explicitamente NÃO utilizadas

| Métrica | Situação |
|---|---|
| **Número de forks** | **Não foi usado em nenhum ponto.** Atenção à ambiguidade: `fork:false` é um booleano que *exclui repositórios que são forks*, e não tem relação com a *contagem* de forks como sinal de popularidade |
| **Número de watchers** | Não usado |
| **Número de contribuidores** | Não usado |
| **Issues abertas / fechadas** | Não usado |
| **Frequência de commits** | Não usado |
| **Cobertura de testes** | Não usado |
| **`MIN_STARS_ABSOLUTE = 30`** | Constante **definida em `config.py:91` mas nunca referenciada** — configuração morta. Os limiares reais estão embutidos nas queries |

**Por que estrelas e não forks?** Estrelas medem *aprovação* (alguém achou o código bom o suficiente para marcar). Forks medem *derivação*, que num contexto de mining é frequentemente ruído — um tutorial popular gera centenas de forks de alunos, todos com o mesmo código boilerplate. Como o objetivo é diversidade de padrões, forks alto seria um anti-sinal tanto quanto um sinal. O filtro `fork:false` ataca justamente esse problema, eliminando as cópias na origem.

**Por que não filtrar por recência?** Uma decisão consciente com trade-off real: `archived:false` já elimina projetos formalmente descontinuados, e um filtro de `pushed:` cortaria bibliotecas estáveis que simplesmente não precisam de commits. O custo é que repositórios abandonados sem arquivamento formal podem ter entrado, possivelmente com padrões FastAPI antigos (ex.: Pydantic v1). **Esta é a limitação mais relevante do mining atual** — ver §7.1.

---

## 4. O funil completo, com números reais

```
    556 resultados brutos das 9 queries
     │  dedupe entre queries (tier mais baixo vence)
     ▼
    410 candidatos únicos
     │  LAYER 1 — validação de dependência declarada
     │  ├─ 59 descartados: nenhum manifesto encontrado
     │  └─ 50 descartados: manifesto existe, mas não declara fastapi
     ▼
    301 repositórios validados          (73% de aproveitamento)
     │  LAYER 2 — seleção por papel + quota, blobs ≤ 50 KB
     ▼
  4.318 arquivos .py baixados           (14,3 por repositório)
     │  LAYER 3 — portão de relevância no nível do arquivo
     │  └─ 1.641 descartados: não importam o ecossistema FastAPI
     ▼
  2.676 arquivos relevantes             (62%)
     │  corte por AST + tagging
     ▼
 15.857 snippets brutos
     │  dedupe por hash de código normalizado
     │  └─ 275 duplicatas removidas (2%)
     ▼
 15.582 snippets únicos
     │  LAYER 4 — balanceamento estratificado
     │  └─ 9.981 descartados por teto de tag e teto por repo
     ▼
  5.601 exemplos no dataset final       (243 repositórios representados)
     │  split estratificado 85/15
     ▼
  4.761 treino  +  840 validação
```

### 4.1 Por que 109 repositórios foram descartados na Layer 1

| Motivo | Qtd | O que significa |
|---|---|---|
| `sem_manifesto` | 59 | Nenhum dos 15 caminhos de manifesto conhecidos existe no repositório |
| `fastapi_nao_declarado` | 50 | Manifesto existe, mas `fastapi` não consta nas dependências |

Exemplos concretos de descarte correto: `coleifer/peewee` (ORM que menciona FastAPI no README), `jina-ai/serve`, `pycaret/pycaret`, `mouredev/Hello-Python` (repositório de tutorial sem manifesto).

**`fastapi/fastapi` foi descartado — e isso é intencional.** O framework não declara a si mesmo como dependência; suas dependências são `starlette` e `pydantic`. Além disso, o código-fonte do framework é *implementação interna* (`fastapi/routing.py`, `fastapi/dependencies/utils.py`), não *uso* de FastAPI. Treinar um modelo para "escrever uma aplicação FastAPI" com as entranhas do framework ensinaria a coisa errada. O critério de dependência declarada, por acaso, produz a exclusão correta.

### 4.2 Por que 1.641 arquivos foram descartados na Layer 3

Validar FastAPI no nível do **repositório** não garante relevância no nível do **arquivo**. Um repo que declara FastAPI também tem CLI, scripts de build e utilitários. Sem essa checagem, `headroom/cli/main.py` casava com o padrão de entrypoint (`main.py`) e entrava no dataset rotulado como aplicação FastAPI — sendo uma ferramenta de linha de comando construída com Click.

O portão exige que o arquivo importe, em nível de módulo, pelo menos um de: `fastapi`, `starlette`, `pydantic`, `pydantic_settings`, `sqlmodel`, `sqlalchemy`, `strawberry`, `beanie`, `tortoise`, `motor` — ou qualquer módulo iniciado por `fastapi`. Arquivos de teste ganham tolerância extra para `httpx`, `pytest_asyncio` e `asgi_lifespan`, comuns em testes de aplicações ASGI.

---

## 5. Perfil dos repositórios minerados

### 5.1 Distribuição de estrelas (301 repositórios)

| Faixa | Repositórios | % |
|---|---|---|
| 30 – 99 | 81 | 26,9% |
| 100 – 499 | 135 | 44,9% |
| 500 – 999 | 38 | 12,6% |
| 1k – 5k | 34 | 11,3% |
| 5k – 20k | 12 | 4,0% |
| > 20k | 1 | 0,3% |

**Mínimo:** 32 · **P25:** 87 · **Mediana:** 214 · **P75:** 584 · **Máximo:** 67.015 · **Média:** 1.063

A média (1.063) é cinco vezes a mediana (214) — a distribuição tem cauda longa, como esperado em popularidade de software. É exatamente por isso que o score de qualidade usa `log10(estrelas)` em vez do valor bruto: sem a compressão logarítmica, um único repositório de 67 mil estrelas dominaria todo o ranking de seleção de snippets.

### 5.2 Distribuição por tier

| Tier | Repositórios | % |
|---|---|---|
| Tier 2 (ecossistema) | 181 | 60,1% |
| Tier 3 (domínio) | 98 | 32,6% |
| Tier 1 (core) | 22 | 7,3% |

O Tier 1 é pequeno por construção: as três queries tinham meta somada de 55 repositórios, e destes 22 sobreviveram à validação.

### 5.3 Licenças

| Licença | Repositórios |
|---|---|
| MIT | 169 |
| **Sem licença** | **54** |
| Apache-2.0 | 34 |
| **AGPL-3.0** | **13** |
| **GPL-3.0** | **11** |
| NOASSERTION | 9 |
| BSD-3-Clause | 2 |
| Unlicense | 2 |
| Outras (BSD-2, GPL-2.0, Zlib, WTFPL, ISC, LGPL-3.0, BSD-3-Clear) | 7 |

Permissivas (MIT, Apache-2.0, BSD-2/3-Clause, BSD-3-Clause-Clear, ISC, Unlicense, Zlib, WTFPL): **212 repositórios (70,4%)**.
Copyleft ou indefinidas (sem licença, AGPL-3.0, GPL-3.0, GPL-2.0, LGPL-3.0, NOASSERTION): **89 repositórios (29,6%)**.

Ver §7.2 para as implicações.

### 5.4 Origem da declaração de dependência

| Arquivo de manifesto | Repositórios |
|---|---|
| `pyproject.toml` | 155 |
| `requirements.txt` | 109 |
| `backend/requirements.txt` | 12 |
| `backend/pyproject.toml` | 10 |
| `Pipfile` | 7 |
| `requirements/base.txt` | 2 |
| `api/requirements.txt` | 2 |
| `server/pyproject.toml` | 2 |
| `server/requirements.txt` | 1 |
| `setup.py` | 1 |

**27 repositórios (9%) só foram encontrados por causa dos caminhos de monorepo** (`backend/`, `server/`, `api/`). Sem eles, projetos como `polarsource/polar` — cujo backend Python vive em `server/` enquanto a raiz tem `package.json` — teriam sido descartados como "sem manifesto". Este foi um falso negativo real, detectado e corrigido durante a construção.

### 5.5 Ecossistema declarado (bibliotecas mais frequentes)

| Biblioteca | Repositórios |
|---|---|
| uvicorn | 255 |
| pydantic | 189 |
| pytest | 188 |
| sqlalchemy | 138 |
| httpx | 105 |
| pydantic-settings | 101 |
| alembic | 89 |
| asyncpg | 80 |
| redis | 70 |
| passlib | 52 |
| gunicorn | 38 |
| python-jose | 36 |

Confirma que a amostra representa stacks de produção reais: servidor ASGI (uvicorn/gunicorn), migrações (alembic), driver assíncrono de banco (asyncpg), cache (redis) e autenticação (passlib/python-jose).

---

## 6. Composição do dataset final

### 6.1 Distribuição por tag (padrão principal do código)

| Tag | Exemplos | % | Teto atingido? |
|---|---|---|---|
| testing | 900 | 16,1% | sim |
| routing | 900 | 16,1% | sim |
| error_handling | 900 | 16,1% | sim |
| validation | 900 | 16,1% | sim |
| database | 861 | 15,4% | não |
| async | 646 | 11,5% | não |
| dependency_injection | 249 | 4,4% | não |
| config | 245 | 4,4% | não |

Antes do balanceamento a distribuição era severamente enviesada: `testing` com 5.981 (38,4%) contra `config` com 245 (1,6%) — uma razão de 24:1. Após o balanceamento, a razão caiu para 3,7:1.

O teto por tag foi de **900**, valor calculado adaptativamente (1,5 × a mediana das tags relevantes = 4.506) mas limitado pelo teto absoluto configurado em `MAX_PER_TAG = 900`.

### 6.2 Cobertura de tags incluindo secundárias

Cada exemplo carrega um padrão principal e até quatro secundários. Contando todas as posições:

| Tag | Ocorrências | % dos exemplos |
|---|---|---|
| async | 2.661 | 47,5% |
| database | 1.506 | 26,9% |
| error_handling | 1.388 | 24,8% |
| routing | 1.217 | 21,7% |
| validation | 974 | 17,4% |
| dependency_injection | 968 | 17,3% |
| testing | 928 | 16,6% |
| config | 254 | 4,5% |
| auth | 251 | 4,5% |
| streaming | 51 | 0,9% |
| file_upload | 42 | 0,7% |

Este quadro corrige uma leitura pessimista da tabela anterior: `dependency_injection` aparece como padrão principal em apenas 4,4% dos exemplos, mas está **presente em 17,3%** deles. A razão é semântica: um handler de rota que consome `Depends()` é classificado como `routing` (a rota é o que ele *é*; `Depends` é o que ele *usa*), enquanto apenas funções provedoras sem decorator de rota recebem `dependency_injection` como principal.

### 6.3 Distribuição por papel do arquivo de origem

| Papel | Exemplos |
|---|---|
| router | 1.975 |
| schema | 1.097 |
| test | 891 |
| database | 491 |
| entrypoint | 434 |
| config | 298 |
| dependency | 263 |
| middleware | 152 |

### 6.4 Distribuição por tier

| Tier | Exemplos | % |
|---|---|---|
| Tier 2 | 3.477 | 62,1% |
| Tier 1 | 1.395 | 24,9% |
| Tier 3 | 729 | 13,0% |

O Tier 1 responde por 24,9% dos exemplos vindo de apenas 7,3% dos repositórios — reflexo do peso `+0,30` no score de qualidade, que favorece deliberadamente as fontes mais confiáveis.

### 6.5 Concentração por repositório

| Repositório | Exemplos | % |
|---|---|---|
| xerrors/Yuxi | 276 | 4,9% |
| plastic-labs/honcho | 228 | 4,1% |
| IBM/mcp-context-forge | 214 | 3,8% |
| polarsource/polar | 209 | 3,7% |
| TracecatHQ/tracecat | 201 | 3,6% |
| xr843/fojin | 163 | 2,9% |
| Soju06/codex-lb | 151 | 2,7% |
| benavlabs/FastAPI-boilerplate | 144 | 2,6% |
| gnuboard/g6 | 115 | 2,1% |
| DarkEnergyProcessor/NPPS4 | 102 | 1,8% |

Os 10 maiores somam **32,2%** do dataset. A mediana é de **9 exemplos por repositório**. Existe um teto de 90 exemplos por repositório *por tag* (`MAX_PER_TAG // 10`), que impede um projeto grande de monopolizar uma categoria — mas como o teto é por tag, um repositório diversificado pode acumular através de várias delas. Ver §7.3.

---

## 7. Limitações conhecidas e vieses

### 7.1 Ausência de filtro de recência

Nenhuma query usa `pushed:>data`. `archived:false` elimina apenas projetos formalmente arquivados. Consequência: repositórios abandonados sem arquivamento formal podem ter entrado, trazendo padrões desatualizados — o risco concreto é código Pydantic v1 (`@validator`, `.dict()`, `class Config`) misturado com Pydantic v2 (`@field_validator`, `.model_dump()`, `model_config`).

**Mitigação disponível:** o campo `last_updated` está em todo `manifest.json`. É possível filtrar o dataset a posteriori sem refazer o mining.

### 7.2 Licenciamento não filtrado

**89 repositórios (29,6%) têm licença copyleft ou indefinida** — 54 sem licença nenhuma, 13 AGPL-3.0, 11 GPL-3.0, 9 NOASSERTION, 1 GPL-2.0, 1 LGPL-3.0.

Código sem licença explícita é, por padrão, "todos os direitos reservados": não há concessão de uso. AGPL e GPL impõem obrigações de reciprocidade cujo alcance sobre pesos de modelo treinado é juridicamente contestado e não pacificado.

**Nenhum filtro foi aplicado porque essa é uma decisão de negócio, não técnica.** A infraestrutura para aplicá-lo está pronta: a licença SPDX está em cada `manifest.json` e `source_repo` mantém atribuição em toda linha do dataset final. Um filtro restrito a MIT/Apache/BSD/ISC preservaria ~70% dos repositórios.

**Recomendação:** decidir a política de licenciamento antes de qualquer uso do dataset em treinamento.

### 7.3 Concentração de fontes

Os 10 maiores repositórios contribuem com 32,2% dos exemplos. O teto por repositório opera *por tag* (90), não globalmente — um projeto grande e diversificado acumula através de múltiplas tags. Para reduzir a concentração, seria necessário um teto global por repositório.

### 7.4 Volume abaixo do planejado

O plano original mirava ~680 repositórios (50 + 375 + 255). O universo real sob esses filtros produziu **410 candidatos únicos**, por dois motivos:

1. **Sobreposição entre queries** — 556 resultados brutos colapsaram em 410 únicos (26% de sobreposição).
2. **Queries que não atingiram a meta** — `domain-micro` devolveu 33 de 85; `eco-pydantic`, 80 de 125.

Simplesmente não existem 680 repositórios Python distintos, não-fork, não-arquivados, com FastAPI e acima dos limiares de estrelas definidos.

**Para aumentar o volume:** baixar os limiares de estrelas, adicionar queries com termos de domínio novos (`fastapi auth`, `fastapi celery`, `fastapi graphql`, `fastapi websocket`), ou aceitar repositórios menores no Tier 3. Todos os knobs estão em `SEARCH_QUERIES` (`src/miner/config.py:73`).

### 7.5 Viés de idioma no código-fonte

Parte dos exemplos contém docstrings e comentários em chinês, vindos de repositórios populares como `Evil0ctal/Douyin_TikTok_Download_API` (19,5k estrelas). É código FastAPI legítimo e bem estruturado, mas se o modelo-alvo deve produzir comentários em português ou inglês, convém filtrar por range Unicode nos campos `output` e `input` antes do treino.

### 7.6 Limitações da Search API

A repository-search do GitHub casa termos com **nome, descrição e README** — não com o conteúdo do código. Um repositório que apenas menciona FastAPI aparece na busca. Foi exatamente essa a razão de a validação por dependência declarada (Layer 1) existir: ela é o mecanismo que separa menção de uso real, e descartou 50 repositórios que a busca textual havia aprovado.

A Search API também limita a 1.000 resultados por query (10 páginas de 100), teto que não chegou a ser atingido nesta execução.

### 7.7 Configuração morta

`MIN_STARS_ABSOLUTE = 30` está definida em `src/miner/config.py:91` mas nunca é referenciada. Os limiares reais vivem embutidos nas strings de query. Igualmente, `isArchived` e `isFork` são solicitados na query GraphQL e nunca verificados — são redundantes com os filtros de busca, mas ocupam espaço na requisição.

---

## 8. Critérios de qualidade aplicados

### 8.1 Seleção de arquivos de alto sinal (Layer 2)

Papéis reconhecidos por padrão de caminho, com quota por repositório:

| Papel | Quota | Padrões de caminho |
|---|---|---|
| router | 12 | `routers/`, `api/`, `endpoints/`, `views/`, `controllers/`, `routes.py` |
| schema | 8 | `schemas/`, `models/`, `dto/`, `entities/` |
| test | 6 | `tests/`, `test_*.py`, `*_test.py` |
| dependency | 4 | `dependencies.py`, `deps/`, `security.py` |
| database | 4 | `database.py`, `db/`, `session.py`, `crud/`, `repository/` |
| entrypoint | 3 | `main.py`, `app.py`, `asgi.py`, `server.py` |
| middleware | 3 | `middleware.py`, `exception_handlers.py`, `errors.py` |
| config | 2 | `config.py`, `settings.py`, `core/` |

Teto global de 40 arquivos por repositório. A quota é o critério de seleção — **não há recomposição até o teto**, porque preencher as vagas restantes com o papel mais abundante desfaria o balanceamento na origem.

**Exclusões absolutas:** `node_modules/`, `.venv/`, `site-packages/`, `migrations/versions/`, `alembic/versions/`, `vendor/`, `third_party/`, `build/`, `dist/`, `docs/`, `static/`, `assets/`, `locale/`, `__pycache__/`, além de `conftest.py`, `__init__.py` e `setup.py`. Blobs acima de 50 KB são descartados usando o campo `size` da própria resposta da Trees API — nunca chegam a ser requisitados.

### 8.2 Corte por AST, nunca cego

Cada snippet vai de `node.lineno` (recuando para incluir decorators) até `node.end_lineno`. Os imports anexados em `input` são apenas os que o snippet efetivamente referencia.

**Verificação:** os **5.601 exemplos** do dataset final parseiam como Python válido isoladamente — 100%. É a evidência de que nenhum corte truncou uma definição no meio.

### 8.3 Tagging por AST, não por regex

Os padrões são detectados sobre a árvore sintática, não por busca textual: `Depends(` dentro de uma string ou comentário não conta como injeção de dependência. Cada detector atribui um peso, e o padrão principal é o de maior pontuação, com desempate por especificidade.

**Nenhuma tag é inventada.** Quando um snippet não apresenta padrão reconhecível, ele é descartado em vez de receber um rótulo por omissão. Rótulo falso é pior que dado ausente num finetuning: ensina a associação errada.

### 8.4 Deduplicação estrutural

O hash de dedupe é calculado sobre o código **normalizado por tokenizer** — comentários removidos, espaçamento colapsado. Dois forks do mesmo boilerplate com indentação ou comentários diferentes colidem no mesmo hash. Removeu 275 duplicatas (2%).

O percentual baixo é consequência direta do `fork:false`: a maior fonte de duplicação já havia sido eliminada na busca.

### 8.5 Score de qualidade

```
score  = log10(estrelas + 1) / 5           # ~0 a 1, comprime a cauda longa
       + {T1: 0.30, T2: 0.20, T3: 0.10}    # confiança da fonte
       + 0.20  se tem docstring
       + 0.15  se tem anotações de tipo
       + 0.15  se tem entre 8 e 60 linhas
```

Usado para decidir quem sobrevive ao teto por tag e qual duplicata é preservada.

### 8.6 Geração determinística de instrução e spec SDD

Instruções e specs são preenchidas por template com **fatos extraídos do próprio código** (método HTTP, rota, nome do símbolo, bibliotecas em uso). Nenhum LLM participa do pipeline — mantém reprodutibilidade, custo zero e evita injetar alucinação nos rótulos que vão treinar outro modelo.

Onde um template poderia prometer mais do que o código entrega, a frase é condicionada à evidência: só afirma `HTTPException` se o código a contém; só afirma `pydantic-settings` se há `BaseSettings`.

---

## 9. Arquitetura e otimizações

O gargalo real deste tipo de mining não é CPU — é cota de API. Três decisões respondem por quase toda a diferença de tempo de execução.

### 9.1 Validação em lote via GraphQL

A abordagem direta (Contents API por arquivo de manifesto) custaria 3 a 5 chamadas REST por repositório: para 410 candidatos, cerca de 1.500 requisições contra uma cota de 5.000/hora — antes de baixar uma linha de código útil.

A GraphQL resolve com aliases: uma requisição pede, para 20 repositórios de uma vez, o SHA do commit, licença, estrelas e o **texto integral de 15 manifestos**. Resultado: **21 requisições no lugar de ~1.500**. O `commit_sha` vem junto, eliminando também a chamada extra que seria necessária para obtê-lo.

### 9.2 Conteúdo pelo CDN

`raw.githubusercontent.com` **não consome a cota de 5.000/h da REST API**. Como o download de arquivos é o passo de maior volume, tirá-lo da cota é a diferença entre o mining caber numa janela de rate limit ou não.

As **4.318 buscas de conteúdo** seriam 4.318 chamadas REST no desenho ingênuo — sozinhas já estourariam a cota horária. A Git Blobs API permanece como fallback quando o CDN falha.

### 9.3 Uma chamada de árvore por repositório

`git/trees?recursive=1` devolve caminho e tamanho de cada blob de uma vez. Toda a seleção de arquivos vira computação local: zero chamadas de descoberta, e blobs grandes descartados sem nunca serem pedidos. **301 chamadas REST no total** — 6% da cota horária.

### 9.4 Rate limiting por classe de recurso

Search (30/min), REST (5.000/h), GraphQL (5.000 pontos/h) e CDN têm cotas independentes. Tratá-las como um pool único faz o miner dormir em backoff desnecessário. Cada classe tem seu próprio budget, concorrência e intervalo, com pausa orientada pelos headers `X-RateLimit-Remaining` / `Reset` em vez de retry cego.

Há ainda **auto-throttle adaptativo**: ao detectar um rate limit *secundário* (403 sem cota esgotada — heurística de abuso do GitHub), o cliente dobra permanentemente o intervalo mínimo daquele recurso pelo resto da execução, em vez de voltar ao mesmo ritmo que causou o bloqueio.

### 9.5 Checkpoints por camada

Cada layer grava seu resultado em `data/.state/` e o relê no rerun. Uma falha na Layer 3 nunca custa as chamadas de API já pagas nas Layers 1 e 2. Interromper com Ctrl+C preserva o progresso.

As layers 3 e 4 são inteiramente locais — iterar nas heurísticas de tagging ou no balanceamento custa segundos de CPU, sem nova rodada de download.

---

## 10. Defeitos encontrados e corrigidos durante a construção

Registrados porque cada um representa uma classe de erro que pode reaparecer em ajustes futuros. Todos têm cobertura de teste em `tests/`.

| # | Defeito | Impacto | Correção |
|---|---|---|---|
| 1 | Alias GraphQL inválido — `pyproject.toml` gerava o alias `mpyproject.toml`, e aliases não aceitam ponto | Layer 1 validava **zero** repositórios | Sanitizador separado para aliases (`[^A-Za-z0-9_]`) e para nomes de repo (onde `.` e `-` são legítimos) |
| 2 | Manifestos de monorepo não eram procurados | `polarsource/polar` e outros descartados como "sem manifesto" | 8 caminhos adicionais (`backend/`, `server/`, `api/`, `src/`) — recuperou 27 repositórios (9%) |
| 3 | Handler `@router.post` classificado como `dependency_injection` | Rótulo semanticamente errado em endpoints | Decorator de rota passou a ter peso decisivo; `Depends` em handler pontua como secundário |
| 4 | Arquivos sem relação com FastAPI entravam no dataset | Função **Click** rotulada "Padrão de roteamento do FastAPI"; `@dataclass` com spec afirmando "pydantic-settings" | Portão de relevância no nível do arquivo — descartou 1.641 arquivos (38%) |
| 5 | Tagging inventava rótulo quando não achava evidência | Código sem padrão recebia `routing` ou `async` por omissão | `analyze()` retorna `None`; o snippet é descartado |
| 6 | `testing` ocupava 62% do dataset | Balanceamento inexistente na prática | Quota por papel na Layer 2 (na origem, economizando I/O) + teto adaptativo na Layer 4 |
| 7 | Teto fixo de 900 por tag não se ajustava ao tamanho do dataset | Com poucos repos, a tag dominante nunca era cortada | Teto adaptativo: 1,5 × mediana das tags relevantes, limitado pelo teto absoluto |
| 8 | `--limit` cortava tiers inteiros (lista ordenada por tier) | Execuções limitadas nunca exercitavam o Tier 3 | Corte estratificado, proporcional por tier |
| 9 | Concordância de gênero em português: "dependência assíncron**o**" | Instrução gramaticalmente incorreta | Adjetivo concorda com o substantivo de cada template |
| 10 | Rate limit secundário da GraphQL (403, espera de 301s) | 5 minutos parados por incidente | Concorrência 4→2, intervalo mínimo de 1s, batch 30→20, e auto-throttle permanente |
| 11 | Bissecção de batch em falha por rate limit **dobrava** as requisições | Agravava o limite que acabara de estourar | Exceção `RetriesExhausted` distinta; bissecção só para erros de query |
| 12 | Repositório com falha transitória gravado como "0 arquivos" | Rerun pularia o repo para sempre | Falha transitória não entra no checkpoint |

**Cobertura de teste:** 46 testes em `tests/test_analysis.py` e `tests/test_dataset.py`, cobrindo parsing de manifestos, classificação de caminhos, corte por AST, tagging, deduplicação, precisão de rótulo e balanceamento.

---

## 11. Reprodutibilidade

### 11.1 Fixação de versão

Todo `manifest.json` grava o `commit_sha` do HEAD no momento do mining. A árvore de arquivos e o conteúdo são buscados **por SHA, não por nome de branch** — a extração é reproduzível mesmo se o repositório receber push durante a execução.

### 11.2 Rastreabilidade ponta a ponta

De qualquer linha do dataset final é possível voltar à origem:

```
balanced_dataset.jsonl
  └── source_snippet_id ──→ processed/snippets.jsonl
  └── source_repo + metadata.file_path ──→ raw/{owner}__{repo}/files/{path}
                                            └── manifest.json (stars, licença, commit_sha)
                                            └── dependencies.json (deps declaradas)
```

Verificado: o arquivo de origem de uma amostra aleatória existe em `raw/` no caminho indicado.

### 11.3 Determinismo

Split treino/validação usa semente fixa (`RANDOM_SEED = 1337`). A geração de instruções e specs é determinística por template. Reprocessar `raw/` com as mesmas heurísticas produz o mesmo dataset.

### 11.4 Como reproduzir

```bash
uv sync
echo "GITHUB_TOKEN=ghp_..." > .env
uv run mine all
```

Layers isoladas: `mine discover` · `mine extract` · `mine normalize` · `mine dataset` · `mine status`

---

## 12. Recomendações

**Antes de treinar:**

1. **Definir a política de licenciamento** (§7.2). É a decisão pendente de maior consequência. Filtrar por MIT/Apache/BSD/ISC preserva ~70% dos repositórios.
2. **Avaliar o filtro de recência** (§7.1). Descartar repositórios sem push há mais de 18–24 meses reduz o risco de misturar padrões Pydantic v1 e v2.
3. **Decidir sobre o idioma nos comentários** (§7.5).

**Para aumentar volume e diversidade:**

4. Adicionar queries de domínio (`fastapi auth`, `fastapi celery`, `fastapi websocket`, `fastapi graphql`) em `SEARCH_QUERIES`.
5. Considerar teto global por repositório, além do teto por tag, para reduzir a concentração de 32,2% nos 10 maiores (§7.3).

**Limpeza técnica:**

6. Remover `MIN_STARS_ABSOLUTE` (configuração morta) e os campos `isArchived`/`isFork` da query GraphQL (§7.7).

---

## 13. Referência de arquivos gerados

| Caminho | Tamanho | Conteúdo |
|---|---|---|
| `data/raw/{owner}__{repo}/` | 43 MB | 301 repositórios: `manifest.json`, `dependencies.json`, `files_index.json` e `files/` com 4.318 arquivos `.py` |
| `data/processed/snippets.jsonl` | 21 MB | 15.582 snippets tagueados |
| `data/processed/snippets_index.json` | 1,1 MB | Lookup `snippet_id → repo_id` |
| `data/final/balanced_dataset.jsonl` | 10 MB | 5.601 exemplos (conjunto completo) |
| `data/final/balanced_dataset_train.jsonl` | 8,8 MB | 4.761 exemplos de treino |
| `data/final/balanced_dataset_val.jsonl` | 1,5 MB | 840 exemplos de validação |
| `data/final/dataset_stats.json` | 598 B | Contagens por tag, tier, papel |
| `data/.state/` | 532 KB | Checkpoints por camada |

**Total: 87 MB.** O diretório `data/` está no `.gitignore`.

---

*Relatório gerado a partir da execução de 21/08/2026. Todos os números foram extraídos dos artefatos em disco e dos logs da execução, não estimados.*
