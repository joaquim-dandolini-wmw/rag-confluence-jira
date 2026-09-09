# SETUP — máquina de IA (servidor do rag-confluence-jira)

Reprodução exata desta máquina. Todos os números aqui foram **medidos nela**,
não estimados. Data da instalação: **2026-09-09**.

---

## 1. Hardware e sistema

| item | valor |
|---|---|
| distro | CachyOS (base Arch) |
| kernel | `7.2.3-1-cachyos` |
| CPU | AMD Ryzen 5 7600, 6 núcleos / 12 threads |
| RAM | 30 GB |
| disco | `/home` em `/dev/nvme0n1p2`, 477 GB (379 GB livres antes desta instalação) |
| GPU 0 | **AMD Radeon RX 6900 XT**, `gfx1030` (RDNA2), 40 CUs, 15,98 GiB de VRAM, PCI `0000:03:00.0`, `card0` |
| GPU 1 | iGPU do Ryzen 7600, `gfx1036`, 1 CU, PCI `0000:12:00.0`, `card1` |
| usuário | `joaquimdp` (uid 1000), shell **fish** |
| diretório da aplicação | `/home/joaquimdp/Documentos/rag` |

O mapeamento GPU→`card` foi conferido pelo `BDFID` do `rocminfo`: `768` = `0x300`
= `0000:03:00.0` = `card0` = `gfx1030`; `4608` = `0x1200` = `0000:12:00.0` =
`card1` = `gfx1036`.

### Permissões de dispositivo

`/dev/kfd`, `/dev/dri/renderD128` e `/dev/dri/renderD129` estão em `0666`.
**O grupo `render` não é necessário nesta máquina** — `joaquimdp` não está nele
e lê e escreve nos três. Não há `usermod` nem relogin a fazer.

---

## 2. ROCm — já vinha instalado, não foi reinstalado

```
rocm-hip-sdk  7.2.4-1        (repo extra do Arch)
rocminfo      7.2.4-1.1
/opt/rocm/.info/version -> 7.2.4
```

Não está no `PATH` padrão. Quando precisar das ferramentas:

```bash
export PATH=/opt/rocm/bin:$PATH
```

O `rocminfo` reporta a placa como `gfx1030` **nativamente**, com o alvo
`amdgcn-amd-amdhsa--gfx1030`. Portanto:

> **`HSA_OVERRIDE_GFX_VERSION` NÃO é necessário nesta máquina.** Foi validado
> sem o override: o torch enxerga a GPU, roda kernel e confere contra a CPU.
> Não defina essa variável — ela só existe para placas que o ROCm não reconhece.

---

## 3. Python e venv

O PyTorch **não tem wheel para Python 3.14**, que é o Python do sistema aqui
(3.14.7). O venv usa 3.12, instalado pelo `uv` sem tocar no Python do sistema:

```
uv      0.12.11         (~/.local/bin)
venv    .venv, Python 3.12.14
```

Nada é instalado no Python do sistema. Todo comando usa `.venv/bin/python`.

---

## 4. PyTorch com build ROCm

### O que foi instalado

```
torch         2.14.0+rocm7.2
triton-rocm   3.8.0
```

```python
torch.__version__   == '2.14.0+rocm7.2'
torch.version.hip   == '7.2.53211'
torch.version.cuda  is None
```

Instalado com:

```bash
cd /home/joaquimdp/Documentos/rag
VIRTUAL_ENV=.venv uv pip install \
  --index-url https://download.pytorch.org/whl/rocm7.2 \
  "torch==2.14.0+rocm7.2"
```

O wheel tem **6,22 GB** (`content-length: 6224871252`) porque empacota as
bibliotecas ROCm próprias. Com `triton-rocm` (344,9 MiB) o `.venv` ficou em
**16 GB**. Baixar leva ~5 min a 50 MiB/s.

### Por que a 7.2 e não a 6.3/6.4

O índice `rocm7.2` do `download.pytorch.org` casa exatamente com o ROCm 7.2.4 do
sistema, e tem wheel cp312. Os índices disponíveis com cp312, conferidos na
máquina em 2026-09-09:

```
rocm6.2 / rocm6.2.4 / rocm6.3 / rocm6.4  ->  até torch 2.9.1
rocm7.0 -> 2.10.0   rocm7.1 -> até 2.13.0   rocm7.2 -> até 2.14.0
```

### A verificação que realmente decide: `gfx1030` está compilado no wheel?

Os arquivos de CI do PyTorch (`.ci/docker/build.sh`) listam
`PYTORCH_ROCM_ARCH="gfx90a;gfx942;gfx950;gfx1100"` — **sem `gfx1030`**. Isso é a
imagem de teste, não o wheel de release, e o wheel publicado é mais amplo. Não
presuma por esses arquivos; pergunte ao torch instalado:

```python
>>> torch.cuda.get_arch_list()
['gfx900', 'gfx906', 'gfx908', 'gfx90a', 'gfx942', 'gfx950',
 'gfx1030', 'gfx1100', 'gfx1101', 'gfx1102', 'gfx1103',
 'gfx1200', 'gfx1201', 'gfx1150', 'gfx1151']
```

`gfx1030` **está** na lista. Confirmado por kernel de verdade, não só pela lista:

| precisão | matmul 8192³ |
|---|---|
| fp32 | **18,35 TFLOP/s** |
| fp16 | **34,91 TFLOP/s** |

E um matmul 2048³ confere contra a CPU com erro máximo `4,8e-04`.

Se algum dia esse teste falhar num wheel novo, desça a escada:
`2.14.0+rocm7.2` → `2.13.0+rocm7.1` → `2.9.1+rocm6.4` → `2.9.1+rocm6.3`.

### NÃO use `HIP_VISIBLE_DEVICES`. Escolha a GPU pelo número de compute units

O torch enumera **as duas** GPUs:

```
device 0: 'AMD Radeon Graphics'  arch='gfx1030'  15.98 GiB  40 CUs   <- a boa
device 1: 'AMD Ryzen 5 7600...'  arch='gfx1036'  15.11 GiB   1 CU    <- iGPU
```

Duas coisas medidas aqui, as duas contraintuitivas:

1. **Rodar kernel no device 1 (a iGPU) mata o processo com SIGSEGV** (exit 139),
   sem exceção Python que dê para tratar.

2. **`HIP_VISIBLE_DEVICES` não usa a mesma ordenação do torch.** Com
   `HIP_VISIBLE_DEVICES=0` medi `device_count()==1` reportando **`gfx1036`** —
   a iGPU — mesmo com a discreta presente e mesmo sendo ela o device 0 da
   enumeração padrão do torch. O efeito é o pior possível: não dá erro, roda, e
   roda ~7x mais lento num chip de 1 compute unit. Uma carga de 148 mil fatias
   passa de 28 minutos para horas e nada no log denuncia.

Por isso a seleção é feita **no código**, por número de compute units, e não por
variável de ambiente — ver `_resolve_device()` em `indexer/embeddings.py`:

- `EMBED_DEVICE=cuda` escolhe a GPU com mais compute units (40 contra 1);
- qualquer device com menos de 4 CUs é **recusado** com mensagem explícita, em
  vez de rodar horas em silêncio;
- `EMBED_DEVICE=cuda:N` fixa um índice, e ainda assim passa pela mesma guarda.

Tamanho de memória **não** serve para distinguir as duas: a iGPU reporta
15,11 GiB de "VRAM" (memória do sistema compartilhada) contra 15,98 GiB da
discreta.

---

## 5. Modelo de embedding — `intfloat/multilingual-e5-large`

Cache local em `models/e5`, no mesmo padrão do `models/fastembed`: o runtime é
**offline por padrão e nunca baixa em silêncio**.

```
models/e5/models--intfloat--multilingual-e5-large/  2,2 GB
revisão: 3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3
```

Pré-cache (só o necessário para o backend PyTorch — deixa de fora o
`pytorch_model.bin`, o `onnx/` e o `openvino/`, que somam ~5 GB inúteis):

```python
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="intfloat/multilingual-e5-large",
    cache_dir="models/e5",
    allow_patterns=[
        "config.json", "model.safetensors", "tokenizer.json",
        "tokenizer_config.json", "special_tokens_map.json",
        "sentencepiece.bpe.model", "sentence_bert_config.json",
        "modules.json", "1_Pooling/config.json",
    ],
)
```

Bibliotecas:

```
sentence-transformers  5.7.0
transformers           5.16.1
tokenizers             0.23.2
safetensors            0.8.0
scikit-learn           1.9.0
scipy                  1.18.1
numpy                  2.5.3
```

> `transformers` 5.x renomeou `torch_dtype` para `dtype` em `from_pretrained`.
> Use `model_kwargs={"dtype": torch.float16}`.

### Teste de sanidade (A.5) — resultado nesta máquina

Embeddings normalizados em L2 (a coleção usa distância de cosseno):
norma medida entre `0,999870` e `1,000441` em fp16, exatamente `1,000000` em
fp32 na CPU.

| par | GPU fp16 | CPU fp32 |
|---|---|---|
| `query: o certificado venceu` × `passage: o certificado expirou` | **0,8793** | 0,8794 |
| `query: o certificado venceu` × `passage: o pedido foi faturado` | **0,8284** | 0,8285 |
| `o certificado venceu` × `o certificado expirou` (sem prefixo) | 0,9400 | 0,9396 |
| `o certificado venceu` × `o pedido foi faturado` (sem prefixo) | 0,8775 | 0,8769 |

GPU fp16 e CPU fp32 concordam em 4 casas decimais — a fp16 não degrada o
resultado de forma perceptível.

**Sobre os prefixos:** nesse par isolado a margem sinônimo-vs-irrelevante fica
até um pouco maior *sem* prefixo (+0,0627 contra +0,0509). Isso **não** significa
que os prefixos são dispensáveis: eles são de recuperação assimétrica, e duas
frases não são um teste de recuperação. Num corpus real de 20.000 fatias deste
store, o top-1 com prefixo é claramente mais no assunto:

| consulta | top-1 COM prefixo | top-1 SEM prefixo |
|---|---|---|
| `boleto bancário do pedido` | *CSU-LVP-Informações do Boleto Bancário do Pedido* | *Foto do Pedido* |
| `erro ao sincronizar pedido` | *FAQ-LVW-Erro A sincronização não pode ser realizada…* | *FAQ-LVW-Erro ao fechar pedido* |
| `aplicativo não gera o app` | *FAQ-LVW-LVP-Erro ao gerar APP Versão 5.XX* | *FAQ-LVW-LVP-Erro ao gerar APP Versão 5.XX* |

Consulta leva `"query: "`, documento leva `"passage: "`. Mantenha.

---

## 6. Medições de VRAM e batch size

Medido com o pior caso pedido — **sequência de 512 tokens**, não a mediana — na
GPU livre (nenhum outro processo na placa).

| | valor medido |
|---|---|
| pesos do e5-large em fp16 | **1,04 GiB** |
| VRAM total utilizável | 15,98 GiB |

Escada de batch com 512 tokens:

| batch | pico de VRAM | ativações | fatias/s |
|---|---|---|---|
| 8 | 1,46 GiB | 0,41 GiB | — |
| 16 | 1,84 GiB | 0,80 GiB | 42,8 |
| 32 | 2,61 GiB | 1,56 GiB | 40,9 |
| 64 | 4,14 GiB | 3,09 GiB | 40,7 |
| 128 | 7,20 GiB | 6,16 GiB | 34,0 |
| 192 | 10,27 GiB | 9,22 GiB | 33,8 |
| **256** | **13,33 GiB** | 12,29 GiB | 34,1 |
| 384 | **OOM** | — | — |

Ou seja: o batch máximo com 512 tokens é **256**. Mas batch grande **não** é
melhor aqui, e é isso que decide o valor de produção. Em fatias **reais** do
store (6.000 fatias, mediana 186 tokens):

| batch | pico de VRAM | fatias/s | 148.085 fatias em |
|---|---|---|---|
| 8 | 1,47 GiB | 89 | 27,7 min |
| **16** | **1,87 GiB** | **89** | **27,6 min** |
| 24 | 2,26 GiB | 88 | 28,1 min |
| 32 | 2,66 GiB | 86 | 28,6 min |
| 48 | 3,45 GiB | 79 | 31,4 min |
| 128 | 7,42 GiB | 71 | 34,9 min |
| 256 | 13,77 GiB | 67 | 36,7 min |

A vazão **satura em batch 8** — 40 CUs se enchem com pouco — e acima de 32 só
piora, porque o padding até o maior item do lote passa a desperdiçar cálculo.
A 16.600 tokens/s a GPU entrega ~18 TFLOP/s efetivos, ou seja está perto do
limite de hardware, não de configuração.

> **`EMBED_BATCH_SIZE=16`** é o valor de produção: mesma vazão do melhor caso,
> **1,87 GiB** de pico (contra 13,77 GiB do batch 256) e a placa sobra para
> outra coisa. O tokenizer não é gargalo: 26.000 a 31.000 fatias/s.

### GPU contra CPU

| device | fatias/s | tokens/s | 148.085 fatias |
|---|---|---|---|
| GPU `gfx1030` fp16, batch 16 | **89** | 16.665 | **~28 min** |
| CPU (6 threads torch) fp32, batch 16 | 3,7 | 681 | **~11 h** |

A GPU é **24x** mais rápida. A carga inicial da Fase 2 é viável em uma janela
noturna curta; na CPU seria uma noite inteira.

---

## 7. O que mais cabe rodando junto nos 16 GB

Esta máquina **já roda um LLM local quantizado em RDNA2** via llama.cpp/ROCm —
serviço systemd de usuário `llama-router.service`, `llama-server` com preset em
`~/models/models.ini`. Portanto a pergunta não é se é possível, é o que caiba
junto. Números:

| componente | VRAM | origem do número |
|---|---|---|
| e5-large fp16, batch 16 | **1,87 GiB** | medido aqui |
| `qwen3.6-35b-a3b` IQ3_XXS, ctx 49152 | **15,32 GiB** | medido no `rocm-smi --showpids` com o serviço no ar |
| KWin, se houver monitor na RX 6900 XT | ~1,5 GiB | documentado no seu próprio `models.ini` |
| total utilizável | 15,98 GiB | — |

Conclusões, sem otimismo:

- **e5 + o 35B com 49k de contexto NÃO cabem juntos.** 15,32 + 1,87 = 17,19 GiB
  contra 15,98 disponíveis. Não é questão de ajuste fino: falta 1,2 GiB.
- **Cabe se o LLM ceder contexto.** O `--ctx-size` é o botão: cortá-lo para
  liberar ~2,5 GiB acomoda o e5 permanentemente. O `models.ini` já dimensiona os
  contextos para deixar ~2 GB livres — é exatamente essa folga que o e5 ocuparia.
- **Um reranker cross-encoder cabe folgado junto do e5.** Um `bge-reranker`
  classe base/large em fp16 fica na mesma ordem do e5 (~1 a 1,5 GiB de pesos) e
  só é chamado sobre 20 a 50 candidatos, então o batch é minúsculo. e5 + reranker
  ficam em ~3,5 GiB, sobrando ~12 GiB para o LLM — o que exclui o 35B a 49k, mas
  acomoda um modelo de ~10 a 11 GiB.
- **O caminho realista de LLM em RDNA2 é llama.cpp, não vLLM.** É o que já está
  provado nesta máquina (135 tok/s no MoE com MTP, registrado no `models.ini`).
  vLLM, exllama e afins têm suporte fraco a `gfx1030`; não conte com eles.
- **Para a carga inicial da Fase 2, pare o LLM.** São ~28 min com a placa livre.
  Descarregar e recarregar o LLM é mais simples e mais rápido do que disputar
  VRAM: `systemctl --user stop llama-router.service` e depois `start`.
- No **regime permanente** o `embed` incremental processa pouca coisa por rodada,
  então 1,87 GiB de residência fixa é um custo pequeno — desde que o `ctx-size`
  do LLM tenha sido ajustado para caber.

> Se houver monitor ligado na RX 6900 XT, subtraia ~1,5 GiB de tudo acima. As
> medições desta seção foram feitas com a máquina **sem monitor conectado**
> (nenhum conector DRM em `connected`) e sem sessão Plasma de usuário ativa —
> só o greeter. Nessa condição a placa tinha 17 MiB em uso de 15,98 GiB.

---

## 8. Docker e Qdrant

```
docker 1:29.7.2-1.1   (joaquimdp no grupo docker, funciona sem sudo)
imagem qdrant/qdrant:v1.19.1
```

Use o `docker-compose.yml` **do repositório**, que já fixa a tag e faz bind só em
loopback. Não escreva outro.

```bash
docker compose up -d
```

| porta | bind |
|---|---|
| 6333 (REST) | `127.0.0.1` |
| 6334 (gRPC) | `127.0.0.1` |

O Qdrant **não usa GPU**: só CPU e disco. Volume persistente `qdrant_storage`.

Estado da coleção `atlassian_kb` nesta máquina:

```
dense  -> size 1024, distance Cosine
bm25   -> sparse, modifier IDF
pontos -> 148.085          status: green          on_disk_payload: true
```

---

## 9. Usuário de serviço e permissões

Os jobs rodam como **`joaquimdp`**, e não sob um usuário de serviço dedicado.
Isso é uma decisão consciente, não um esquecimento:

- a aplicação, o `.venv`, o `.env` (chmod `600`) e o `data/documents.sqlite3`
  estão todos dentro de `/home/joaquimdp`;
- o `document store` é um SQLite com **um único escritor**. Dois usuários
  gravando nele é risco real de lock e de corrupção, sem ganho;
- o transporte do MCP (seção 11) é SSH como `joaquimdp`, e o servidor MCP **só
  lê o Qdrant** — não abre o document store.

O que efetivamente limita privilégio aqui:

| medida | estado |
|---|---|
| `.env` com credenciais | `-rw-------` (600), dono `joaquimdp` |
| `logs/` | `drwxr-x---` (750) |
| acesso ao Jira e ao Confluence | **somente leitura, sempre** |
| Qdrant | `127.0.0.1` apenas, sem serviço de rede novo |
| GPU | sem grupo `render`, sem privilégio extra |

> **Pendência conhecida:** `joaquimdp` pertence ao grupo `wheel`, logo tem sudo.
> Um usuário de serviço realmente sem privilégio exigiria mover a aplicação para
> fora do home (ex.: `/srv/rag`, dono `rag:rag`) e dar leitura de grupo no
> `.env` — o que enfraquece o `600` e muda os caminhos da seção 11. Está
> registrado como decisão aberta.

---

## 10. Agendamento e rotação de log

**Não havia daemon de cron nesta máquina.** Foi instalado:

```bash
sudo pacman -S --needed cronie          # cronie 1.7.2-2.1
sudo systemctl enable --now cronie
```

Rotação de log — o `su` é obrigatório porque o diretório está dentro do home de
um usuário sem privilégio e o logrotate roda como root:

```bash
sudo install -m 644 -o root -g root deploy/logrotate.rag /etc/logrotate.d/rag
sudo logrotate --debug /etc/logrotate.d/rag      # -> "Handling 1 logs"
```

Os jobs estão em **`deploy/crontab.example`**, todos **comentados**, para
habilitar só depois que a Fase 2 fechar:

```
*/15 * * * *  indexer.sync run --only jira
0 2 * * *     indexer.sync run --only confluence --no-count-attachments
30 3 * * 0    indexer.sync reconcile --only jira
```

```bash
crontab deploy/crontab.example      # quando for a hora
```

> O shell de `joaquimdp` é **fish**, que não entende a sintaxe do `sh`. O cron
> usa `SHELL=/bin/bash` explicitamente e cada linha é autocontida; o ambiente vem
> do `.env` lido pelo `config.py`, nunca de variável de shell interativo.
> Variável persistente em fish, quando precisar, é `set -Ux NOME valor`.

---

## 11. Transporte do MCP — stdio por SSH

O transporte continua **stdio**; o cliente de IA o alcança por SSH. Nada de MCP
por HTTP, e o Qdrant não sai de `127.0.0.1`.

No cliente de IA:

```
ssh joaquimdp@cachyos-x8664 "cd ~/Documentos/rag && PYTHONPATH=$HOME/Documentos/rag .venv/bin/python -m mcp_server.server"
```

O acesso à máquina é por **Tailscale SSH**. A ACL da tailnet precisa liberar
`joaquimdp` com:

```json
"action": "accept"
```

Com `"check"` o Tailscale exige autenticação pelo navegador a cada sessão, e o
cliente de IA não tem como abrir navegador.

Motivo da escolha: não sobe serviço de rede novo, e a autenticação é a chave SSH
já administrada.

---

## 12. Ruídos desta máquina que NÃO são defeito

1. **`(null): No such file or directory`**, duas linhas no `import torch` e mais
   duas na inicialização do HIP. Vem do runtime ROCm empacotado no wheel. Sai
   **só na stderr** — verificado: a **stdout permanece limpa**, o que é o que o
   contrato JSON-RPC do MCP exige. Filtre no log se incomodar; não indica falha.

2. **`rocm-smi`** emite `WARNING: AMD GPU device(s) is/are in a low-power state`
   e às vezes `Exception caught: map::at` com a GPU ociosa. Não conclua daí que
   o ROCm está quebrado — valide com `rocminfo` e com um tensor de verdade.

3. **`HIPCachingAllocator ... memory allocation failed with OOM`** como *warning*
   durante a escada de batch: é o alocador tentando um bloco grande, liberando
   cache e conseguindo na segunda. Só é falha quando vira `torch.OutOfMemoryError`.

---

## 13. Fase 2: carga do vetor denso e ajuste da fusão

### Carga inicial, medida

```
33.317 documentos -> 146.712 fatias   em 1.082 s (18,0 min)
135,6 fatias/s   batch 16   device cuda:0 (gfx1030)   0 falhas
pontos com vetor denso: 148.085 de 148.085
```

Acima dos 89 fatias/s medidos em amostra, porque o lote de 1.024 fatias dá mais
material para o `sentence-transformers` ordenar por comprimento antes de fatiar
em batches, e sobra menos padding.

O caminho de escrita foi **verificado ponto a ponto**, não assumido: em amostra
aleatória de 12 pontos, o vetor gravado confere com o recalculado do mesmo texto
(cosseno mínimo `0,999779`, só arredondamento de fp16), a norma L2 é exatamente
`1,000000`, e 12 de 12 fatias recuperam o próprio documento em primeiro lugar no
modo denso.

### Dois ajustes da fusão que só apareceram com medição

**1. O peso do RRF não pode ser igual.** Com peso igual, `VENDAS-14993` perdia o
primeiro lugar para `VENDAS-12493` — token numericamente parecido que o denso
traz e que não tem vizinhança semântica real. Pior: o resultado era
**instável**, cinco execuções idênticas davam ordens diferentes.

| peso bm25:denso | identificador em 1º (3 execuções) |
|---|---|
| 1:1 | 1/3 |
| 1:2 | 0/3 |
| **2:1** | **3/3** |
| 3:1 a 12:1 | 3/3, mas o ganho de sinônimo desaparece a partir de 4:1 |

Usado: `models.RrfQuery(rrf=models.Rrf(k=60, weights=[2.0, 1.0]))`, na ordem
`[bm25, denso]` — a mesma ordem dos prefetch.

**2. `hnsw_ef` alto na perna densa.** A instabilidade acima vinha também da
busca aproximada: com o `ef` padrão o conjunto trazido variava entre chamadas e
a fusão desempatava ao acaso. Fixado em `max(profundidade × 2, 256)`.

**3. As duas pernas não buscam na mesma profundidade.** Foi o ajuste de maior
efeito:

| profundidade | posição do alvo em consulta por sinônimo |
|---|---|
| bm25=50, denso=50 | 46 / 54 / não acha |
| **bm25=5, denso=100** | **17 / 20 / 36** |

O RRF soma `1/(k+rank)`, então cada candidato do BM25 ocupa posição boa mesmo
sendo irrelevante para prosa. O BM25 é instrumento de **precisão** — só as
primeiras posições valem; o denso é de **recall** e precisa de profundidade.

### Resolução: modo `auto`, roteado pela consulta

Como nenhum ajuste da fusão atende os dois critérios, o modo padrão passou a
rotear: identificador vai para o BM25, prosa vai para o denso, identificador no
meio de frase vai para o híbrido. Resultado medido:

| critério | bm25 | denso | híbrido | **auto** |
|---|---|---|---|---|
| `VENDAS-14993` em 1º (3 execuções) | 3/3 | 0/3 | 3/3 | **3/3** |
| `PRUPSYNCPRODUTOS` em 1º | 3/3 | 0/3 | 0/3 | **3/3** |
| alvos de sinônimo achados | 0/3 | 3/3 | 2/3 | **3/3** |
| latência | 1,7 ms | 15,6 ms | 50,9 ms | **1,7 / 15,6 ms** |

`--mode` continua aceitando `bm25`, `dense` e `hybrid` para comparação A/B.

### Deduplicação por documento

Os resultados devolvem a melhor fatia de cada documento, com sobrebusca de 3x.
Antes, um documento longo ocupava 3 das 5 vagas com fatias vizinhas — contexto
desperdiçado para quem consome via MCP.

### Limitação conhecida, medida e não resolvida

A fusão RRF não herda todo o ganho do denso. Números em
[README.md](README.md#fase-2--busca-híbrida-implementada). Resumo: o denso acha
os três alvos de sinônimo (posições 8, 12, 28) que o BM25 não acha em nenhuma
profundidade; o híbrido os coloca em 53, 61 e fora. Nenhum peso resolve os dois
lados ao mesmo tempo.

`PRUPSYNCPRODUTOS` fica fora do top-5 no híbrido contra 1º no BM25 puro. É
estrutural no RRF: um documento presente nas duas listas bate um presente em só
uma, e elevar o peso não inverte isso sem destruir o ganho de sinônimo
(verificado até 12:1). O modo `auto` contorna roteando, não fundindo.

O que ainda falta de verdade: dois dos três alvos de sinônimo ficam nas posições
13 e 28 — dentro do índice, fora de uma página de 10. Fecha com **reranker
cross-encoder** sobre os candidatos. Cabe na mesma GPU: o e5 usa 1,87 GiB dos
15,98 GiB. Fora do escopo da Fase 2.

---

## 14. INCIDENTE 2026-09-09: a RX 6900 XT saiu do barramento PCI

Registrado porque vai acontecer de novo e o sintoma engana.

**O que houve.** Durante a primeira carga do `embed` na GPU, o kernel registrou
a placa **saindo de suspensão** e, cerca de um minuto depois, a máquina fez
**reset a frio** — o journal do boot anterior termina sem nenhuma linha de
desligamento limpo:

```
set 09 10:45:31 kernel: amdgpu 0000:03:00.0: PSP is resuming...
set 09 10:45:31 kernel: amdgpu 0000:03:00.0: SMU is resuming...
set 09 10:45:31 kernel: amdgpu 0000:03:00.0: SMU is resumed successfully!
   (~60 s depois, fim abrupto do journal; boot novo às 10:47:18)
```

**Depois do reboot a placa não voltou.** `lspci` passou a listar somente a iGPU:

```
$ lspci -nn | grep -iE 'vga|display'
0f:00.0 VGA compatible controller [AMD/ATI] Raphael [1002:164e]     <- só a iGPU
$ rocminfo | grep 'Name: *gfx'
  Name:                    gfx1036                                   <- só a iGPU
```

Note que os endereços PCI mudaram: antes a discreta era `0000:03:00.0` e a
integrada `0000:12:00.0`; depois sobrou `0000:0f:00.0`.

**Como recuperar.** Falha de enumeração PCIe não se resolve com `reboot`: o
barramento não é re-treinado num reset morno. É preciso **desligamento completo**
— `poweroff`, cortar a energia por ~30 s, ligar de novo. Se depois disso a placa
ainda não aparecer no `lspci`, aí sim é assunto de hardware (encaixe, cabo de
alimentação, slot).

**Suspeita de causa.** Runtime PM da amdgpu em RDNA2. A placa estava em
suspensão (`rocm-smi` avisava `low-power state`), foi acordada por uma carga
pesada e não sobreviveu ao ciclo. Enquanto não houver certeza, vale desativar a
suspensão automática antes de uma carga longa:

```bash
echo on | sudo tee /sys/class/drm/card0/device/power/control
```

**O que o código aprendeu com isso.** Antes, um `embed` nessa situação rodaria
na iGPU sem reclamar. Agora ele aborta dizendo exatamente isso — ver seção 4 e
`_resolve_device()`. Os testes `test_recusa_rodar_so_na_igpu` e
`test_indice_explicito_apontando_para_a_igpu_tambem_aborta` travam esse
comportamento.

---

## 15. Reproduzir do zero

```bash
# 1. ROCm (repo extra do Arch)
sudo pacman -S --needed rocm-hip-sdk rocminfo      # 7.2.4
export PATH=/opt/rocm/bin:$PATH && rocminfo | grep gfx    # espere gfx1030

# 2. Python 3.12 em venv isolado
uv python install 3.12 && uv venv --python 3.12 .venv

# 3. dependências da Fase 1
VIRTUAL_ENV=.venv uv pip install -r requirements-dev.txt

# 4. PyTorch ROCm
VIRTUAL_ENV=.venv uv pip install \
  --index-url https://download.pytorch.org/whl/rocm7.2 "torch==2.14.0+rocm7.2"
VIRTUAL_ENV=.venv uv pip install "sentence-transformers>=3,<6"

# 5. confirme que o wheel tem gfx1030 E roda kernel
HIP_VISIBLE_DEVICES=0 .venv/bin/python -c "
import torch
assert 'gfx1030' in torch.cuda.get_arch_list(), torch.cuda.get_arch_list()
a = torch.randn(2048, 2048, device='cuda')
assert (a @ a).cpu().isfinite().all()
print(torch.__version__, torch.version.hip)"

# 6. modelos em cache (seção 5) e Qdrant
docker compose up -d

# 7. agendamento
sudo pacman -S --needed cronie && sudo systemctl enable --now cronie
sudo install -m 644 -o root -g root deploy/logrotate.rag /etc/logrotate.d/rag
```

Variáveis dos jobs (já registradas no `.env`):

```
EMBED_DEVICE=cuda          # o PyTorch ROCm usa a mesma API "cuda"
EMBED_BATCH_SIZE=16        # medido nesta GPU
EMBED_CACHE_DIR=./models/e5
```

E **não** defina nenhuma destas duas:

- `HSA_OVERRIDE_GFX_VERSION` — aqui não é necessária, o ROCm 7.2.4 já reporta
  `gfx1030` nativamente;
- `HIP_VISIBLE_DEVICES` — ativamente prejudicial nesta máquina, ver seção 4.
