# INICIALIZAÇÃO — subir o projeto do zero, em máquina própria

Este é o documento para quem vai colocar o rag-confluence-jira de pé onde não
existe nada: máquina nova, índice vazio, primeira carga a fazer. Ele termina com
o sistema atualizando sozinho **uma vez por noite, à meia-noite**.

Qual documento é qual:

| documento | quando usar |
|---|---|
| **este** | máquina nova, começar do zero, reconstruir o índice extraindo tudo |
| [MAQUINA-NOVA.md](MAQUINA-NOVA.md) | levar um store e um índice **já prontos** de outra máquina |
| [SETUP.md](SETUP.md) | por quê de cada escolha e as medições da máquina de referência |
| [ACESSO-MCP.md](ACESSO-MCP.md) | como o time conecta o cliente depois que está no ar |

Todos os números aqui foram medidos na instância real (Jira 8.14 e Confluence
4.2.4 da empresa) e na máquina de referência, em set/2026.

---

## 1. Recomendação de máquina

### Onde o tempo realmente vai

Esta é a medida que decide o hardware. Carga inicial completa, instância inteira
— **65.695 documentos** (40.730 páginas e blogs do Confluence + 24.965 issues do
Jira):

| fase | tempo | limitado por |
|---|---|---|
| extração do Confluence | **4h32** (3 chamadas/página) ou **3h10** sem contar anexos | latência do XML-RPC, uma chamada por vez |
| extração do Jira | **~18 min** | REST em lotes de 100 |
| chunking e indexação | minutos | 1 núcleo de CPU |
| vetor denso na GPU | **~36 min** (≈289 mil fatias a 135,6 fatias/s) | GPU |
| **total** | **~5h30**, ou ~4h10 sem anexos | |

**Mais de 80% do tempo é espera de rede.** O Confluence 4.2.4 não tem REST e o
XML-RPC não tem paginação nem filtro por data: cada página custa `getPage` +
`getLabelsById` + `getAttachments`, uma de cada vez, 140 ms por chamada. Nenhuma
CPU e nenhuma GPU do mundo encurtam isso.

A consequência prática é contraintuitiva: **GPU maior não reduz a janela**. A
GPU responde por 36 minutos de um trabalho de cinco horas e meia, e o pico de
VRAM medido é de **1,87 GiB**.

### O que pedir

| item | mínimo | recomendado | por quê |
|---|---|---|---|
| CPU | 4 núcleos | 6 a 8 núcleos | extração é serial e espera rede; o chunking usa 1 núcleo |
| RAM | 16 GB | **32 GB** | Qdrant ~3 GB neste tamanho (vetores densos em RAM) + PyTorch + folga |
| disco | 120 GB SSD | 500 GB NVMe | hoje o conjunto todo dá ~7 GB; NVMe ajuda a latência do Qdrant, não a janela |
| GPU | 8 GB VRAM | 12 a 16 GB VRAM | pico medido 1,87 GiB; e5-large + reranker somam ~2,2 GB de pesos. 8 GB já sobra |
| rede | 1 Gbit com fio | 1 Gbit com fio, mesma LAN do Jira e do Confluence | **é aqui que a janela é decidida**: 122 mil requisições, o que pesa é latência, não banda |

**Sem GPU o projeto roda, mas a carga inicial não cabe numa noite.** Medido: na
CPU o embed faz 3,7 fatias/s contra 89 na GPU — 24x mais lento. As 289 mil
fatias da primeira carga viram **~21 h**. Uma vez carregado, as noites seguintes
mexem em poucas dezenas de documentos e a CPU dá conta; mas qualquer
reconstrução total volta a ser um fim de semana.

**Se a GPU for NVIDIA**, o único ponto que muda é o wheel do PyTorch: instale o
build `cu12` em vez do `rocm7.2` do `SETUP.md` §4, e ignore as seções de ROCm,
`gfx1030` e `/dev/kfd`. O código usa a API `cuda` nos dois casos — `EMBED_DEVICE=cuda`
continua correto.

Máquina de referência, para comparar: Ryzen 5 7600 (6 núcleos), 30 GB de RAM,
NVMe de 477 GB, Radeon RX 6900 XT de 16 GB.

---

## 2. Instalação

```bash
sudo pacman -S --needed docker docker-compose git cronie sqlite flock
sudo systemctl enable --now docker cronie
sudo usermod -aG docker $USER        # relogue depois disto
```

Fora do Arch, troque o gerenciador: precisam existir Docker, cron, git, o CLI do
sqlite, o `flock` e o `uv`.

GPU AMD: siga `SETUP.md` §2 a §4 — ROCm 7.2.4, o wheel `torch==2.14.0+rocm7.2` e
a verificação de que `gfx1030` (ou o seu alvo) está compilado no wheel. É a única
parte da instalação que já deu trabalho de verdade.

```bash
git clone https://github.com/joaquim-dandolini-wmw/rag-confluence-jira.git
cd rag-confluence-jira

uv python install 3.12
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -r requirements-dev.txt
# GPU: o torch vem depois, pelo índice do fabricante (SETUP.md §4)

docker compose up -d                  # Qdrant v1.19.1, só em 127.0.0.1
curl -s http://127.0.0.1:6333/ | head -1
```

O caminho do repositório e o nome do usuário estão escritos por extenso em
`deploy/rag-mcp.service`, `deploy/crontab.example` e `deploy/mcp-stdio.sh`. Se
não for `/home/joaquimdp/Documentos/rag`, edite os três — serviço e cron falham
em silêncio.

### Modelos

O runtime é **offline por padrão** (`ALLOW_MODEL_DOWNLOAD=0`): se um artefato
faltar, ele aborta dizendo qual, em vez de baixar 4,3 GB no meio de uma rodada.

```bash
ALLOW_MODEL_DOWNLOAD=1 .venv/bin/python -m scripts.precache_models
```

São `models/e5` (2,2 GB), `models/reranker` (2,2 GB) e `models/fastembed`
(164 KB). Se a máquina não tem internet, rode isso em outra e copie o diretório.

---

## 3. Configuração

```bash
cp .env.example .env
```

O `.env.example` documenta cada variável. O que **precisa** ser decidido antes da
primeira carga:

| variável | cuidado |
|---|---|
| `JIRA_URL`, `JIRA_PAT` | PAT de conta de serviço **somente leitura** |
| `JIRA_PROJECTS` | obrigatório e não vazio; vazio **aborta** de propósito |
| `CONFLUENCE_URL` | inclua o context path (`https://host/confluence`), não só o host |
| `CONFLUENCE_USER`, `CONFLUENCE_PASSWORD` | a 4.2.4 **não** aceita PAT, é usuário e senha |
| `CONFLUENCE_SPACES` | lista fixa, ou `auto` para "o que o usuário de serviço enxerga" |
| `MCP_ALLOWED_HOSTS` | deixe **vazio** para o servidor descobrir o próprio IP no boot |
| `EMBED_DEVICE` | `cuda` com GPU (inclusive AMD), `cpu` sem |

**Confira o escopo antes de extrair.** Este comando só analisa, não escreve
nada:

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync scope
```

Ele diz quantos espaços entram, quais e — em máquina já carregada — o que sai. É
a diferença entre uma janela de 1h40 e uma de 5h: medido nesta instância, um
usuário de serviço enxerga **30 espaços** e outro enxerga **716**.

Com `CONFLUENCE_SPACES=auto`, quem controla o escopo é a **administração de
grupos do Confluence**, não este arquivo — adicionar um grupo ao usuário de
serviço traz os espaços dele na próxima rodada, e remover **purga** o conteúdo
daquele espaço do store e do índice. Não coloque espaço restrito (RH, jurídico,
financeiro) no escopo: o MCP não tem autenticação e não reproduz as permissões
por espaço do Confluence.

Antes de qualquer carga:

```bash
.venv/bin/python -m pytest tests/ -q      # 202 testes, nada toca instância real
```

---

## 4. A primeira carga

**Faça acompanhando, na mão, e não pelo cron.** São horas de trabalho e é a única
rodada em que tudo é novo; se algo estiver errado no escopo ou na credencial, é
aqui que se descobre.

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync run > logs/carga-inicial.log 2>&1
```

Um comando só faz tudo, nesta ordem: extrai o Confluence, extrai o Jira, indexa
(chunking + BM25) e por último popula o vetor denso. **O denso vem depois de
propósito**: o embed atualiza o vetor de um ponto que já existe, então o ponto
precisa existir antes.

Acompanhe por espaço:

```bash
tail -f logs/carga-inicial.log | grep -E 'espaço concluído|progresso|falhas'
```

Um espaço grande fica até 30 minutos entre "listado" e "concluído" — por isso
existe a linha `progresso do espaço` a cada 250 documentos, com percentual,
ritmo e ETA. **Queda no meio não perde trabalho:** o store commita a cada 25
documentos e o cursor do Jira é salvo no `finally`. Rodar de novo continua de
onde parou.

Duas opções que valem na primeira noite:

- `--no-count-attachments` corta um terço dos round-trips (de 4h32 para 3h10) ao
  custo de não ter a contagem de anexos. Anexos são **contados, não indexados**;
  se o número não importa, deixe desligado para sempre;
- `--full` força reextração ignorando o incremental. Na primeira carga é
  redundante — não há nada para pular.

Ao terminar, confira:

```bash
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync status
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync search "como abrir ticket de backup"
```

No `status`, `pendentes idx` e `pendentes denso` em **0**, e `pontos c/ denso`
igual a `pontos`. `falhas: 0` no relatório da rodada.

---

## 5. O agendamento de meia-noite

Uma janela por dia, à meia-noite, rodando até acabar. Substitua o
`deploy/crontab.example` por isto:

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""

RAG=/home/joaquimdp/Documentos/rag

# Segunda a sábado: extract + index + embed das duas fontes.
0 0 * * 1-6 cd $RAG && flock -n /tmp/rag-sync.lock env PYTHONPATH=$RAG .venv/bin/python -m indexer.sync run --no-count-attachments >> logs/noturno.log 2>&1

# Domingo: a mesma rodada e, DEPOIS que ela termina, a reconciliação de deleções.
0 0 * * 0 cd $RAG && flock -n /tmp/rag-sync.lock bash -c 'env PYTHONPATH=$RAG .venv/bin/python -m indexer.sync run --no-count-attachments && env PYTHONPATH=$RAG .venv/bin/python -m indexer.sync reconcile --only jira' >> logs/noturno.log 2>&1
```

```bash
crontab deploy/crontab.example
sudo install -m 644 -o root -g root deploy/logrotate.rag /etc/logrotate.d/rag
```

Por que está escrito assim:

- **`flock -n`** garante uma rodada por vez. Se uma noite atrasar e passar da
  meia-noite seguinte, a nova desiste em vez de as duas disputarem o mesmo
  SQLite. O `-n` é o que faz desistir na hora, sem enfileirar;
- **o `&&` do domingo** encadeia o `reconcile` *depois* da rodada, dentro do
  mesmo lock, em vez de agendá-lo em paralelo;
- **o ambiente vem do `.env`**, lido pelo `config.py`. O cron não carrega shell
  interativo, e a máquina de referência usa `fish`, que não entende sintaxe de
  `sh`: cada linha é autocontida de propósito.

### O que esperar toda noite

| | tempo |
|---|---|
| Confluence, escopo de 716 espaços | **~1h40** |
| Confluence, escopo de 30 espaços | **~26 min** |
| Jira incremental | segundos a 1 min |
| index + embed do que mudou | segundos a poucos minutos |

O Confluence paga `getPage` de **toda** página todas as noites, mesmo sem
mudança nenhuma: a API da 4.2.4 não devolve `version` na listagem, só no
`getPage` completo. O incremental economiza parse, chunking e upsert — não o
round-trip. É por isso que a janela noturna é proporcional ao **tamanho do
escopo**, não ao volume de alterações.

### Três coisas que quebram um agendamento noturno

1. **A máquina não pode dormir.** Cron não acorda ninguém: se a máquina suspende
   às 23h, a rodada da meia-noite simplesmente não acontece.
   ```bash
   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
   ```
2. **Noite perdida é noite perdida.** O cron não roda trabalho atrasado. Não é
   grave — o incremental é por versão e por `updated`, então a noite seguinte
   traz tudo o que ficou. Se você quiser que uma rodada perdida seja executada no
   próximo boot, troque o cron por um `systemd timer` com `Persistent=true`,
   aceitando que ele pode disparar em horário de trabalho.
3. **A GPU não pode entrar em runtime PM.** Em 09/09/2026 a RX 6900 XT saiu do
   barramento PCI por causa disso, no meio de uma carga (`SETUP.md` §14).
   ```bash
   sudo install -m 644 -o root -g root \
       deploy/90-amdgpu-no-runtime-pm.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   cat /sys/class/drm/card0/device/power/control     # espere "on"
   ```

### O preço de uma janela só por dia

O índice passa a ter **até 24 h de atraso**. Para pergunta de assunto —
procedimento, runbook, post-mortem — isso é irrelevante. Para estado atual de
issue, não: use as ferramentas **ao vivo** do MCP (`get_jira_issue`,
`search_jira_jql`, `get_confluence_page`), que vão à instância na hora e não
dependem do índice. A separação entre índice e ao vivo existe exatamente para
que a janela noturna possa ser larga sem tornar o sistema mentiroso.

---

## 6. Pôr no ar

```bash
sudo install -m 644 deploy/rag-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rag-mcp
systemctl is-active rag-mcp
```

O serviço é de **sistema**, não de usuário: precisa subir no boot sem ninguém
logar. Transporte, porta e bind vêm do `.env`, não do arquivo do serviço.

Depois disso, `ACESSO-MCP.md` é o que o time segue para conectar. Dois avisos que
pertencem a este documento:

- **o MCP não tem autenticação.** Quem alcança a porta lê tudo que está
  indexado, sem as permissões por espaço do Confluence. Quem controla o acesso é
  o firewall;
- **se o IP for DHCP e mudar, todos os clientes param juntos.** Reserva de DHCP
  ou IP fixo, e `MCP_ALLOWED_HOSTS` vazio para o servidor descobrir o próprio
  endereço no boot.

---

## Checklist

```
[ ] hardware conforme §1 — atenção à rede, não à GPU
[ ] docker, cron, git, sqlite3, flock, uv instalados; usuário no grupo docker
[ ] (GPU) driver + torch conferidos: o wheel roda kernel de verdade
[ ] repositório clonado; caminho ajustado nos 3 arquivos de deploy
[ ] .venv criado, dependências instaladas, pytest -q passando
[ ] modelos em cache: e5, reranker, fastembed
[ ] .env preenchido; MCP_ALLOWED_HOSTS vazio; EMBED_DEVICE correto
[ ] sync scope confere o escopo ANTES da carga
[ ] docker compose up -d; Qdrant responde
[ ] carga inicial rodada À MÃO e acompanhada; falhas: 0
[ ] status: pendentes idx 0, pendentes denso 0, pontos c/ denso == pontos
[ ] search devolve resultado com URL
[ ] crontab de meia-noite instalado, com flock; logrotate instalado
[ ] suspend/hibernate mascarados; (GPU) regra udev e power/control "on"
[ ] rag-mcp ativo; firewall restringindo a porta; time avisado
```
