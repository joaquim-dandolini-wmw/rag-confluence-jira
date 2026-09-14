<h1 align="center">atlassian-kb</h1>

<p align="center">
  <strong>Perguntar em português para o Jira e o Confluence da WMW, de dentro do seu chat de IA.</strong>
</p>

<p align="center">
  <code>https://wmw-rag.wmw.com.br/</code><br>
  <sub>busca híbrida · somente leitura · 65.756 documentos indexados</sub>
</p>

---

O Jira 8.14 e o Confluence 4.2.4 da empresa guardam anos de runbooks, decisões,
post-mortems e chamados. A busca nativa dos dois só acha a **palavra exata**: se
a página diz *"certificado vencido"* e você procura *"certificado expirado"*, o
resultado é zero. O conhecimento existe, mas não é encontrável.

Este projeto extrai as duas instâncias para um índice próprio, indexa por
**significado** além de por palavra, e publica tudo como um servidor **MCP** —
o protocolo que Claude, ChatGPT, Codex, Cursor, Gemini e companhia usam para
falar com ferramentas externas.

Na prática, você digita no seu chat de IA de sempre:

> *Por que o boleto não é gerado em alguns pedidos? Cite os links.*

e ele procura nas duas instâncias, lê as páginas e as issues, e responde com a
URL de origem de cada afirmação.

**Nada sai da rede.** A extração, o índice e os modelos rodam em máquina nossa;
nenhum conteúdo é enviado para API de terceiro. O acesso é **somente leitura**:
não existe uma única chamada de escrita em nenhum dos dois conectores.

---

## Conectar o seu cliente de IA

O servidor fala **MCP sobre Streamable HTTP**, que é o transporte remoto padrão
do protocolo. Todo cliente moderno suporta isso **nativamente** — não é preciso
instalar Node.js, nem `npx mcp-remote`, nem ponte nenhuma. Se um guia por aí
mandar você fazer isso, ele está desatualizado.

Você só precisa de uma coisa, em qualquer cliente:

```
https://wmw-rag.wmw.com.br/
```

> A barra final faz parte do endereço: o endpoint MCP está na **raiz**, não em
> `/mcp`. E não há senha, token nem OAuth — escolha "sem autenticação" quando o
> cliente perguntar.

### Passo 0 — você alcança o servidor?

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://wmw-rag.wmw.com.br/
```

**`400` é a resposta certa.** Significa que chegou lá — o `curl` não fala MCP,
então o servidor recusa. Se travar ou devolver `000`, o problema é rede e
configurar o cliente não vai adiantar.

---

### Claude Code

```bash
claude mcp add --transport http --scope user atlassian-kb https://wmw-rag.wmw.com.br/
claude mcp list
```

A segunda linha tem que mostrar `atlassian-kb: ... - ✔ Connected`.

`--scope user` deixa o servidor disponível em todos os seus projetos. Troque por
`--scope project` para versionar no repositório (grava um `.mcp.json`) ou omita
para valer só no diretório atual.

### Claude Desktop

**+** ao lado da caixa de mensagem → **Connectors** → **Add custom connector** →
cole a URL → **Add**.

Tem que aparecer `atlassian-kb` com quatro ferramentas.

### claude.ai (navegador e celular)

**Settings** → **Connectors** → **Add custom connector** → cole a URL → **Add**.

Aqui quem conecta são os servidores da Anthropic, não o seu computador — por
isso funciona também fora da rede da empresa.

### ChatGPT

Precisa de **plano pago** (Plus, Pro, Business, Enterprise ou Edu). Em
Business/Enterprise/Edu, um administrador pode ter de liberar conectores para o
workspace antes.

1. **Settings** → **Apps & Connectors** → **Advanced settings** → ligue
   **Developer mode**.
2. Volte para **Apps & Connectors** → **Create**.
3. Preencha:
   - **Name**: `atlassian-kb`
   - **Description**: `Busca no Jira e no Confluence da WMW`
   - **MCP Server URL**: `https://wmw-rag.wmw.com.br/`
   - **Authentication**: **No authentication**
4. **Create**. As quatro ferramentas aparecem sozinhas.

Depois, ative o conector no seletor de ferramentas da conversa.

### Codex CLI

```bash
codex mcp add atlassian-kb --url https://wmw-rag.wmw.com.br/
codex mcp list
```

Ou, direto no `~/.codex/config.toml`:

```toml
[mcp_servers.atlassian-kb]
url = "https://wmw-rag.wmw.com.br/"
```

### Cursor

Edite `~/.cursor/mcp.json` (global) ou `.cursor/mcp.json` (só no projeto):

```json
{
  "mcpServers": {
    "atlassian-kb": {
      "url": "https://wmw-rag.wmw.com.br/"
    }
  }
}
```

Confira em **Settings** → **MCP**: o servidor tem que ficar verde, com quatro
ferramentas.

### VS Code (GitHub Copilot)

Um comando, sem editar arquivo:

```bash
code --add-mcp '{"name":"atlassian-kb","type":"http","url":"https://wmw-rag.wmw.com.br/"}'
```

Ou, para versionar junto com o repositório, crie `.vscode/mcp.json`:

```json
{
  "servers": {
    "atlassian-kb": {
      "type": "http",
      "url": "https://wmw-rag.wmw.com.br/"
    }
  }
}
```

As ferramentas aparecem no Chat em modo **Agent**, no ícone de ferramentas.

### Gemini CLI

```bash
gemini mcp add --transport http atlassian-kb https://wmw-rag.wmw.com.br/
```

Ou no `~/.gemini/settings.json` — repare que aqui a chave é `httpUrl`:

```json
{
  "mcpServers": {
    "atlassian-kb": {
      "httpUrl": "https://wmw-rag.wmw.com.br/"
    }
  }
}
```

Dentro do CLI, `/mcp` lista o que conectou.

### Windsurf

Edite `~/.codeium/windsurf/mcp_config.json` — aqui a chave é `serverUrl`:

```json
{
  "mcpServers": {
    "atlassian-kb": {
      "serverUrl": "https://wmw-rag.wmw.com.br/"
    }
  }
}
```

### Outro cliente qualquer

Qualquer coisa que fale MCP serve. O que muda de um para outro é só o nome da
chave que guarda a URL:

| cliente | onde | chave da URL |
|---|---|---|
| Claude Code, Claude Desktop, claude.ai, ChatGPT | pela interface / CLI | — |
| Codex CLI | `~/.codex/config.toml` | `url` |
| Cursor | `~/.cursor/mcp.json` | `url` |
| VS Code Copilot | `.vscode/mcp.json` | `url` + `"type": "http"` |
| Gemini CLI | `~/.gemini/settings.json` | `httpUrl` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `serverUrl` |

Se o seu cliente pedir um "tipo" de transporte, escolha **`http`** /
**Streamable HTTP** — nunca `sse`, que é o transporte antigo e este servidor não
atende.

---

## Como perguntar bem

A busca **escolhe sozinha** entre exata e semântica, olhando o que você digitou:

| você escreve | o que acontece |
|---|---|
| `VENDAS-14993`, `PRUPSYNCPRODUTOS` | busca exata pelo termo |
| "por que o boleto não é gerado" | busca por significado |
| "erro no PRUPSYNCPRODUTOS ao sincronizar" | as duas, fundidas |

Descreva o problema **com as suas palavras**. Você não precisa adivinhar o
vocabulário de quem escreveu a página: *"dados da fatura para pagamento em
banco"* encontra a página que fala em *"boleto bancário"*.

Perguntas de contagem e listagem — *"quantos bugs abertos em VENDAS"* — não
passam pelo índice; o cliente vai sozinho na consulta ao vivo do Jira.

### As quatro ferramentas

| ferramenta | fonte | para quê |
|---|---|---|
| `search_knowledge_base(query, limit, source, project, space_key)` | índice | encontrar conteúdo nas duas fontes, por assunto ou por significado |
| `search_jira_jql(jql, max_results)` | Jira ao vivo | contagem, filtro por status, ordenação, conjunto exato |
| `get_jira_issue(issue_key, include_comments)` | Jira ao vivo | a issue inteira, estado atual, thread completa |
| `get_confluence_page(page_id)` | Confluence ao vivo | a página inteira, quando a fatia não basta |

Cada docstring diz explicitamente **quando usar e quando NÃO usar** a
ferramenta — é assim que o modelo decide sozinho para onde ir. Todo resultado de
busca traz a URL de origem, sempre.

---

## Quando não funcionar

| sintoma | causa provável | o que fazer |
|---|---|---|
| o `curl` do passo 0 trava ou dá `000` | rede | é preciso alcançar `wmw-rag.wmw.com.br`. No Wi-Fi isso oscila |
| o cliente conecta mas não lista ferramenta | transporte errado | tem que ser `http` / Streamable HTTP, não `sse` |
| `404` ou "endpoint not found" | path errado | o endereço é a **raiz**, com a barra final. Não é `/mcp` nem `/sse` |
| alguém mandou usar `npx mcp-remote` | guia velho | não precisa. Use a URL direto |
| `http://10.2.1.132:8765/mcp` não responde | endereço morto | mudou o IP, entrou HTTPS e o endpoint foi para a raiz. Refaça a configuração |

Um teste que não depende de cliente nenhum — se isto responder, o servidor está
bom e o problema é configuração local:

```bash
curl -s -X POST https://wmw-rag.wmw.com.br/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"curl","version":"1"}}}'
```

---

## Segurança — leia antes de indexar mais alguma coisa

**O indexador achata o controle de acesso.** Ele entra com um usuário de
serviço, então tudo que *ele* enxerga entra no índice e passa a ser visível para
qualquer pessoa que consulte o MCP. As permissões por espaço do Confluence
**não são aplicadas na busca**.

Nesta instalação, hoje:

- o escopo do Confluence está em `auto` — **os 716 espaços** visíveis ao usuário
  de serviço estão indexados, inclusive os 651 espaços de cliente;
- `https://wmw-rag.wmw.com.br/` resolve na internet e **responde sem
  credencial nenhuma**. O nome está público nos logs de Certificate Transparency
  do certificado Let's Encrypt;
- não há registro de quem perguntou o quê.

Enquanto não houver autenticação na frente do proxy, **trate o conteúdo indexado
como material publicado**. Se isso não for aceitável, o caminho é reduzir
`CONFLUENCE_SPACES` para a lista de espaços internos e pôr autenticação no
proxy — não adianta contar com o firewall, que não protege este endereço.

O que o código garante, independentemente disso:

- `JIRA_PROJECTS` ou `CONFLUENCE_SPACES` **vazios abortam a execução**, com
  mensagem explícita. Não existe "indexar tudo por padrão" por acidente:
  `auto` tem que ser escrito de propósito;
- o Qdrant faz bind em `127.0.0.1` e não é exposto na rede;
- o sistema nunca escreve no Jira nem no Confluence.

---

## Como funciona

Dois ciclos independentes, que se comunicam **apenas pelo índice**.

```
CICLO 1 — indexação (offline, janela noturna)

   Jira 8.14                Confluence 4.2.4
   REST v2 + PAT            XML-RPC /rpc/xmlrpc (confluence2)
        |                        |
        +-----------+------------+
                    |  extração incremental, somente leitura
                    v
        DOCUMENT STORE LOCAL (SQLite)      <- texto já limpo e normalizado
                    |
                    |  chunking (1400 chars, 200 de sobreposição)
                    v
                 QDRANT     sparse BM25  +  denso 1024 (e5-large)


CICLO 2 — consulta (tempo real)

   Cliente de IA --> Servidor MCP --+--> Qdrant             (índice: conteúdo)
                                    +--> Jira REST          (ao vivo: exatidão)
                                    +--> Confluence XML-RPC (ao vivo: a página inteira)
```

### Por que o document store é a peça central

A extração é a parte cara e frágil. O XML-RPC do Confluence é lento, não pagina
e não filtra por data — uma rodada completa leva horas. Por isso extração e
indexação são **etapas separadas**: a extração grava o texto limpo num SQLite
local, e a indexação lê desse store.

Mudar o tamanho das fatias, trocar o modelo de embedding ou reconstruir o índice
do zero vira operação de minutos, que **não toca em nenhuma instância
Atlassian**:

```bash
python -m indexer.sync index --recreate     # zero requisições ao Jira/Confluence
```

### Índice x ao vivo: uma separação deliberada

| | índice (`search_knowledge_base`) | ao vivo (`search_jira_jql`, `get_*`) |
|---|---|---|
| responde | conteúdo, significado, "onde está escrito" | contagem, filtro por status, ordenação |
| frescor | até a última janela noturna | agora |
| formato | trechos relevantes | conjunto exato |

Contagem e ordenação **nunca** saem do índice: ele tem atraso, e essas respostas
precisam estar corretas agora, não aproximadamente.

### A busca: exata, densa, ou as duas — decidido pela consulta

O plano original previa fusão RRF como resposta única. Ela está implementada e
ponderada, mas **medida, a fusão não atende os dois critérios ao mesmo tempo**:
o RRF soma `1/(k+rank)`, então todo candidato que o BM25 traz ocupa posição boa
mesmo sendo irrelevante para uma pergunta em prosa — e, na direção oposta, um
documento que só o BM25 acha nunca bate um que aparece nas duas listas.

Em vez de um compromisso que perde dos dois lados, **a consulta escolhe o
instrumento** (`indexer/index.py:classify_query`):

| formato da consulta | modo | por quê |
|---|---|---|
| só um identificador — `VENDAS-14993` | `bm25` | é onde o exato ganha e o denso não tem o que oferecer |
| prosa, sem identificador | `dense` | é onde o sinônimo vive |
| identificador dentro de uma frase | `hybrid` | os dois contribuem |

Siglas curtas da própria base (`APP`, `SQL`, `ERP`) não são confundidas com
chave de issue. `--mode` continua disponível para comparação A/B.

O que o denso acrescenta, medido na instância real com consulta em **sinônimo
puro** — nenhuma palavra de conteúdo em comum com o alvo. O número é a posição
do documento certo:

| consulta | alvo | bm25 | denso | híbrido | **auto** |
|---|---|---|---|---|---|
| "dados da fatura para pagamento em banco" | *Informações do Boleto Bancário do Pedido* | não acha | 8 | 55 | **8** |
| "administração de celulares dos vendedores" | *Gerenciamento de Smartphones* | não acha | 13 | 64 | **13** |

E o custo, com identificador exato (alvo em 1º lugar, 3 execuções):

| consulta | bm25 | denso | híbrido | **auto** |
|---|---|---|---|---|
| `VENDAS-14993` | 3/3 | 0/3 | 3/3 | **3/3** |
| `PRUPSYNCPRODUTOS` | 3/3 | 0/3 | **0/3** | **3/3** |

### O reranker: uma segunda etapa, hoje desligada

O bi-encoder vetoriza consulta e documento **separadamente**, então o vetor do
documento tem que servir para toda consulta possível. Por isso ele acha o
documento por sinônimo mas não o coloca no topo. Um cross-encoder
(`BAAI/bge-reranker-v2-m3`) lê os dois **juntos** num forward só — muito mais
preciso, e caro, por isso rodaria só sobre os 30 candidatos que a primeira etapa
filtrou:

| consulta em sinônimo puro | sem reranker | com reranker |
|---|---|---|
| "dados da fatura para pagamento em banco" | 8º | **3º** |
| "administração de celulares dos vendedores" | fora da página | **5º** |

Duas decisões que só apareceram medindo: **consulta com identificador não passa
pelo reranker** (o cross-encoder julga relevância semântica, e `VENDAS-14993`
quase não tem semântica — com ele o alvo caía de 1º para 2º); e **mais
candidatos piora** — de 20 para 80, o alvo caiu da posição 3 para a 5, porque
mais competição dilui.

Está **desligado nesta instalação** (`RERANK_ENABLED=0`): na máquina com GPU
custava 475 ms por consulta em prosa contra 19 ms sem ele, e aqui, em CPU, não
compensa. Falha do reranker degrada para a ordem da primeira etapa, com aviso no
log, em vez de quebrar a busca.

---

## As armadilhas do Confluence 4.2.4

Nenhuma ferramenta de mercado fala com o Confluence 4.2.4, então a camada de
extração foi construída à mão. Oito bugs diagnosticados em ambiente real, todos
cobertos por teste:

1. **Fragmento sem elemento raiz.** O storage format são vários irmãos sem raiz
   única; o parser XML para no primeiro nó e a página inteira se perde em
   silêncio. O conteúdo é envolvido em `<root>` com os namespaces declarados.
2. **Entidades HTML nomeadas destruindo acentos.** O parser não conhece
   `&ecirc;`, `&ccedil;`, `&nbsp;` e apaga a entidade *junto com a letra* —
   "você" vira "Voc". Numa base em português, isso corrompe conteúdo em massa. O
   unescape é feito antes do parse e é **seletivo**: as cinco entidades nativas
   do XML são preservadas, senão um exemplo de markup escapado viraria tag.
3. **Cabeçalho quebrando em duas linhas.** Marcador antes e depois da tag gera
   `"# \nTítulo"` e arruína o chunking por seção.
4. **Blocos de código em CDATA.** Um `unwrap()` genérico das macros descarta o
   CDATA inteiro e todo snippet da base desaparece. As macros de código são
   tratadas antes de qualquer unwrap — tanto `<ac:macro>` (4.2) quanto
   `<ac:structured-macro>` (4.3+, presente em bases migradas).
5. **Palavras colando quando a tag não é fechada.** `<p>texto solto<p>outro`
   vira `texto soltooutro`.
6. **Fallback de parser obrigatório.** Base de 2012 tem XML mal formado. A
   boa-formação é testada com `recover=False` **antes** de parsear, porque o
   lxml em modo recuperação não levanta exceção: ele conserta em silêncio e
   descarta pedaços.
7. **`allow_none=True` no `ServerProxy`.** O Confluence 4.x não aceita `<nil/>`
   e a chamada falha. Fica desligado.
8. **Emoji como par surrogate.** O Confluence serializa emoji como o par
   surrogate da UTF-16 — 📝 sai como `&#55357;&#56541;`. Surrogate não é
   caractere válido em XML 1.0, então o expat rejeita a **resposta inteira** e a
   página nunca chega ao parser. A faxina é feita na resposta bruta, antes do
   parse: o par é recombinado no code point de verdade.

Além disso, wiki markup residual pré-4.0 nunca migrado (`h2. Título`, `{code}`,
`{noformat}`) é convertido num passe próprio, reaproveitado para a `description`
e os comentários do Jira, que no Server 8.14 também vêm em wiki markup.

---

## Números desta instalação

| | |
|---|---|
| documentos no store | **65.756** — 40.733 do Confluence, 25.023 do Jira |
| fatias no índice | **290.686**, todas com vetor denso |
| espaços do Confluence no escopo | 716 (`CONFLUENCE_SPACES=auto`) |
| projetos do Jira no escopo | `VENDAS`, `ECOMMERCE` |
| máquina | 8 vCPU, 16 GB, sem GPU — embedding em CPU |
| latência aquecida | bm25 ~2 ms · denso ~16 ms · híbrido ~51 ms |
| `getPages` num espaço de 6.129 páginas | 70 a 181 s, numa única chamada |
| `getPage` | ~145 ms/página |
| `POST /search` do Jira | 1,5 ms/issue (100 por requisição) |

Consequências práticas dessas medidas:

- **o `CONFLUENCE_TIMEOUT` padrão é 300 s.** Um `getPages` de 181 s estourava o
  padrão anterior de 120 s. Não abaixe sem medir o maior espaço do escopo;
- **cada página custa 3 round-trips**, não 1: `getPage` + `getAttachments` +
  `getLabelsById`. `--no-count-attachments` e `--no-labels` cortam um terço cada;
- **espaço grande fica ~30 min sem emitir log** entre "listado" e "concluído".
  Por isso existe a linha `progresso do espaço` a cada 250 documentos, com
  percentual, ritmo e ETA.

---

## Rodar a sua própria instância

Requer Python **3.12+** e Docker.

```bash
git clone https://github.com/joaquim-dandolini-wmw/rag-confluence-jira.git
cd rag-confluence-jira

python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose up -d          # Qdrant em 127.0.0.1:6333
cp .env.example .env          # e preencha
```

Pré-requisito do lado do Confluence: a **Remote API (XML-RPC)** precisa estar
habilitada em *Administração → Configuração Geral → Remote API*. Ela vem
desligada por padrão em muitas instalações; sem ela `/rpc/xmlrpc` responde
404/403 e nada funciona.

As variáveis todas estão documentadas em `.env.example`. As que não têm padrão
seguro:

| variável | observação |
|---|---|
| `JIRA_URL` / `JIRA_PAT` | PAT via header Bearer; PATs existem a partir da 8.14 |
| `JIRA_PROJECTS` | **vazio aborta** — ver Segurança |
| `CONFLUENCE_URL` | |
| `CONFLUENCE_USER` / `CONFLUENCE_PASSWORD` | a 4.2.4 **não** suporta PAT |
| `CONFLUENCE_SPACES` | **vazio aborta**; `auto` indexa tudo que o usuário vê |

O runtime é **offline por padrão e nunca baixa modelo sozinho**. Se um artefato
faltar, ele aborta dizendo exatamente o que falta e como resolver. Para
pré-cachear numa máquina com rede:

```bash
ALLOW_MODEL_DOWNLOAD=1 python -m scripts.precache_models
```

### Operação

```bash
python -m indexer.sync extract --only confluence   # extrai para o store
python -m indexer.sync index                       # store -> Qdrant (BM25)
python -m indexer.sync embed                       # vetor denso
python -m indexer.sync run                         # extract + index + embed
python -m indexer.sync reconcile --only jira       # remove issues apagadas
python -m indexer.sync status                      # estado do store e do índice
python -m indexer.sync search "cobrança bancária"  # busca pela linha de comando
```

O log é JSON por linha, em **stderr**. Cada rodada fecha com uma linha de
métricas: páginas listadas, buscadas, puladas por versão inalterada, falhas,
tempo total e média por `getPage`.

O servidor MCP sobe com:

```bash
python -m mcp_server.server        # transporte conforme MCP_TRANSPORT
```

`MCP_TRANSPORT=streamable-http` publica uma URL — é o modo em produção, e o
único que os clientes deste README usam. O TLS não é feito aqui: quem termina o
certificado é o proxy reverso na frente, e o serviço fala HTTP na 8765.
`stdio`, um processo por cliente, continua suportado pelo código para quem
precisar rodar o servidor local junto do cliente.

Em produção são dois serviços systemd, `rag-mcp` e `rag-painel`, com os unit
files em `deploy/`. Nada de ambiente vai no unit: transporte, porta e bind saem
todos do `.env` lido pelo `config.py`, para haver **um lugar só** de configurar.

Há ainda um **painel web de operação** (`panel/`) na porta 8770: mostra as
mudanças da última rodada, os logs, os acessos e a agenda. Ele **escreve no
crontab** do usuário ao salvar o agendamento, e por isso o padrão é escutar só
em `127.0.0.1`; para atender a rede é obrigatório definir `PANEL_PASSWORD`, sem
a qual ele se recusa a subir.

### A janela de meia-noite

Uma rodada por dia, à meia-noite, até acabar — `deploy/crontab.example` traz o
bloco pronto, que é o mesmo que o painel escreve:

```bash
crontab deploy/crontab.example
sudo install -m 644 -o root -g root deploy/logrotate.rag /etc/logrotate.d/rag
```

O `flock -n` garante uma rodada por vez: se uma noite atrasar e passar da
meia-noite seguinte, a nova desiste em vez de duas disputarem o mesmo SQLite.

### Subir em máquina nova

O store e o índice **viajam** — reconstruir do zero é de 9 a 22 h numa máquina
sem GPU.

O SQLite usa WAL, então um `cp` no meio de uma escrita leva metade de uma
transação. Use o `.backup`, que é consistente com o banco em uso:

```bash
sqlite3 data/documents.sqlite3 ".backup /tmp/store.sqlite3"   # na ANTIGA
scp ANTIGA:/tmp/store.sqlite3 data/documents.sqlite3          # na NOVA
```

O índice vai por snapshot do Qdrant. A coleção **não precisa existir** do outro
lado: o upload a recria.

```bash
# na ANTIGA: cria e baixa
NOME=$(curl -s -X POST http://127.0.0.1:6333/collections/atlassian_kb/snapshots        | python -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')
curl -s -o /tmp/$NOME "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/$NOME"
scp /tmp/$NOME NOVA:/tmp/

# na NOVA, com o Qdrant de pé
curl -s -X POST   "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/upload?priority=snapshot"   -H 'Content-Type: multipart/form-data' -F "snapshot=@/tmp/$NOME"
```

`priority=snapshot` diz ao Qdrant que o arquivo vence o que estiver na coleção.
Apague o snapshot da origem depois: ele ocupa quase o tamanho da coleção.

**A armadilha, se você trouxer o store e NÃO o índice.** O índice é derivável do
store, mas os dois `--*-all` são obrigatórios:

```bash
python -m indexer.sync index --reindex-all
python -m indexer.sync embed --reembed-all
```

Sem eles, o store copiado já tem `indexed_hash` e `embedded_hash` preenchidos, o
incremental conclui "nada pendente", e você fica com `pendentes idx: 0` num
índice **vazio**: o `status` mente e a busca não acha nada.

Numa carga do zero, `indexer.sync run` faz tudo na ordem certa — extrai,
indexa e só então popula o vetor denso, porque o `embed` atualiza ponto que já
existe. Faça acompanhando, na mão, não pelo cron: são horas, e é a única rodada
em que tudo é novo. Queda no meio não perde trabalho.

### Testes

```bash
.venv/bin/python -m pytest tests/ -q
```

**248 testes**, nenhum depende de instância real. Cobrem o parser (HTML mal
formado, entidades acentuadas, wiki markup residual, CDATA nas duas formas de
macro), o chunking (IDs estáveis entre execuções, fronteira de cabeçalho), o
transporte (os oito pares surrogate que apareceram nas páginas reais), o store
(dois escritores concorrentes no mesmo SQLite) e o painel.

---

## Estrutura

```
config.py                        env, validação de escopo, logging JSON em stderr
connectors/confluence_legacy.py  parser de storage format + cliente XML-RPC
connectors/jira_server.py        REST v2, PAT, JQL incremental, comentários
store/documents.py               SQLite: documents, confluence_versions, sync_state
indexer/chunking.py              fatiamento com título prefixado e IDs uuid5
indexer/index.py                 Qdrant: schema, BM25 + denso, roteamento da consulta
indexer/sync.py                  CLI extract / index / embed / run / reconcile / status
mcp_server/server.py             as quatro ferramentas MCP
panel/                           painel web de operação
scripts/precache_models.py       pré-cache dos modelos para operação offline
deploy/                          unit files do systemd, crontab e logrotate
tests/                           parser, chunking, transporte, escopo, embeddings, painel
```

Este README é a documentação do projeto. O `.env.example` documenta cada
variável no lugar onde ela é definida, com o porquê de cada padrão.
