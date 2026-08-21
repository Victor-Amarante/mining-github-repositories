# zup-mining-repos

Mining de repositórios FastAPI no GitHub para construção de dataset de finetuning.

Pipeline de 4 camadas que busca repositórios por tier, valida a dependência FastAPI
antes de baixar qualquer código, extrai apenas os arquivos de alto sinal, corta
snippets pela AST e gera um dataset balanceado no formato `instruction/input/output`
com spec SDD por exemplo.

## Uso

```bash
uv sync
echo "GITHUB_TOKEN=ghp_..." > .env

uv run mine all              # pipeline completo
uv run mine status           # estado atual
```

Cada layer também roda isolada:

```bash
uv run mine discover    # Layer 1 — busca + valida FastAPI (rede)
uv run mine extract     # Layer 2 — baixa arquivos essenciais (rede)
uv run mine normalize   # Layer 3 — snippets, tags e dedupe (local)
uv run mine dataset     # Layer 4 — balanceia e gera o dataset (local)
```

Isso é proposital: as layers 1 e 2 gastam cota de API, as 3 e 4 não. Iterar nas
heurísticas de tagging ou no balanceamento custa segundos de CPU, sem uma nova
rodada de download.

### Flags

| Flag | Aplica-se a | Efeito |
|---|---|---|
| `--tiers 1 2 3` | discover, all | Quais tiers de busca executar |
| `--limit N` | discover, extract, all | Limita repositórios, **proporcionalmente por tier** |
| `--fresh` | discover, extract, all | Ignora checkpoints e refaz do zero |
| `--scale 2.0` | discover, extract, all | Multiplica a concorrência |
| `--max-per-tag N` | dataset, all | Teto fixo por tag (padrão: adaptativo) |
| `-v` | todos | Log em DEBUG |

Interromper com Ctrl+C preserva os checkpoints — rodar o mesmo comando continua
de onde parou.

## Arquitetura

```
LAYER 1  discover   Search API por tier  ->  dedupe  ->  validação GraphQL em lote
                    Descarta quem não declara fastapi ANTES de baixar código
                            |
LAYER 2  extract    Git Tree recursivo (1 chamada/repo)  ->  seleção local por papel
                    Conteúdo via CDN raw.githubusercontent.com
                            |
LAYER 3  normalize  AST -> snippets completos  ->  tagging  ->  dedupe por hash
                    (local, sem rede)
                            |
LAYER 4  dataset    Instrução + spec SDD  ->  balanceamento  ->  split train/val
                    (local, sem rede)
```

### Decisões de otimização

O gargalo real deste tipo de mining não é CPU, é cota de API. As três decisões
abaixo respondem por quase toda a diferença de tempo de execução:

**1. Validação em lote via GraphQL (Layer 1).** A abordagem direta — Contents API
por arquivo de manifesto — custa 3 a 5 chamadas REST por repositório. Para ~800
candidatos, até 4.000 requisições contra uma cota de 5.000/hora, antes de baixar
uma linha de código útil. A GraphQL resolve com aliases: uma requisição pede, para
30 repositórios de uma vez, o SHA do commit, licença, estrelas e o texto integral
de todos os manifestos. **~27 requisições no lugar de ~4.000** — e o `commit_sha`
já vem junto, eliminando também a chamada extra que seria necessária para obtê-lo.

**2. Conteúdo pelo CDN (Layer 2).** `raw.githubusercontent.com` não consome a cota
de 5.000/h da REST API. Como o download de arquivos é o passo de maior volume
(dezenas de arquivos × centenas de repos), tirá-lo da cota é a diferença entre o
mining caber numa janela de rate limit ou não. A Blobs API fica como fallback.

**3. Uma chamada de árvore por repositório (Layer 2).** `git/trees?recursive=1`
devolve path *e tamanho* de cada blob de uma vez. Toda a seleção de arquivos vira
computação local — zero chamadas de descoberta, e blobs acima de 50KB são
descartados sem nunca serem pedidos.

Complementam: rate limiting por classe de recurso (search, REST, GraphQL e CDN têm
cotas independentes — tratá-las como um pool só faz o miner dormir em backoff
desnecessário), pausa orientada por `X-RateLimit-Remaining`/`Reset` em vez de retry
cego, e checkpoints por layer.

### Decisões de qualidade

**Validação de dependência, não busca textual.** As queries de repository-search
casam com nome, descrição e README — não com código. Um repositório que apenas
*menciona* FastAPI aparece na busca. A Layer 1 lê os manifestos declarados
(`pyproject.toml`, `requirements.txt`, `Pipfile`, `setup.py`/`.cfg`, incluindo os
de monorepo em `backend/`, `server/`, `api/`, `src/`) e descarta quem não declara
fastapi de fato. Na prática, ~35% dos candidatos da busca caem aqui.

**Relevância no nível do arquivo, não só do repositório.** Um repo que declara
FastAPI também tem CLI, scripts e utilitários. Sem essa checagem, `cli/main.py`
casa com o padrão de entrypoint e entra no dataset rotulado como aplicação FastAPI.
A Layer 3 exige que o arquivo importe o ecossistema (fastapi, starlette, pydantic,
sqlalchemy…) — cerca de 40% dos arquivos baixados são descartados aqui.

**Nenhuma tag inventada.** Quando um snippet não apresenta padrão reconhecível, ele
é descartado em vez de receber uma tag por omissão. Rótulo falso é pior que dado
ausente: ensina o modelo a associação errada.

**Instrução e spec descrevem o código real.** A geração é determinística por
template, preenchida com fatos extraídos do próprio snippet (método HTTP, rota,
símbolo, libs em uso). Nada de LLM no meio do pipeline — mantém reprodutibilidade
e evita injetar alucinação nos rótulos que vão treinar outro modelo. Onde o
template poderia prometer mais que o código entrega (`HTTPException` num
`try/except` comum, `pydantic-settings` num getter de env), a frase é condicionada
à evidência.

**Corte por AST, nunca cego.** O snippet vai de `node.lineno` (contando decorators)
até `node.end_lineno`, e os imports anexados são só os que ele referencia.
Verificação: 100% dos exemplos gerados parseiam como Python válido isoladamente.

**Balanceamento em dois pontos.** Quota por papel na seleção de arquivos (um repo
com 200 testes e 5 routers não gasta o teto todo em testes — e os arquivos
descartados nunca chegam a ser baixados) e teto adaptativo por tag no dataset final,
proporcional à mediana das tags relevantes em vez de um número fixo.

### Resultados medidos (execução completa, 2026-08-21)

| Etapa | Resultado |
|---|---|
| Busca (9 queries, 3 tiers) | 410 candidatos únicos |
| Layer 1 — validação FastAPI | 301 validados (73%), 109 descartados |
| Layer 2 — extração | 4.317 arquivos, 14,3 por repo |
| Layer 3 — snippets | 2.676 arquivos relevantes → 15.582 snippets únicos |
| Layer 4 — dataset | 5.601 exemplos de 243 repositórios |
| **Tempo total** | **177s** |

Consumo de API: 11 chamadas de search, 301 REST (6% da cota horária), 21 GraphQL
e 4.318 ao CDN (fora de qualquer cota). Total de 6s em espera por rate limit.

As 4.318 buscas de conteúdo pelo CDN seriam 4.318 chamadas REST no desenho
ingênuo — sozinhas já estourariam a cota de 5.000/h, sem contar as ~1.500 de
validação que a GraphQL substituiu por 21.

## Estrutura dos dados

```
data/
├── raw/{owner}__{repo}/
│   ├── manifest.json        # metadados + commit_sha (fixa a versão minerada)
│   ├── dependencies.json    # dependências declaradas + ecosystem_tags
│   ├── files_index.json     # path -> papel
│   └── files/               # cópia literal, path original (camada de auditoria)
├── processed/
│   ├── snippets.jsonl       # 1 linha = 1 snippet tagueado
│   └── snippets_index.json  # snippet_id -> repo_id
├── final/
│   ├── balanced_dataset.jsonl
│   ├── balanced_dataset_train.jsonl
│   ├── balanced_dataset_val.jsonl
│   └── dataset_stats.json
└── .state/                  # checkpoints por layer
```

`commit_sha` fixa a versão exata minerada: a árvore e os arquivos são buscados por
SHA, não por nome de branch, então a extração é reproduzível mesmo se o repositório
receber push no meio da execução.

### Formato do dataset

```json
{
  "id": "ft_000123",
  "instruction": "Implemente um endpoint POST assíncrono em `/users` usando FastAPI...",
  "input": "# Imports disponíveis:\nfrom fastapi import APIRouter, Depends",
  "output": "@router.post(\"/users\", response_model=UserRead)\nasync def create_user(...)",
  "spec_sdd": {
    "objetivo": "...",
    "padrao_aplicado": "...",
    "quando_usar": "..."
  },
  "tags": ["routing", "dependency_injection"],
  "tier": 2,
  "source_repo": "owner/repo",
  "source_snippet_id": "sha256:...",
  "metadata": { "file_path": "...", "role": "router", "repo_stars": 1200, "lines": 14 }
}
```

`source_repo` e `source_snippet_id` mantêm o link até a origem — de qualquer linha
do dataset final dá para voltar ao arquivo em `raw/`.

## Papéis e tags

**Papéis** (do path do arquivo): `entrypoint`, `router`, `schema`, `dependency`,
`middleware`, `config`, `database`, `test`.

**Tags** (do padrão no código, via AST): `routing`, `validation`,
`dependency_injection`, `error_handling`, `async`, `config`, `testing`, `database`.
Tópicos transversais (`auth`, `streaming`, `file_upload`) entram só como
secundários.

Um handler de rota que usa `Depends()` é `routing` com `dependency_injection`
secundário — a rota é o que ele *é*; `Depends` é o que ele *usa*. Já uma função
sem decorator de rota que provê um recurso é `dependency_injection` como padrão
principal.

## Licenciamento

O dataset contém código de terceiros. `manifest.json` guarda a licença SPDX de cada
repositório e `source_repo` mantém a atribuição em toda linha do dataset final —
filtre por licença antes de qualquer uso que exija.
