# Busca semântica sobre Jira 8.14 e Confluence 4.2.4, via MCP

Busca unificada sobre duas instâncias Atlassian legadas on-premise, exposta
para clientes de IA através do Model Context Protocol.

**Estado: Fase 1 concluída.** Busca lexical BM25, sem nenhum componente de
machine learning denso. A camada de embeddings e a busca híbrida são a Fase 2.

---

## O problema

A busca nativa das duas instâncias é lexical: encontra só as palavras exatas
digitadas. Se a página diz *"certificado vencido"* e a pessoa procura
*"certificado expirado"*, o resultado é zero. No Confluence 4.2.4 o ranking de
relevância é primitivo, o que agrava. O conhecimento existe na empresa, mas não
é encontrável.

Restrições não negociáveis do projeto:

- upgrade das instâncias está fora de escopo;
- nenhum conteúdo sai da rede, nenhuma API externa, modelos rodam local;
- acesso **somente leitura**: o sistema nunca escreve no Jira ou no Confluence;
- nenhuma ferramenta de mercado fala com Confluence 4.2.4, então a camada de
  extração foi construída à mão.

---

## Arquitetura

Dois ciclos independentes que se comunicam **apenas pelo índice**.

```
CICLO 1 - indexação (offline, cron)

   Jira 8.14                Confluence 4.2.4
   REST v2 + PAT            XML-RPC /rpc/xmlrpc (confluence2)
        |                        |
        +-----------+------------+
                    |  extração incremental somente leitura
                    v
        DOCUMENT STORE LOCAL (SQLite)      <-- texto já limpo e normalizado
                    |
                    |  chunking (1400 chars, 200 de sobreposição)
                    v
                 QDRANT     vetor sparse BM25 (populado)
                            vetor denso 1024 (declarado, vazio - Fase 2)


CICLO 2 - consulta (tempo real)

   Cliente de IA --> Servidor MCP --+--> Qdrant           (índice: conteúdo)
                                    +--> Jira REST        (ao vivo: exatidão)
                                    +--> Confluence XML-RPC (ao vivo: página inteira)
```

### Por que o document store é a peça central

A extração é a parte cara e frágil. O XML-RPC do Confluence é lento, não pagina
e não filtra por data. Reextrair tudo a cada mudança de estratégia de indexação
levaria horas.

Por isso extração e indexação são **etapas separadas e desacopladas**: a
extração grava o texto limpo num store local; a indexação lê desse store.
Mudar o tamanho das fatias, trocar o modelo de embedding ou reconstruir o
índice do zero é operação de minutos e não toca em nenhuma instância Atlassian:

```bash
python -m indexer.sync index --recreate     # reconstrói tudo, zero requisições ao Atlassian
```

### Índice x ao vivo: uma separação deliberada

| | índice (`search_knowledge_base`) | ao vivo (`search_jira_jql`, `get_*`) |
|---|---|---|
| responde | conteúdo, significado, "onde está escrito" | contagem, filtro por status, ordenação |
| frescor | atraso de 15 min (Jira) a 24 h (Confluence) | agora |
| formato | trechos relevantes | conjunto exato |

Contagem, filtro por status e ordenação **nunca** saem do índice: ele tem
atraso e a resposta precisa estar correta agora. Essa regra está escrita nas
docstrings das ferramentas, que são o contrato de roteamento lido pelo modelo.

---

## Instalação

Requer Python **3.12+** e Docker.

```bash
git clone <este-repo> && cd rag-confluence-jira

python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
# Se a máquina não tiver ensurepip/pip (o caso deste host), use uv:
#   curl -LsSf https://astral.sh/uv/install.sh | sh
#   uv venv --python 3.13 .venv
#   uv pip install --python .venv/bin/python -r requirements-dev.txt

docker compose up -d          # Qdrant em 127.0.0.1:6333
cp .env.example .env          # e preencha
```

O código é escrito para 3.12+ e não usa sintaxe exclusiva da 3.13; o venv pode
ser trocado de versão sem alterar código (relevante para a Fase 2, que trará
torch/ROCm).

### Pré-cache do modelo BM25 (host sem internet)

O runtime é **offline por padrão e nunca baixa nada sozinho**. Se o artefato
faltar, ele aborta dizendo exatamente o que falta e como resolver.

Em uma máquina com rede:

```bash
FASTEMBED_CACHE_DIR=./models/fastembed ALLOW_MODEL_DOWNLOAD=1 \
    python -m scripts.precache_models
```

Copie o diretório `models/fastembed` inteiro (≈55 KiB) para o host restrito e
aponte `FASTEMBED_CACHE_DIR` para ele. O artefato do `Qdrant/bm25` é apenas
lista de stopwords e stemmer — não é uma rede neural.

---

## Configuração

Todas as variáveis estão documentadas em `.env.example`.

| variável | obrigatória | observação |
|---|---|---|
| `JIRA_URL` | se usar Jira | |
| `JIRA_PAT` | se usar Jira | PAT via header Bearer; PATs existem a partir da 8.14 |
| `JIRA_PROJECTS` | **sim, não vazio** | vazio **aborta** — ver Segurança |
| `CONFLUENCE_URL` | se usar Confluence | |
| `CONFLUENCE_USER` / `CONFLUENCE_PASSWORD` | se usar Confluence | a 4.2.4 **não** suporta PAT |
| `CONFLUENCE_SPACES` | **sim, não vazio** | vazio **aborta** — ver Segurança |
| `CONFLUENCE_INCREMENTAL_STRATEGY` | não | `full_scan` (única na Fase 1) |
| `QDRANT_URL` | não | `http://127.0.0.1:6333` |
| `QDRANT_COLLECTION` | não | `atlassian_kb` |
| `STORE_PATH` | não | `./data/documents.sqlite3` |
| `FASTEMBED_CACHE_DIR` | não | `./models/fastembed` |
| `ALLOW_MODEL_DOWNLOAD` | não | `0`. Ligue só para pré-cachear |
| `VERIFY_SSL` | não | `0` apenas para certificado autoassinado |

Pré-requisito do lado do Confluence: a **Remote API (XML-RPC)** precisa estar
habilitada em *Administração → Configuração Geral → Remote API*. Ela vem
desligada por padrão em muitas instalações; sem ela `/rpc/xmlrpc` responde
404/403 e nada funciona.

---

## Uso

```bash
python -m indexer.sync extract --only confluence      # extrai para o store
python -m indexer.sync extract --only jira --full     # ignora o incremental
python -m indexer.sync index                          # store -> Qdrant
python -m indexer.sync run                            # extract + index
python -m indexer.sync reconcile --only jira          # remove issues apagadas
python -m indexer.sync status                         # estado do store e do índice
```

Flags úteis:

| flag | padrão | efeito |
|---|---|---|
| `--full` | off | reextrai tudo, ignorando versões e cursor |
| `--no-count-attachments` | contagem ligada | desliga a contagem de anexos (economiza 1 chamada XML-RPC por página) |
| `--no-labels` | labels ligados | desliga a busca de labels (economiza 1 chamada XML-RPC por página) |
| `--no-include-comments` | comentários ligados | não busca a thread completa das issues |
| `--reindex-all` | off | reindexa o store inteiro sem tocar no Atlassian |
| `--recreate` | off | apaga e recria a coleção do Qdrant |
| `--max-chars` / `--overlap` | 1400 / 200 | experimentar chunking sem reextrair |

### Cron

```cron
# Jira a cada 15 min: o incremental é server-side via JQL e é barato.
*/15 * * * *  cd /opt/rag && .venv/bin/python -m indexer.sync run --only jira      >> /var/log/rag/jira.log 2>&1

# Confluence 1x por noite: getPages é caro e não tem filtro por data.
0 2 * * *     cd /opt/rag && .venv/bin/python -m indexer.sync run --only confluence >> /var/log/rag/confluence.log 2>&1

# Reconciliação de deleções do Jira, semanal, fora do caminho quente.
30 3 * * 0    cd /opt/rag && .venv/bin/python -m indexer.sync reconcile --only jira >> /var/log/rag/reconcile.log 2>&1
```

O log é JSON por linha, em **stderr**. Cada rodada fecha com uma linha de
métricas: páginas listadas, buscadas, puladas por versão inalterada, falhas,
anexos contados, tempo total e média por `getPage`.

---

## Servidor MCP

```bash
python -m mcp_server.server        # transporte stdio
```

Configuração no cliente de IA:

```json
{
  "mcpServers": {
    "atlassian-kb": {
      "command": "/opt/rag/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/opt/rag",
      "env": { "PYTHONPATH": "/opt/rag" }
    }
  }
}
```

### Ferramentas

| ferramenta | fonte | para quê |
|---|---|---|
| `search_knowledge_base(query, limit, source, project, space_key)` | índice | encontrar conteúdo nas duas fontes por assunto |
| `search_jira_jql(jql, max_results)` | Jira ao vivo | contagem, filtro, ordenação, conjunto exato |
| `get_jira_issue(issue_key, include_comments)` | Jira ao vivo | issue inteira, estado atual, thread completa |
| `get_confluence_page(page_id)` | Confluence ao vivo | página inteira quando a fatia não basta |

Cada docstring diz explicitamente **quando usar e quando NÃO usar** a
ferramenta — é assim que o modelo roteia. Todo resultado de busca traz a URL de
origem, sempre.

> O SDK `mcp` 2.x renomeou `FastMCP` para `MCPServer`; é a mesma classe. O
> código usa a API 2.x. Para voltar ao nome antigo bastaria fixar `mcp<2` e
> trocar o import.

---

## Segurança

**O indexador achata o controle de acesso.** Ele entra com um usuário de
serviço, então tudo que ele consegue ler entra no índice e passa a ser visível
para qualquer pessoa que consulte o MCP. Se existem espaços restritos no
Confluence — RH, jurídico, financeiro — indexá-los expõe o conteúdo deles a
toda a organização.

Por isso:

- `JIRA_PROJECTS` ou `CONFLUENCE_SPACES` vazios **abortam a execução** com
  mensagem explícita. Não existe modo "indexar tudo por padrão", em nenhuma
  circunstância;
- o usuário de serviço deve ter permissão somente leitura e ser membro apenas
  dos projetos e espaços realmente listados;
- o Qdrant faz bind em `127.0.0.1` e não é exposto na rede;
- **nesta instalação a topologia torna isso crítico**: dos 716 espaços, 651 são
  um por cliente (27.753 páginas). Indexá-los tornaria o conteúdo de cada
  cliente pesquisável por qualquer pessoa com acesso ao MCP. O escopo inicial
  cobre apenas espaços internos e de produto;
- o sistema nunca escreve: não há nenhuma chamada de escrita em nenhum dos dois
  conectores.

---

## Armadilhas do Confluence 4.2.4 tratadas no parser

Sete bugs diagnosticados em ambiente real, todos cobertos por teste em
`tests/test_parser.py`.

1. **Fragmento sem elemento raiz.** O storage format são vários irmãos sem raiz
   única; o parser XML para no primeiro nó e a página inteira se perde em
   silêncio. O conteúdo é envolvido em `<root>` com os namespaces `ac:` e `ri:`
   declarados antes do parse.
2. **Entidades HTML nomeadas destruindo acentos.** O parser XML não conhece
   `&ecirc;`, `&ccedil;`, `&nbsp;` e apaga a entidade *junto com a letra* —
   "você" vira "Voc". Numa base em português isso corrompe conteúdo em massa.
   O unescape acontece antes do parse e é **seletivo**: as cinco entidades
   nativas do XML (`&lt;` `&gt;` `&amp;` `&quot;` `&apos;`) são preservadas,
   senão um exemplo de markup escapado viraria tag de verdade.
3. **Cabeçalho quebrando em duas linhas.** Marcador antes e depois da tag gera
   `"# \nTítulo"` e arruína o chunking por seção. A tag inteira é substituída
   pelo texto já montado em uma linha só.
4. **Blocos de código em CDATA.** Ficam em `ac:plain-text-body`; um `unwrap()`
   genérico das macros descarta o CDATA inteiro e todo snippet da base
   desaparece. As macros de código são tratadas antes de qualquer unwrap — e
   tanto `<ac:macro>` (usada na 4.2) quanto `<ac:structured-macro>` (só a
   partir da 4.3, presente em bases migradas).
5. **Palavras colando quando a tag não é fechada.** `<p>texto solto<p>outro`
   vira `texto soltooutro`. Separadores são inseridos nos limites de bloco.
6. **Fallback de parser obrigatório.** Base de 2012 tem XML mal formado. A
   boa-formação é testada com `recover=False` **antes** de parsear, porque o
   lxml em modo recuperação não levanta exceção: ele conserta em silêncio e
   descarta pedaços. Falhou, cai para `html.parser`, que é tolerante a erro.
7. **`allow_none=True` no `ServerProxy`.** O Confluence 4.x não aceita `<nil/>`
   e a chamada falha. Fica desligado.

Além disso, wiki markup residual pré-4.0 nunca migrado (`h2. Título`,
`{code}`, `{noformat}`, macro `unmigrated-wiki-markup`) é convertido num passe
próprio, reaproveitado para a `description` e os comentários do Jira, que no
Server 8.14 também vêm em wiki markup.

---

## Decisões e limitações conhecidas

### Números medidos na instância real (set/2026)

| medida | valor |
|---|---|
| espaços visíveis ao usuário de serviço | **716** (687 globais, 29 pessoais) |
| páginas totais na instância | **40.381** + 348 blogs |
| páginas em espaços de **cliente** | 27.753 (651 espaços) |
| páginas em espaços internos/produto | 12.432 (36 espaços) |
| `getSpaces` | 0,4 s |
| `getPages` num espaço de 6.129 páginas | **70 a 181 s** (uma única chamada) |
| `getPage` | 140 ms/página, storage format de ~10.700 chars |
| extração completa por página | ~400 ms — são **3 round-trips**: `getPage` + `getAttachments` + `getLabelsById` |
| issues no escopo VENDAS + ECOMMERCE | 24.928 |
| `POST /search` do Jira | 1,5 ms/issue (100 por requisição) |
| comentários do Jira | 43 ms/issue, média de 1,0 comentário por issue |
| carga inicial do Jira completa | ~18 min |

Consequências práticas dessas medidas:

- **o `CONFLUENCE_TIMEOUT` padrão é 300 s.** Um `getPages` de 181 s estourava o
  padrão anterior de 120 s. Não abaixe sem medir o maior espaço do escopo;
- **cada página custa 3 chamadas, não 1.** `--no-count-attachments` e
  `--no-labels` cortam um terço cada. A contagem de anexos vem ligada para
  produzir o número pedido na primeira rodada; depois disso, desligue;
- **espaço grande fica ~30 min sem emitir log** entre "listado" e "concluído".
  Por isso existe a linha `progresso do espaço` a cada 250 documentos, com
  percentual, ritmo e ETA.

**Incremental do Confluence é caro por limitação da API.** `getPages()` devolve
`PageSummary`, que nesta versão **não** traz `version` nem `modified` — só o
`getPage()` completo traz. O incremental não consegue evitar o round-trip; ele
evita o parse, o chunking e o upsert. A escolha está registrada em
`CONFLUENCE_INCREMENTAL_STRATEGY` e a estratégia é uma interface, para que uma
alternativa mais barata entre sem tocar no conector. As métricas de cada rodada
existem para decidir isso com dado real.

**Contagem de anexos custa +1 chamada por página.** `getAttachments` dobra os
round-trips. Fica ligada por padrão, com o tempo gasto reportado em separado
(`tempo_anexos_s`) para permitir desligar com `--no-count-attachments` depois
da primeira noite. Anexos são **contados, não indexados**.

**Fuso horário do JQL.** `updated >= "yyyy-MM-dd HH:mm"` é interpretado no fuso
do usuário do Jira, não em UTC. O cursor é o próprio campo `updated` devolvido
pelo Jira, com o offset dele, nunca o relógio da máquina do indexador. A janela
de segurança de 1 hora cobre a alteração concorrente no minuto de virada.

**Acentuação.** O tokenizer BM25 do fastembed faz stopwords e stemming
(configurado para português), mas não normaliza diacríticos. A normalização
NFKD é aplicada simetricamente na indexação e na consulta; o texto original
permanece intacto no payload. Efeito colateral aceito: numa base mista PT/EN, o
stemming português também é aplicado ao inglês.

**Paginação profunda do Jira.** `startAt`/`maxResults` com dados mudando
durante a rodada pode deslocar a janela. Mitigado por `ORDER BY updated ASC`
mais a janela de segurança na rodada seguinte.

**Deleções.** No Confluence, detectadas no fluxo normal por diff dos IDs de
`getPages`/`getBlogEntries` — é de graça, a listagem já foi feita. No Jira,
ficam no comando `reconcile`, semanal, fora do caminho de 15 minutos. Se a
listagem de um espaço falhar, o diff daquele espaço é pulado para não apagar
conteúdo por engano.

**Fora de escopo na Fase 1:** anexos do Confluence (só contagem), comentários
de página do Confluence, changelog/histórico de issues do Jira. Blog posts do
Confluence **estão** incluídos (`getBlogEntries`), porque é onde costumam estar
os post-mortems.

---

## Testes

```bash
.venv/bin/python -m pytest tests/ -q
```

72 testes, nenhum depende de instância real. Cobrem, no parser: HTML mal
formado com tag não fechada, entidades acentuadas, wiki markup residual,
tabela sem `tbody`, lista aninhada, página vazia e página só com whitespace,
bloco de código em CDATA nas duas formas de macro, e link interno
`ac:link`/`ri:page`. No chunking: parágrafo único gigante, documento curto,
título sempre prefixado, IDs estáveis entre execuções e fronteira de cabeçalho.

---

## Critérios de aceite da Fase 1

| critério | como validar |
|---|---|
| Segunda execução consecutiva processa quase nada | `sync index` duas vezes: a segunda reporta `documentos: 0` |
| Queda no meio do sync não perde progresso | `Ctrl-C` durante `extract`; o store commita a cada 25 documentos e o cursor do Jira é salvo em `finally` |
| Busca por identificador exato acha o documento certo | `ERR-4012` e `PROJ-1234` retornam o documento correto em primeiro lugar |
| Busca com e sem acento retorna o mesmo resultado | `configuração do proxy` e `configuracao do proxy` devolvem a mesma lista |
| MCP conecta e as quatro ferramentas respondem | handshake stdio, `list_tools` devolve as quatro |
| Todo resultado traz URL clicável | campo `url` presente em todo hit |
| Testes do parser passando, incluindo acentuados | `pytest tests/ -q` |
| Escopo vazio aborta | `JIRA_PROJECTS= python -m indexer.sync status` sai com código 2 |

---

## Estrutura

```
config.py                        env, validação de escopo, logging JSON em stderr
connectors/confluence_legacy.py  parser de storage format + cliente XML-RPC
connectors/jira_server.py        REST v2, PAT, JQL incremental, comentários
store/documents.py               SQLite: documents, confluence_versions, sync_state
indexer/chunking.py              fatiamento com título prefixado e IDs uuid5
indexer/index.py                 Qdrant: schema, BM25 sparse, upsert, search
indexer/sync.py                  CLI extract / index / run / reconcile / status
mcp_server/server.py             quatro ferramentas MCP
scripts/precache_models.py       pré-cache do BM25 para operação offline
tests/                           parser e chunking
```

## Fase 2 (não implementada)

Camada de embeddings densos e busca híbrida. O schema do Qdrant já declara o
vetor denso de 1024 dimensões, vazio, justamente para que a Fase 2 não exija
migração de schema nem reextração: bastará popular o vetor a partir do document
store que já existe.
