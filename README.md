<h1 align="center">wmw-mcp-interno</h1>

<p align="center">
  Pergunte em português para o Jira e o Confluence da WMW,<br>
  de dentro do seu chat de IA.
</p>

<p align="center">
  <code>https://wmw-rag.wmw.com.br/</code>
</p>

<p align="center">
  <sub>busca híbrida · login pela conta da rede · somente leitura · 65.756 documentos</sub>
</p>

---

A busca nativa do Jira 8.14 e do Confluence 4.2.4 só acha a **palavra exata**.
Se a página diz *"certificado vencido"* e você procura *"certificado expirado"*,
o resultado é zero. O conhecimento está lá; não é encontrável.

Este projeto indexa as duas instâncias **por significado** e publica tudo como
um servidor MCP — o protocolo que Claude, ChatGPT, Codex, Cursor e Gemini usam
para falar com ferramentas externas. Na prática, você pergunta no seu chat de
sempre:

> *Por que o boleto não é gerado em alguns pedidos? Cite os links.*

e ele procura nas duas instâncias, lê as páginas e as issues, e responde com a
URL de origem de cada afirmação.

Nada sai da rede: índice e modelos rodam em máquina nossa. O acesso é **somente
leitura** e **exige login** — a sua conta da rede, conferida no LDAP na hora.

---

## Conectar

Uma URL, em qualquer cliente:

```
https://wmw-rag.wmw.com.br/
```

A barra final faz parte: o endpoint está na **raiz**, não em `/mcp`. Deixe a
autenticação no automático — todo cliente moderno descobre o OAuth sozinho.

**Claude Code**

```bash
claude mcp add --transport http --scope user wmw-mcp-interno https://wmw-rag.wmw.com.br/
```

**Claude Desktop**

**+** ao lado da caixa de mensagem → **Connectors** → **Add custom connector** →
cole a URL → **Add**.

**ChatGPT** (plano pago)

**Settings** → **Apps & Connectors** → **Advanced settings** → ligue
**Developer mode**. Volte e clique em **Create**: nome `wmw-mcp-interno`, a URL
acima, autenticação **OAuth**.

<details>
<summary><b>Outros clientes</b> — claude.ai, Codex, Cursor, VS Code, Gemini CLI, Windsurf</summary>

<br>

Todos suportam MCP remoto nativamente. Não é preciso Node.js nem
`npx mcp-remote` — guia que mande fazer isso está desatualizado.

**claude.ai** (navegador e celular) — **Settings** → **Connectors** → **Add
custom connector**. Aqui quem conecta são os servidores da Anthropic, não o seu
computador, então funciona também fora da rede da empresa.

**Codex CLI**

```bash
codex mcp add wmw-mcp-interno --url https://wmw-rag.wmw.com.br/
```

**Cursor** — `~/.cursor/mcp.json` (ou `.cursor/mcp.json` no projeto):

```json
{ "mcpServers": { "wmw-mcp-interno": { "url": "https://wmw-rag.wmw.com.br/" } } }
```

**VS Code (GitHub Copilot)**

```bash
code --add-mcp '{"name":"wmw-mcp-interno","type":"http","url":"https://wmw-rag.wmw.com.br/"}'
```

**Gemini CLI**

```bash
gemini mcp add --transport http wmw-mcp-interno https://wmw-rag.wmw.com.br/
```

**Windsurf** — `~/.codeium/windsurf/mcp_config.json`:

```json
{ "mcpServers": { "wmw-mcp-interno": { "serverUrl": "https://wmw-rag.wmw.com.br/" } } }
```

**Qualquer outro** — o que muda de um para outro é só o nome da chave que
guarda a URL:

| cliente | arquivo | chave |
|---|---|---|
| Codex CLI | `~/.codex/config.toml` | `url` |
| Cursor | `~/.cursor/mcp.json` | `url` |
| VS Code Copilot | `.vscode/mcp.json` | `url` + `"type": "http"` |
| Gemini CLI | `~/.gemini/settings.json` | `httpUrl` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `serverUrl` |

Se pedirem um tipo de transporte, escolha **`http`** / Streamable HTTP — nunca
`sse`, que é o antigo e este servidor não atende.

</details>

### O login

Na primeira conexão o cliente **abre o seu navegador** numa tela da WMW. Você
entra com o usuário e a senha da rede e pronto — daí em diante ele renova
sozinho.

Repare no que *não* acontece: **a sua senha nunca é digitada dentro do Claude
ou do ChatGPT**. Eles abrem o navegador na nossa página e recebem de volta só
um token. É por isso que o mesmo desenho funciona em todos sem precisar
confiar em nenhum.

A tela diz **qual aplicativo** pediu acesso. Se aparecer um nome que você não
abriu, não entre.

Ter conta na rede não basta: é preciso estar no grupo do diretório.

---

## Como perguntar

Descreva o problema **com as suas palavras**. Você não precisa adivinhar o
vocabulário de quem escreveu a página — *"dados da fatura para pagamento em
banco"* encontra a página que fala em *"boleto bancário"*.

A busca escolhe sozinha entre exata e semântica:

| você escreve | o que acontece |
|---|---|
| `VENDAS-14993`, `PRUPSYNCPRODUTOS` | busca exata pelo termo |
| "por que o boleto não é gerado" | busca por significado |
| "erro no PRUPSYNCPRODUTOS ao sincronizar" | as duas, fundidas |

Contagem e listagem — *"quantos bugs abertos em VENDAS"* — não passam pelo
índice; o cliente vai sozinho na consulta ao vivo do Jira.

<details>
<summary><b>As quatro ferramentas</b></summary>

<br>

| ferramenta | fonte | para quê |
|---|---|---|
| `search_knowledge_base` | índice | encontrar conteúdo nas duas fontes, por assunto ou significado |
| `search_jira_jql` | Jira ao vivo | contagem, filtro por status, ordenação, conjunto exato |
| `get_jira_issue` | Jira ao vivo | a issue inteira, estado atual, thread completa |
| `get_confluence_page` | Confluence ao vivo | a página inteira, quando a fatia não basta |

Cada docstring diz explicitamente **quando usar e quando NÃO usar** a
ferramenta — é assim que o modelo decide sozinho para onde ir. Todo resultado
traz a URL de origem, sempre.

</details>

---

## Quando não funcionar

| sintoma | o que fazer |
|---|---|
| não conecta de jeito nenhum | você precisa alcançar `wmw-rag.wmw.com.br`. No Wi-Fi isso oscila |
| o navegador não abre sozinho | o login precisa de navegador na mesma máquina; em SSH sem X11 não funciona |
| "usuário ou senha inválidos" com a senha certa | provavelmente você não está no grupo. A mensagem é a mesma de propósito — ver Segurança |
| "Muitas tentativas, tente em N minutos" | o freio travou. Espere o tempo indicado |
| `401` depois de funcionar um tempo | token expirado. Reconecte o servidor no cliente |
| "Pedido de login inválido ou expirado" | a tela ficou aberta tempo demais. Mande conectar de novo |
| `404`, ou pediram `npx mcp-remote` | endereço ou guia errado: é a **raiz**, com a barra final, e sem ponte nenhuma |

<details>
<summary>Testar o servidor sem cliente nenhum</summary>

<br>

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://wmw-rag.wmw.com.br/
```

**`400` é a resposta certa** — significa que chegou lá, e o `curl` não fala MCP.
Se travar ou devolver `000`, é rede.

Para ver o servidor exigindo login:

```bash
curl -si -X POST https://wmw-rag.wmw.com.br/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | head -n 12
```

Tem que vir `401` com um cabeçalho `WWW-Authenticate` apontando os metadados do
OAuth. É esse `401` que faz o cliente abrir a tela de login.

</details>

---

## Segurança

**O indexador achata o controle de acesso.** Ele entra com um usuário de
serviço, então tudo que *ele* enxerga entra no índice — e as permissões por
espaço do Confluence **não são aplicadas na busca**.

Nesta instalação: o escopo do Confluence está em `auto`, com os **716 espaços**
indexados, os 651 de cliente inclusive. O endereço resolve na internet, e desde
**14/09/2026** exige login — quem chega sem token recebe `401`.

**O que o login resolve, e o que não resolve.** Ele fecha a porta para quem não
é da empresa e registra quem entrou. Ele **não** aplica as permissões por
espaço: quem está no grupo continua enxergando tudo que foi indexado. Fechar
isso pede filtrar a busca por espaço, usando o `space_key` que já está no
payload de cada fatia — é trabalho de outra fase, e ela deveria existir.

O código também garante, independentemente disso: `JIRA_PROJECTS` ou
`CONFLUENCE_SPACES` vazios **abortam a execução** (não existe "indexar tudo"
por acidente — `auto` tem que ser escrito de propósito); o Qdrant faz bind em
`127.0.0.1`; e o sistema nunca escreve no Jira nem no Confluence.

<details>
<summary><b>Como a autenticação foi feita</b></summary>

<br>

**OAuth 2.1**, o padrão do próprio MCP, com registro dinâmico ligado — é o que
faz cada cliente conectar sem ninguém digitar `client_id`. Registrar não dá
acesso a nada: quem decide é o diretório na tela seguinte.

**A identidade é o LDAP, e só ele.** Não existe tabela de usuários, não existe
cadastro, e nenhuma senha é guardada aqui, nem em hash. O bind é feito com a
credencial da própria pessoa, o que também dispensa conta de serviço — nenhuma
credencial do diretório fica no `.env`.

**O freio de tentativas protege o diretório, não este servidor.** A tela
responde da internet e faz bind no LDAP corporativo; sem freio, um script de
fora trancaria contas de gente que nem sabe que isto existe. Passado o limite
por usuário, a tentativa **não é encaminhada ao LDAP**.

**Token opaco, guardado como hash**, num SQLite separado do document store.
Revogar é um `UPDATE`, e um vazamento do arquivo não entrega credencial
utilizável.

**Erro de login é um só** para senha errada, usuário inexistente e usuário fora
do grupo. Distinguir os casos transformaria a tela num jeito de descobrir quem
trabalha na empresa.

Quem entrou aparece no painel de operação, na aba **Logins**.

</details>

---

## Como funciona

Dois ciclos independentes, que se comunicam **apenas pelo índice**.

```
CICLO 1 — indexação (offline, janela noturna)

   Jira 8.14                Confluence 4.2.4
   REST v2 + PAT            XML-RPC /rpc/xmlrpc
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

**O document store é a peça central, não um cache.** A extração é a parte cara
e frágil: o XML-RPC do Confluence é lento, não pagina e não filtra por data.
Gravando o texto limpo num SQLite local, reconstruir o índice do zero vira
operação de minutos que **não toca em nenhuma instância Atlassian**.

**Índice e ao vivo são separados de propósito.** O índice responde conteúdo e
significado, com o atraso da última janela noturna. Contagem e ordenação
**nunca** saem dele: essas respostas precisam estar corretas agora, não
aproximadamente.

<details>
<summary><b>A busca em detalhe</b> — por que a fusão RRF não bastou</summary>

<br>

O plano original previa fusão RRF como resposta única. Ela está implementada e
ponderada, mas **medida, não atende os dois critérios ao mesmo tempo**: o RRF
soma `1/(k+rank)`, então todo candidato que o BM25 traz ocupa posição boa mesmo
sendo irrelevante para uma pergunta em prosa — e, na direção oposta, um
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

Medido na instância real, com consulta em **sinônimo puro** — nenhuma palavra
de conteúdo em comum com o alvo. O número é a posição do documento certo:

| consulta | alvo | bm25 | denso | híbrido | **auto** |
|---|---|---|---|---|---|
| "dados da fatura para pagamento em banco" | *Informações do Boleto Bancário* | não acha | 8 | 55 | **8** |
| "administração de celulares dos vendedores" | *Gerenciamento de Smartphones* | não acha | 13 | 64 | **13** |

E o custo, com identificador exato (alvo em 1º lugar, 3 execuções):

| consulta | bm25 | denso | híbrido | **auto** |
|---|---|---|---|---|
| `VENDAS-14993` | 3/3 | 0/3 | 3/3 | **3/3** |
| `PRUPSYNCPRODUTOS` | 3/3 | 0/3 | **0/3** | **3/3** |

**O reranker.** O bi-encoder vetoriza consulta e documento separadamente, então
o vetor do documento tem que servir para toda consulta possível — por isso ele
acha o documento por sinônimo mas não o coloca no topo. Um cross-encoder
(`BAAI/bge-reranker-v2-m3`) lê os dois juntos num forward só:

| consulta em sinônimo puro | sem reranker | com reranker |
|---|---|---|
| "dados da fatura para pagamento em banco" | 8º | **3º** |
| "administração de celulares dos vendedores" | fora da página | **5º** |

Duas decisões que só apareceram medindo: **consulta com identificador não passa
pelo reranker** (`VENDAS-14993` quase não tem semântica — com ele o alvo caía de
1º para 2º); e **mais candidatos piora** — de 20 para 80, o alvo caiu da posição
3 para a 5.

Está **desligado nesta instalação** (`RERANK_ENABLED=0`): na máquina com GPU
custava 475 ms por consulta em prosa contra 19 ms sem ele, e aqui, em CPU, não
compensa. Falha dele degrada para a ordem da primeira etapa, com aviso no log.

</details>

<details>
<summary><b>As armadilhas do Confluence 4.2.4</b> — oito bugs reais, todos cobertos por teste</summary>

<br>

Nenhuma ferramenta de mercado fala com o Confluence 4.2.4, então a camada de
extração foi construída à mão.

1. **Fragmento sem elemento raiz.** O storage format são vários irmãos sem raiz
   única; o parser XML para no primeiro nó e a página inteira se perde em
   silêncio. O conteúdo é envolvido em `<root>` com os namespaces declarados.
2. **Entidades HTML nomeadas destruindo acentos.** O parser não conhece
   `&ecirc;`, `&ccedil;`, `&nbsp;` e apaga a entidade *junto com a letra* —
   "você" vira "Voc". Numa base em português isso corrompe conteúdo em massa. O
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

</details>

<details>
<summary><b>Números desta instalação</b></summary>

<br>

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

</details>

---

## Instalar e operar

<details>
<summary><b>Subir a sua própria instância</b></summary>

<br>

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

As variáveis estão todas documentadas em `.env.example`, cada uma com o porquê
do padrão. As que não têm padrão seguro:

| variável | observação |
|---|---|
| `JIRA_URL` / `JIRA_PAT` | PAT via header Bearer; PATs existem a partir da 8.14 |
| `JIRA_PROJECTS` | **vazio aborta** |
| `CONFLUENCE_USER` / `CONFLUENCE_PASSWORD` | a 4.2.4 **não** suporta PAT |
| `CONFLUENCE_SPACES` | **vazio aborta**; `auto` indexa tudo que o usuário vê |
| `MCP_AUTH_ENABLED` | `1` fecha o servidor. Exige `MCP_PUBLIC_URL` e as `LDAP_*` |

O runtime é **offline por padrão e nunca baixa modelo sozinho**. Se um artefato
faltar, ele aborta dizendo o que falta e como resolver. Para pré-cachear numa
máquina com rede: `ALLOW_MODEL_DOWNLOAD=1 python -m scripts.precache_models`.

</details>

<details>
<summary><b>Comandos do dia a dia</b></summary>

<br>

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

Em produção são dois serviços systemd, `rag-mcp` e `rag-painel`, com os unit
files em `deploy/`. Nada de ambiente vai no unit: transporte, porta e bind saem
todos do `.env` lido pelo `config.py`, para haver **um lugar só** de configurar.

O **painel de operação** (`panel/`) fica na porta 8770: mudanças da última
rodada, logs, acessos, agenda e quem fez login. Ele **escreve no crontab** ao
salvar o agendamento, e por isso o padrão é escutar só em `127.0.0.1`; para
atender a rede é obrigatório definir `PANEL_PASSWORD`, sem a qual ele se recusa
a subir.

**A janela de meia-noite** — uma rodada por dia, até acabar:

```bash
crontab deploy/crontab.example
sudo install -m 644 -o root -g root deploy/logrotate.rag /etc/logrotate.d/rag
```

O `flock -n` garante uma rodada por vez: se uma noite atrasar e passar da
meia-noite seguinte, a nova desiste em vez de duas disputarem o mesmo SQLite.

</details>

<details>
<summary><b>Migrar para outra máquina</b></summary>

<br>

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
NOME=$(curl -s -X POST http://127.0.0.1:6333/collections/atlassian_kb/snapshots \
       | python -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')
curl -s -o /tmp/$NOME "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/$NOME"
scp /tmp/$NOME NOVA:/tmp/

# na NOVA, com o Qdrant de pé
curl -s -X POST \
  "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/upload?priority=snapshot" \
  -H 'Content-Type: multipart/form-data' -F "snapshot=@/tmp/$NOME"
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

Numa carga do zero, `indexer.sync run` faz tudo na ordem certa — extrai, indexa
e só então popula o vetor denso, porque o `embed` atualiza ponto que já existe.
Faça acompanhando, na mão, não pelo cron: são horas, e é a única rodada em que
tudo é novo. Queda no meio não perde trabalho.

</details>

---

## Testes

```bash
.venv/bin/python -m pytest tests/ -q
```

**277 testes**, nenhum depende de instância real nem de diretório real. Cobrem
o parser (HTML mal formado, entidades acentuadas, CDATA nas duas formas de
macro), o chunking (IDs estáveis, fronteira de cabeçalho), o transporte (os
oito pares surrogate que apareceram nas páginas reais), o store (dois
escritores concorrentes no mesmo SQLite), a autenticação (injeção no DN, senha
vazia, o freio, uso único do código, revogação) e o painel.

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
auth/                            OAuth 2.1, bind no LDAP, tela de login, freio
panel/                           painel web de operação
deploy/                          unit files do systemd, crontab e logrotate
tests/                           parser, chunking, transporte, escopo, auth, painel
```

Este README é a documentação do projeto. O `.env.example` documenta cada
variável no lugar onde ela é definida, com o porquê de cada padrão.
