# INICIALIZAÇÃO — subir o projeto do zero em máquina nova

Documento único: da máquina vazia até o sistema atualizando sozinho toda
meia-noite, com o índice trazido pronto de outra máquina. Não há segundo
documento de instalação — o que existe além deste é o `SETUP.md`, que guarda o
*por quê* de cada escolha e as medições, e o `ACESSO-MCP.md`, que é o passo a
passo do cliente para quem vai só consultar.

Todos os números vêm de medição na instância real (Jira 8.14 e Confluence 4.2.4
da empresa) e na máquina de referência, em set/2026.

---

## 1. Recomendação de máquina

### Onde o tempo vai

O dimensionamento muda completamente conforme você **traz o índice pronto** ou
**carrega do zero**. Os dois cenários, medidos:

| | trazendo o índice pronto | carregando do zero |
|---|---|---|
| Confluence | 1h40 por noite (escopo de 716 espaços) | **4h32** de uma vez |
| Jira | segundos | ~18 min |
| index (chunking) | segundos a minutos | minutos |
| vetor denso | só o que mudou | **~36 min** na GPU, **~9 a 22 h** na CPU |

Na carga do zero, **mais de 80% do tempo é espera de rede**: o Confluence 4.2.4
não tem REST, e o XML-RPC cobra três chamadas por página — `getPage`,
`getLabelsById`, `getAttachments` — a 140 ms cada, uma por vez, sem paginação
nem filtro por data. Nenhum hardware encurta isso.

Trazendo o índice pronto, o trabalho pesado já foi feito **aqui**. É o que torna
uma máquina sem GPU viável.

### Sem GPU: o que funciona e o que não

Medido na CPU desta máquina (Ryzen 5 7600, 6 núcleos):

| operação | CPU | GPU | veredito |
|---|---|---|---|
| embed da **consulta** (toda busca paga) | **69 ms** | ~15 ms | **sem problema** |
| rerank de 30 candidatos | **3,35 s** | 0,475 s | **pesado**, veja abaixo |
| embed de fatias (indexação) | 3,7 a 9,2 fatias/s | 89 a 135 fatias/s | ok para a noite incremental |
| carga do zero (289 mil fatias) | 9 a 22 h | 36 min | **não cabe numa noite** |

Duas conclusões práticas:

1. **A busca funciona bem na CPU.** O que toda consulta paga é o embed da
   pergunta: 69 ms. O índice já traz os vetores dos documentos calculados.
2. **O reranker é o problema.** São 30 passagens de cross-encoder por consulta,
   112 ms cada na CPU. Três segundos e meio de espera para quem pergunta. Sem
   GPU, escolha uma:
   - `RERANK_ENABLED=0` — perde a segunda etapa. Medido: o alvo de sinônimo cai
     da posição 3 para 8, e o de "fora da página" sai do top 5;
   - `RERANK_CANDIDATES=10` — cerca de **1,1 s**, com boa parte do ganho. É o
     meio-termo recomendado para máquina sem GPU.

E a noite incremental cabe folgada: umas dezenas de documentos mudam por dia,
o que dá poucas centenas de fatias — menos de um minuto de CPU.

### O que pedir

| item | só CPU (índice trazido) | com GPU (carga do zero) |
|---|---|---|
| rede | **1 Gbit com fio, mesma LAN do Jira e do Confluence** | idem — é o que decide a janela |
| CPU | 6 a 8 núcleos (os modelos rodam aqui) | 4 a 6 núcleos |
| RAM | **32 GB** — e5 e reranker ficam residentes em fp32, ~2,2 GB cada, além do Qdrant (~3 GB neste tamanho) | 16 a 32 GB |
| disco | 120 GB SSD, 500 GB NVMe folgado | idem |
| GPU | nenhuma | 8 GB VRAM já sobra: pico medido **1,87 GiB** |

Sobre a GPU, para não superdimensionar: mesmo na carga do zero ela responde por
36 minutos de um trabalho de 5h30, e o pico de VRAM é de 1,87 GiB. **Placa maior
não encurta a janela.** Se a placa for NVIDIA, muda só o wheel do PyTorch
(`cu12` em vez de `rocm7.2`); o código usa a API `cuda` nos dois casos.

Máquina de referência, para comparar: Ryzen 5 7600 (6 núcleos), 30 GB de RAM,
NVMe de 477 GB, Radeon RX 6900 XT de 16 GB.

---

## 2. O que viaja para a máquina nova

| o que | tamanho | como vai | está no git? |
|---|---|---|---|
| o repositório | — | `git clone` | sim |
| `.env` | 8 KB | `scp`, e três ajustes (§4) | **não** (`.gitignore`) |
| `data/documents.sqlite3` | 99 MB | `.backup` do sqlite3 (§5.1) | **não** |
| `models/` (e5 2,2 G + reranker 2,2 G + fastembed 164 K) | 4,3 GB | `rsync`, ou pré-cache com internet | **não** |
| índice Qdrant | 887 MB | snapshot (§5.2) | **não** |
| `.venv/` | — | **não copie**, recrie | não |
| `logs/`, `certs/` | — | não vão | não |

A hierarquia entre os dois bancos manda em tudo o que vem depois: o **SQLite é a
fonte da verdade** e o **Qdrant é derivado dele**. Dá para chegar do outro lado
sem o Qdrant e reconstruí-lo — mas numa máquina sem GPU isso custa de 9 a 22 h,
e é justamente o que trazer o snapshot evita. O contrário não existe: sem o
SQLite, reconstruir significa reextrair Jira e Confluence inteiros.

O `.env` carrega `JIRA_PAT` e `CONFLUENCE_PASSWORD` em texto puro. Passe por
`scp`, nunca por e-mail, chat ou anexo.

---

## 3. Instalação

```bash
sudo pacman -S --needed docker docker-compose git cronie sqlite flock
sudo systemctl enable --now docker cronie
sudo usermod -aG docker $USER        # relogue depois disto
```

Fora do Arch, troque o gerenciador: precisam existir Docker, cron, git, o CLI do
sqlite, o `flock` e o `uv`.

```bash
git clone https://github.com/joaquim-dandolini-wmw/rag-confluence-jira.git
cd rag-confluence-jira

uv python install 3.12
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -r requirements-dev.txt

# PyTorch. SEM GPU basta o build de CPU, que é bem menor:
VIRTUAL_ENV=.venv uv pip install \
    --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0"
VIRTUAL_ENV=.venv uv pip install "sentence-transformers>=3,<6"
# COM GPU AMD, o wheel é outro e a verificação importa: SETUP.md §4.
```

O caminho `/home/joaquimdp/Documentos/rag` e o nome do usuário estão escritos
por extenso em `deploy/rag-mcp.service`, `deploy/rag-painel.service`,
`deploy/crontab.example` e `deploy/mcp-stdio.sh`. Se for diferente, edite os
quatro — serviço e cron falham em silêncio.

### Modelos

O runtime é **offline por padrão** (`ALLOW_MODEL_DOWNLOAD=0`): se um artefato
faltar, ele aborta dizendo qual, em vez de baixar 4,3 GB no meio de uma rodada.

```bash
# a) copiar da máquina antiga — o caminho normal numa migração
rsync -a --info=progress2 joaquimdp@10.2.1.132:~/Documentos/rag/models/ models/

# b) baixar, se esta máquina tem internet
ALLOW_MODEL_DOWNLOAD=1 .venv/bin/python -m scripts.precache_models
```

### Qdrant

```bash
docker compose up -d
curl -s http://127.0.0.1:6333/ | head -1
```

Use o `docker-compose.yml` **do repositório**, que fixa `qdrant/qdrant:v1.19.1`
e faz bind só em `127.0.0.1` — o índice tem conteúdo interno das duas instâncias
Atlassian e não deve ficar exposto. Não escreva outro. O Qdrant não usa GPU:
só CPU e disco.

---

## 4. Configuração

```bash
scp joaquimdp@10.2.1.132:~/Documentos/rag/.env .env    # ou cp .env.example .env
```

Três coisas mudam ao trocar de máquina:

1. **`MCP_ALLOWED_HOSTS`** traz os IPs da máquina antiga. O SDK valida o
   cabeçalho `Host` e **recusa** o que não estiver na lista, então com o IP novo
   ausente todo cliente para de conectar. Deixe **vazio**: o servidor descobre o
   próprio IP no boot;
2. **sem GPU**, ajuste os três:
   ```
   EMBED_DEVICE=cpu
   RERANK_CANDIDATES=10      # 3,35 s -> ~1,1 s por consulta
   #RERANK_ENABLED=0         # ou desligue de vez, aceitando perda de precisão
   ```
3. **`CONFLUENCE_SPACES`** — se estiver `auto`, o escopo é o que o usuário de
   serviço enxerga, e isso muda sem você tocar em arquivo.

**Confira o escopo antes de qualquer extração.** Só analisa, não escreve:

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync scope
```

É a diferença entre uma janela de 26 min e uma de 1h40: medido nesta instância,
um usuário de serviço enxerga **30 espaços** e outro enxerga **716**. Com
`auto`, quem controla isso é a administração de **grupos do Confluence** —
adicionar um grupo traz os espaços dele na próxima rodada, e remover **purga** o
conteúdo daquele espaço do store e do índice. Não coloque espaço restrito (RH,
jurídico, financeiro) no escopo: o MCP não tem autenticação e não reproduz as
permissões por espaço do Confluence.

---

## 5. Levar o store e o índice

Antes de copiar, **pare o que escreve** na máquina de origem: `crontab -r`, ou
espere uma rodada terminar. Cópia tirada no meio de uma indexação sai
consistente, mas sai *velha*.

### 5.1 O store

O store usa WAL: um `cp` no meio de uma escrita leva metade de uma transação. Use
o `.backup`, que é consistente **com o banco em uso**:

```bash
# na máquina ANTIGA
sqlite3 data/documents.sqlite3 ".backup /tmp/store-migracao.sqlite3"
# na máquina NOVA
scp joaquimdp@10.2.1.132:/tmp/store-migracao.sqlite3 data/documents.sqlite3
```

### 5.2 O índice — snapshot da coleção

Este caminho foi **testado de ponta a ponta** nesta versão do Qdrant, em coleção
descartável com o mesmo esquema da real: criar, baixar, apagar a coleção e
restaurar devolveu os pontos, o esquema denso e o esparso, o `on_disk_payload` e
os payloads intactos. A coleção **não precisa existir** do outro lado: o upload
a recria.

```bash
# 1. na máquina ANTIGA: cria o snapshot e guarda o nome
NOME=$(curl -s -X POST http://127.0.0.1:6333/collections/atlassian_kb/snapshots \
       | python -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')
echo $NOME    # ex.: atlassian_kb-7436581249880683-2026-09-10-12-29-02.snapshot

# 2. baixa
curl -s -o /tmp/$NOME \
     "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/$NOME"

# 3. leva
scp /tmp/$NOME joaquimdp@IP-NOVO:/tmp/

# 4. na máquina NOVA, com o Qdrant de pé
curl -s -X POST \
  "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/upload?priority=snapshot" \
  -H 'Content-Type: multipart/form-data' -F "snapshot=@/tmp/$NOME"

# 5. confere
curl -s http://127.0.0.1:6333/collections/atlassian_kb \
  | python -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r["points_count"], r["status"])'
```

`priority=snapshot` diz ao Qdrant que o arquivo vence o que estiver na coleção —
é o que se quer numa restauração. Apague o snapshot da origem depois: ele ocupa
quase o tamanho da coleção.

**Alternativa, cópia byte a byte de tudo** (todas as coleções, aliases,
`raft_state.json`), com o container **parado** — o Qdrant tem WAL e faz flush a
cada 5 s, e um tar quente pega segmento pela metade:

```bash
# ANTIGA
docker compose down
docker run --rm -v rag_qdrant_storage:/from -v /tmp:/backup \
       qdrant/qdrant:v1.19.1 tar czf /backup/qdrant-storage.tgz -C /from .
docker compose up -d
# NOVA
docker compose up -d && docker compose down      # cria o volume vazio
docker run --rm -v rag_qdrant_storage:/to -v /tmp:/backup \
       qdrant/qdrant:v1.19.1 tar xzf /backup/qdrant-storage.tgz -C /to
docker compose up -d
```

### 5.3 Se você trouxe o store e NÃO o índice

O índice é derivável do store, mas os dois `--*-all` são **obrigatórios** — e é
aqui que se erra:

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync index --reindex-all
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync embed --reembed-all
```

Sem eles, o store copiado já tem `indexed_hash` e `embedded_hash` preenchidos, o
incremental conclui "nada pendente" e você fica com `pendentes idx: 0` num
índice **vazio**: o `status` mente e a busca não acha nada. Numa máquina sem
GPU, isto é de 9 a 22 h de trabalho — motivo de sobra para trazer o snapshot.

### 5.4 Se for carga do zero, sem trazer nada

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync run > logs/carga-inicial.log 2>&1
```

Um comando faz tudo, nesta ordem: extrai o Confluence, extrai o Jira, indexa e
por último popula o vetor denso — **o denso vem depois de propósito**, porque o
embed atualiza vetor de ponto que já existe. Faça **acompanhando, na mão**, não
pelo cron: são horas, e é a única rodada em que tudo é novo. Queda no meio não
perde trabalho (o store commita a cada 25 documentos e o cursor do Jira é salvo
no `finally`); rodar de novo continua de onde parou. `--no-count-attachments`
corta um terço dos round-trips, de 4h32 para 3h10.

---

## 6. A janela de meia-noite

Uma rodada por dia, à meia-noite, até acabar. Pelo painel (§7) isso é um campo
de horário; à mão, é o `deploy/crontab.example`:

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
RAG=/home/joaquimdp/Documentos/rag

# Segunda a sábado: extract + index + embed das duas fontes.
0 0 * * 1-6 cd $RAG && flock -n /tmp/rag-sync.lock env PYTHONPATH=$RAG .venv/bin/python -m indexer.sync run --no-count-attachments >> logs/noturno.log 2>&1

# Domingo: a mesma rodada e, DEPOIS dela, a reconciliação de deleções do Jira.
0 0 * * 0 cd $RAG && flock -n /tmp/rag-sync.lock bash -c 'env PYTHONPATH=$RAG .venv/bin/python -m indexer.sync run --no-count-attachments && env PYTHONPATH=$RAG .venv/bin/python -m indexer.sync reconcile --only jira' >> logs/noturno.log 2>&1
```

```bash
crontab deploy/crontab.example
sudo install -m 644 -o root -g root deploy/logrotate.rag /etc/logrotate.d/rag
```

Por que está escrito assim:

- **`flock -n`** garante uma rodada por vez. Se uma noite atrasar e passar da
  meia-noite seguinte, a nova desiste em vez de duas disputarem o mesmo SQLite.
  O `-n` é o que faz desistir na hora, sem enfileirar;
- **o `&&` do domingo** encadeia o `reconcile` depois da rodada, dentro do mesmo
  lock, em vez de agendá-lo em paralelo;
- **o ambiente vem do `.env`**, lido pelo `config.py`. O cron não carrega shell
  interativo, e a máquina de referência usa `fish`, que não entende sintaxe de
  `sh`: cada linha é autocontida de propósito.

### Quanto dura cada noite

| | tempo |
|---|---|
| Confluence, escopo de 716 espaços | **~1h40** |
| Confluence, escopo de 30 espaços | **~26 min** |
| Jira incremental | segundos a 1 min |
| index + embed do que mudou (CPU) | menos de 1 min |

O Confluence paga `getPage` de **toda** página **todas** as noites, mesmo sem
mudança: a listagem da 4.2.4 não devolve `version`, só o `getPage` completo
traz. O incremental economiza parse, chunking e upsert — não o round-trip. A
janela é proporcional ao **escopo**, não ao volume de alterações.

### Três coisas que quebram um agendamento noturno

1. **A máquina não pode dormir.** Cron não acorda ninguém: se ela suspende às
   23h, a rodada da meia-noite não acontece.
   ```bash
   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
   ```
2. **Noite perdida é noite perdida.** O cron não executa trabalho atrasado. Não
   é grave: o incremental é por versão e por `updated`, então a noite seguinte
   traz o que ficou. Se quiser recuperação automática no próximo boot, troque por
   um `systemd timer` com `Persistent=true`, aceitando que ele pode disparar em
   horário de trabalho.
3. **(Com GPU) a placa não pode entrar em runtime PM.** Em 09/09/2026 a RX 6900
   XT saiu do barramento PCI por isso, no meio de uma carga (`SETUP.md` §14).
   ```bash
   sudo install -m 644 -o root -g root \
       deploy/90-amdgpu-no-runtime-pm.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && cat /sys/class/drm/card0/device/power/control
   ```

### O preço de uma janela só por dia

O índice passa a ter **até 24 h de atraso**. Para pergunta de assunto —
procedimento, runbook, post-mortem — é irrelevante. Para estado atual de issue,
não: use as ferramentas **ao vivo** do MCP (`get_jira_issue`, `search_jira_jql`,
`get_confluence_page`), que vão à instância na hora e não passam pelo índice. A
separação entre índice e ao vivo existe exatamente para que a janela noturna
possa ser larga sem tornar o sistema mentiroso.

---

## 7. Painel de operação

```bash
PYTHONPATH=$PWD .venv/bin/python -m panel.server
# http://127.0.0.1:8770/
```

Quatro telas, na barra lateral:

- **Visão geral** — documentos por fonte, pontos no índice, pendentes, e o
  relatório da última rodada com as falhas em destaque;
- **Logs** — cauda do arquivo escolhido, um evento por linha, com filtro de "só
  resumos e falhas" e atualização a cada 5 s. Traceback aparece em vermelho;
  barra de progresso de carregamento de modelo é descartada, para não se
  confundir com erro;
- **Acessos** — como o indexador entra nas duas instâncias, sem exibir senha, e
  o botão **Testar acesso agora**, que faz `login` + `getSpaces` ao vivo. Esse
  teste existe porque o login **sozinho não serve**: ele continua passando
  depois de o usuário perder a permissão global "Use Confluence" — foi o que
  aconteceu em 10/09/2026, e só a primeira chamada de verdade denuncia;
- **Agenda** — o horário da janela, as fontes, e o que vai para o crontab. Salvar
  reescreve **apenas** o bloco entre `# BEGIN rag-painel` e `# END rag-painel`,
  preservando todo o resto do crontab.

Para deixá-lo no ar:

```bash
sudo install -m 644 deploy/rag-painel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rag-painel
```

**Segurança.** O painel altera o crontab e **não tem autenticação**. O padrão é
escutar só em `127.0.0.1`; para acessar de outra máquina, túnel SSH em vez de
expor a porta:

```bash
ssh -L 8770:127.0.0.1:8770 joaquimdp@IP-NOVO
```

Ele **não dispara rodada** de propósito: um clique que começa um trabalho de
horas escrevendo no mesmo SQLite do cron merece mais cuidado do que um botão, e
o `flock` que protege isso está no crontab, não no painel.

---

## 8. Pôr no ar e validar

```bash
sudo install -m 644 deploy/rag-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rag-mcp

.venv/bin/python -m pytest tests/ -q     # 228 testes, nada toca instância real
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync status
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync search "abertura de ticket de backup"
```

No `status`, o que decide se a migração deu certo:

```
documentos        confere com a máquina antiga
pendentes idx     0
pendentes denso   0
pontos            confere com a máquina antiga
pontos c/ denso   igual a "pontos"
```

O `rag-mcp` é serviço de **sistema**, não de usuário: precisa subir no boot sem
ninguém logar. Transporte, porta e bind vêm do `.env`, não do arquivo do serviço.
Depois disso, `ACESSO-MCP.md` é o que o time segue. Dois avisos:

- **o MCP não tem autenticação**: quem alcança a porta lê tudo que está
  indexado, sem as permissões por espaço do Confluence. Quem controla o acesso é
  o firewall;
- **se o IP for DHCP e mudar, todos os clientes param juntos.** Reserva de DHCP
  ou IP fixo, e `MCP_ALLOWED_HOSTS` vazio.

**Decida qual máquina manda.** Se as duas ficarem com o cron ligado, as duas
extraem em paralelo e os dois índices divergem — e ninguém sabe a qual MCP está
perguntando. Ao migrar, tire o cron da antiga.

---

## 9. Armadilhas que já custaram tempo

1. **`MCP_ALLOWED_HOSTS` com o IP antigo.** O SDK recusa pelo cabeçalho `Host` e
   o cliente não explica o motivo. Deixe vazio.
2. **O IP é DHCP.** Se mudar, todos os clientes param juntos. A máquina também
   precisa do cabo de rede conectado.
3. **Caminho absoluto em quatro arquivos de deploy** (§3).
4. **`.env` não está no git.** Sem ele nada sobe, e é o único lugar com as
   credenciais.
5. **Os modelos não se baixam sozinhos.** É proposital: aborta explicando o que
   falta em vez de puxar 4,3 GB no meio de uma rodada.
6. **Store copiado quente.** Use `.backup` do sqlite3.
7. **Store sem índice mente** que está indexado (§5.3).
8. **Login do Confluence não é teste de saúde.** Ele passa mesmo sem a permissão
   global "Use Confluence"; só uma chamada real revela (o painel tem o botão).
9. **(Com GPU)** `/dev/kfd` precisa estar `0666`, e a placa não pode entrar em
   runtime PM (`SETUP.md` §14).

---

## Checklist

```
[ ] hardware conforme §1 — atenção à rede; sem GPU, ajustar o reranker
[ ] docker, cron, git, sqlite3, flock, uv instalados; usuário no grupo docker
[ ] torch instalado (build de CPU, ou o do fabricante da GPU)
[ ] repositório clonado; caminho ajustado nos 4 arquivos de deploy
[ ] .venv criado, dependências instaladas, pytest -q passando
[ ] modelos em cache: e5, reranker, fastembed
[ ] .env ajustado: MCP_ALLOWED_HOSTS vazio, EMBED_DEVICE, RERANK_CANDIDATES
[ ] sync scope confere o escopo ANTES de qualquer extração
[ ] docker compose up -d; Qdrant responde em 127.0.0.1:6333
[ ] store no lugar (.backup do sqlite3)
[ ] índice restaurado por snapshot, e conferido pelo points_count
[ ] status: pendentes 0/0, pontos c/ denso == pontos, documentos conferem
[ ] search devolve resultado com URL
[ ] crontab de meia-noite instalado, com flock; logrotate instalado
[ ] suspend/hibernate mascarados
[ ] rag-mcp e rag-painel ativos; painel só em loopback; firewall no MCP
[ ] cron REMOVIDO da máquina antiga
[ ] time avisado do endereço novo — ACESSO-MCP.md
```
