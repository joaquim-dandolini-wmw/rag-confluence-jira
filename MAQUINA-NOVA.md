# MÁQUINA NOVA — levar um índice pronto para outra máquina

Este documento é para **migrar**: a máquina de origem já tem store e índice
carregados, e você quer levá-los para outra sem reextrair o Jira e o Confluence.

Se a máquina nova vai começar do zero, com índice vazio e carga inicial a fazer,
o documento é a **[INICIALIZACAO.md](INICIALIZACAO.md)** — que também traz a
recomendação de hardware e o agendamento noturno. Aqui não se repete a
instalação: siga a INICIALIZACAO.md §2 e §3 e volte para cá na hora de levar os
dados. O `SETUP.md` tem o *por quê* de cada escolha da máquina de referência e as
medições.

Números medidos em **10/09/2026**, na origem `cachyos-x8664` (10.2.1.132). Eles
crescem com o escopo do Confluence: confira com
`python -m indexer.sync status` antes de dimensionar a cópia.

---

## O que viaja, o que se reconstrói

| o que | tamanho | como vai | está no git? |
|---|---|---|---|
| o repositório | — | `git clone` | sim |
| `.env` | 8 KB | cópia manual, por `scp` | **não** (`.gitignore`) |
| `data/documents.sqlite3` | 99 MB | cópia a frio ou `.backup` | **não** |
| `models/` (e5 2,2 G + reranker 2,2 G + fastembed 164 K) | 4,3 GB | cópia, ou pré-cache com internet | **não** |
| volume `rag_qdrant_storage` | 887 MB | snapshot ou tar (Parte 2) | **não** |
| `.venv/` | — | **não copie**, recrie | não |
| `logs/`, `certs/` | — | não vão | não |

A hierarquia entre os dois bancos importa para tudo o que vem depois: o
**SQLite é a fonte da verdade** e o **Qdrant é derivado dele**. Dá para chegar
na máquina nova sem levar o Qdrant e reconstruí-lo (Caminho C), ao custo de
horas de GPU. O contrário não existe: sem o SQLite, reconstruir significa
reextrair o Jira e o Confluence inteiros.

O `.env` carrega `JIRA_PAT` e `CONFLUENCE_PASSWORD` em texto puro. Passe por
`scp`, nunca por e-mail, chat ou anexo.

---

## Parte 1 — o que a migração muda

A instalação é a da `INICIALIZACAO.md` §2. Quatro pontos são específicos de
migração:

### 1. O `.env` vem da origem, e três coisas nele mudam

```bash
scp joaquimdp@10.2.1.132:~/Documentos/rag/.env .env
```

1. **`MCP_ALLOWED_HOSTS`** traz os IPs da máquina antiga. O SDK valida o
   cabeçalho `Host` e **recusa** o que não estiver na lista, então com o IP novo
   ausente todo cliente para de conectar. Acrescente o IP novo, ou deixe a
   variável **vazia**, que faz o servidor descobrir o próprio IP no boot;
2. **`CONFLUENCE_SPACES=auto`** significa que o escopo é o que o usuário de
   serviço enxerga — pode ter mudado desde a última rodada da origem. Rode
   `python -m indexer.sync scope` (só analisa) antes de qualquer extração;
3. **caminhos** (`STORE_PATH`, `*_CACHE_DIR`) são relativos ao repositório e não
   precisam mudar; o que muda é o caminho absoluto dentro de
   `deploy/rag-mcp.service`, `deploy/crontab.example` e `deploy/mcp-stdio.sh`,
   se o diretório ou o usuário forem diferentes.

### 2. Os modelos podem ser copiados em vez de baixados

```bash
rsync -a --info=progress2 joaquimdp@10.2.1.132:~/Documentos/rag/models/ models/
```

São 4,3 GB. A alternativa é o pré-cache com internet
(`INICIALIZACAO.md` §2), que baixa o mesmo conteúdo.

### 3. O store é cópia de arquivo, e não pode ser copiado quente

O store usa WAL: um `cp` no meio de uma escrita leva metade de uma transação.
Ou pare o cron da origem, ou use o `.backup`, que é consistente **com o banco
em uso**:

```bash
# na máquina ANTIGA
sqlite3 data/documents.sqlite3 ".backup /tmp/store-migracao.sqlite3"
# na máquina NOVA
scp joaquimdp@10.2.1.132:/tmp/store-migracao.sqlite3 data/documents.sqlite3
```

### 4. Decida qual máquina manda

Se as duas ficarem com o cron ligado, as duas extraem em paralelo e os dois
índices divergem — e o time não tem como saber a qual dos dois MCPs está
perguntando. Ao migrar, tire o cron da antiga: `crontab -r` ou comente as
linhas. Depois avise o time do endereço novo (`ACESSO-MCP.md`).

### Validação, antes de ligar qualquer automação

```bash
.venv/bin/python -m pytest tests/ -q          # 202 testes, nada toca instância real
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

---

## Parte 2 — passar o Qdrant de cá para lá

Estado atual da coleção na origem, para comparar depois:

```
coleção atlassian_kb    156.656 pontos    status green
denso  -> size 1024, distance Cosine        esparso -> bm25, modifier IDF
on_disk_payload true    volume rag_qdrant_storage, 887 MB
```

Antes de qualquer um dos caminhos, **pare o que escreve**: `crontab -r` na
máquina antiga, ou espere uma rodada terminar. Snapshot tirado no meio de uma
indexação sai consistente, mas sai *velho* — sem os documentos que ainda estavam
sendo gravados.

### Caminho A — snapshot da coleção (recomendado)

Testado nesta versão do Qdrant, de ponta a ponta: criar, baixar, apagar a
coleção e restaurar devolveu os pontos, o esquema denso e esparso, o
`on_disk_payload` e os payloads intactos. A coleção **não precisa existir** na
máquina nova: o upload a recria.

```bash
# 1. na máquina ANTIGA: cria o snapshot e guarda o nome
NOME=$(curl -s -X POST http://127.0.0.1:6333/collections/atlassian_kb/snapshots \
       | python -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')
echo $NOME     # ex.: atlassian_kb-7436581249880683-2026-09-10-12-29-02.snapshot

# 2. baixa para arquivo
curl -s -o /tmp/$NOME \
     "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/$NOME"
ls -lh /tmp/$NOME

# 3. leva para a máquina nova
scp /tmp/$NOME joaquimdp@IP-NOVO:/tmp/

# 4. na máquina NOVA, com o Qdrant já de pé
curl -s -X POST \
  "http://127.0.0.1:6333/collections/atlassian_kb/snapshots/upload?priority=snapshot" \
  -H 'Content-Type: multipart/form-data' -F "snapshot=@/tmp/$NOME"

# 5. confere
curl -s http://127.0.0.1:6333/collections/atlassian_kb \
  | python -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r["points_count"], r["status"])'
```

O `priority=snapshot` diz ao Qdrant que o conteúdo do arquivo vence o que
estiver na coleção — é o que se quer numa restauração. Terminado o teste,
apague o snapshot da origem, que ele ocupa quase o tamanho da coleção:
`curl -X DELETE ".../snapshots/$NOME"`.

### Caminho B — tar do volume, com o container parado

Serve quando você quer cópia **byte a byte de tudo**: todas as coleções, os
aliases e o `raft_state.json`, não só a `atlassian_kb`.

```bash
# máquina ANTIGA
docker compose down
docker run --rm -v rag_qdrant_storage:/from -v /tmp:/backup \
       qdrant/qdrant:v1.19.1 tar czf /backup/qdrant-storage.tgz -C /from .
docker compose up -d
scp /tmp/qdrant-storage.tgz joaquimdp@IP-NOVO:/tmp/

# máquina NOVA
docker compose up -d && docker compose down     # cria o volume vazio
docker run --rm -v rag_qdrant_storage:/to -v /tmp:/backup \
       qdrant/qdrant:v1.19.1 tar xzf /backup/qdrant-storage.tgz -C /to
docker compose up -d
```

O `down` não é zelo excessivo: o Qdrant tem WAL e faz flush a cada 5 s, e um tar
tirado quente pode pegar um segmento pela metade. O volume tem
`aliases/  collections/  raft_state.json  tmp/` — vá pela raiz, não por
subdiretório.

### Caminho C — não levar o Qdrant, reconstruir do store

Se a rede não ajuda ou o snapshot deu problema, o índice é **inteiramente
derivável** do `documents.sqlite3`:

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync index --reindex-all
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync embed --reembed-all
```

**Os dois `--*-all` são obrigatórios, e é aqui que se erra.** O store guarda
`indexed_hash` e `embedded_hash` por documento; num store copiado eles já estão
preenchidos, então o incremental olha, conclui "nada pendente" e não faz nada.
Você fica com `pendentes idx: 0` num índice vazio — o `status` mente e a busca
não acha nada.

Custo, com o número medido em carga de verdade — 146.712 fatias em 18 min, a
**135,6 fatias/s**: as 156 mil fatias de hoje levam cerca de **20 min de GPU**,
mais alguns minutos de chunking. Sem GPU, a 3,7 fatias/s, o mesmo trabalho passa
de **11 h**. É o caminho lento; o snapshot leva o tempo de copiar 887 MB.

---

## Armadilhas que já custaram tempo

1. **`MCP_ALLOWED_HOSTS` com o IP antigo.** O SDK recusa pelo cabeçalho `Host` e
   o cliente não diz o motivo com clareza. Deixe vazio ou inclua o IP novo.
2. **O IP é DHCP.** Se mudar, todos os clientes param juntos, e o
   `MCP_ALLOWED_HOSTS` fixo torna isso pior. Reserva de DHCP ou IP fixo resolve.
   A máquina também precisa do cabo de rede conectado.
3. **Caminho absoluto em três arquivos** (Parte 1, item 1). Serviço e cron
   falham em silêncio.
4. **`.env` não está no git.** Sem ele nada sobe, e ele é o único lugar com as
   credenciais.
5. **Os modelos não se baixam sozinhos.** `ALLOW_MODEL_DOWNLOAD=0` é proposital:
   o runtime aborta explicando o que falta em vez de puxar 4,3 GB no meio de uma
   rodada.
6. **Store copiado quente.** Use `.backup` do sqlite3 ou pare o cron.
7. **Store sem índice mente** que está indexado (Caminho C).
8. **`/dev/kfd` precisa estar `0666`** para a GPU funcionar sem grupo extra, e a
   placa não pode entrar em runtime PM — foi o que tirou a RX 6900 XT do
   barramento PCI em 09/09 (`SETUP.md` §14).

---

## Checklist

```
[ ] instalação feita pela INICIALIZACAO.md §2 (docker, cron, uv, .venv, torch)
[ ] repositório clonado; caminho conferido nos 3 arquivos de deploy
[ ] .env copiado; MCP_ALLOWED_HOSTS revisto; sync scope confere o escopo
[ ] models/ com e5, reranker e fastembed
[ ] docker compose up -d; Qdrant responde em 127.0.0.1:6333
[ ] data/documents.sqlite3 no lugar (cópia a frio ou .backup)
[ ] índice migrado: Caminho A, B ou C
[ ] pytest -q passa; status bate com a origem; search devolve resultado
[ ] rag-mcp habilitado e ativo; logrotate e crontab instalados
[ ] (GPU) regra udev instalada; power/control diz "on"
[ ] cron REMOVIDO da máquina antiga
[ ] time avisado do endereço novo — ACESSO-MCP.md
```
