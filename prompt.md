Vamos preparar esta máquina como servidor de um sistema de busca semântica
interno e, em seguida, implementar a Fase 2 dele. Leia esta mensagem inteira
antes de fazer qualquer coisa: ela é a especificação completa.

═══════════════════════════════════════════════════════════
CONTEXTO — O QUE JÁ EXISTE
═══════════════════════════════════════════════════════════

A Fase 1 do projeto está pronta, commitada e VALIDADA CONTRA AS INSTÂNCIAS
REAIS. Repositório: github.com/joaquim-dandolini-wmw/rag-confluence-jira

O que ele faz hoje: busca unificada sobre um Jira 8.14 Server e um Confluence
4.2.4 on-premise, exposta a clientes de IA via MCP. Arquitetura em dois ciclos
desacoplados:

  extração (Jira REST v2 + Confluence XML-RPC)
      -> DOCUMENT STORE LOCAL em SQLite (texto já limpo e normalizado)
      -> chunking
      -> Qdrant

O document store é a peça central: a extração é cara (1h19min contra as
instâncias reais), mas reconstruir o índice a partir do store leva ~6 minutos
e não faz UMA chamada sequer ao Atlassian. É isso que torna a Fase 2 viável
sem reextrair nada.

Números reais medidos em produção (setembro/2026):

  documentos no store            33.592  (8.662 Confluence + 24.930 Jira)
  fatias no índice              148.085
  tamanho das fatias            mediana 218 chars, p90 1.225, máx 1.400
  tokens por fatia (estimado)   mediana 68, p90 383, MÁXIMO 438
  arquivo do store              78 MB

O ÍNDICE ATUAL É SÓ LEXICAL (BM25 sparse no Qdrant, via fastembed). Ele resolve
identificador exato, acentuação e ranking, mas NÃO resolve sinônimo. Verificado
contra os dados reais — estes três pares NÃO encontram o mesmo documento hoje:

  "boleto bancário do pedido"      x  "cobrança bancária da ordem de compra"
  "erro ao sincronizar pedido"     x  "falha no envio do pedido"
  "aplicativo não gera o app"      x  "smartphone nao compila o pacote"

Fechar essa lacuna é exatamente o objetivo da Fase 2.

═══════════════════════════════════════════════════════════
ESTA MÁQUINA — JÁ INSPECIONADA E PARCIALMENTE PREPARADA
═══════════════════════════════════════════════════════════

GPU AMD Radeon RX 6900 XT, 16 GB VRAM, RDNA2 (gfx1030).

  distro     CachyOS (base Arch), kernel 7.2.3-1-cachyos
  usuário    joaquimdp (uid 1000), home /home/joaquimdp, shell FISH
  grupos     wheel, video, docker, storage, audio — NÃO está em 'render'
  RAM        30 GB
  disco      /home com 380 GB livres
  GPU        03:00.0 Navi 21 [RX 6800/6800 XT/6900 XT]  <- a discreta
             12:00.0 Raphael  <- iGPU do Ryzen, integrada
  ROCm       AUSENTE
  Docker     29.7.2, joaquimdp já no grupo docker, funciona sem sudo
  Python     3.14.7 no sistema; 3.12.14 já instalado via uv
  uv         0.12.11 em ~/.local/bin

RESTRIÇÕES:
- Rede interna. Nenhum conteúdo sai da rede.
- Qdrant NUNCA exposto na rede: bind em 127.0.0.1 apenas.
- Nada instalado no Python do sistema.
- Acesso ao Jira e ao Confluence é SOMENTE LEITURA, sempre.

───────────────────────────────────────────────────────────
O QUE JÁ ESTÁ PRONTO E VERIFICADO EM ~/Documentos/rag
───────────────────────────────────────────────────────────

O diretório de trabalho é /home/joaquimdp/Documentos/rag. Ele já contém:

  .venv/                  Python 3.12.14, criado com uv, com todas as
                          dependências da Fase 1 instaladas
  data/documents.sqlite3  33.592 documentos já extraídos (8.662 Confluence
                          + 24.930 Jira). sha256 conferido contra a origem:
                          c7a0ef25ae851da20680fd5b68daf4fd9f55e238280645e54623b59e19c49e10
  models/fastembed/       artefato BM25 pré-cacheado (runtime é offline)
  .env                    credenciais, chmod 600
  o repositório           clonado e atualizado

E já foi validado nesta máquina:

  docker compose up -d        Qdrant 1.19.1 no ar, LISTEN só em 127.0.0.1
  indexer.sync index          148.085 pontos reconstruídos em 2m27s
  indexer.sync index (2x)     processa zero — idempotente
  pytest tests/ -q            72 testes passando
  buscas de aceite            acento indiferente, filtros por source e
                              space_key funcionando, URL em todo resultado

PORTANTO: A PARTE B (MIGRAÇÃO) ESTÁ CONCLUÍDA. Não reextraia nada do
Atlassian e não recrie o venv. Apenas confirme o estado com:

    cd ~/Documentos/rag && export PYTHONPATH=$HOME/Documentos/rag
    docker compose up -d
    .venv/bin/python -m indexer.sync status
    .venv/bin/python -m pytest tests/ -q

Você deve ver 33.592 documentos, 0 pendentes, 148.085 pontos e 72 testes
verdes. Se vir isso, vá direto para a PARTE A.

───────────────────────────────────────────────────────────
QUATRO ARMADILHAS DESTA MÁQUINA, JÁ MAPEADAS
───────────────────────────────────────────────────────────

  a) O PyTorch NÃO tem wheel para Python 3.14 em nenhuma variante. Por isso
     o venv já está em 3.12.14, instalado com `uv python install 3.12` sem
     tocar no Python do sistema. Use ESTE venv; não crie outro.

  b) joaquimdp precisa entrar no grupo 'render' para o ROCm acessar
     /dev/kfd. Ele já está em 'video', que sozinho não basta. Isso exige
     sudo e relogin para valer.

  c) São DUAS GPUs. O ROCm vai enumerar as duas e o device 0 pode ser a
     iGPU Raphael, não a Navi 21. Descubra o índice correto e fixe com
     HIP_VISIBLE_DEVICES ou ROCR_VISIBLE_DEVICES. Não presuma device 0.

  d) O shell é fish: `export VAR=valor` não funciona. Variável persistente
     é `set -Ux HSA_OVERRIDE_GFX_VERSION 10.3.0`. Para o usuário de serviço
     e para o cron prefira um arquivo de ambiente lido explicitamente, que
     não depende de shell interativo. Ao rodar scripts bash por SSH, use
     `ssh host bash -s < script`, porque fish não entende sintaxe bash.

───────────────────────────────────────────────────────────
DOIS DEFEITOS JÁ CORRIGIDOS QUE EXPLICAM DECISÕES DO CÓDIGO
───────────────────────────────────────────────────────────

Ambos apareceram justamente nesta migração. Não os reintroduza.

  1. indexed_hash mora no document store mas descreve o estado do Qdrant.
     Ao trazer o store para cá, ele afirmava "tudo indexado" com a coleção
     vazia, e a indexação processou 5 fatias em vez de 148.085. Hoje o
     código detecta coleção vazia com store cheio e remarca tudo sozinho.

  2. Uma falha transitória parava a indexação INTEIRA em silêncio, com
     código de saída 0. O iter_pending_index reconsultava o topo da fila e
     abortava quando o último do lote seguia pendente. Agora pagina por
     doc_id crescente, então o cursor avança mesmo quando um documento
     falha, e delete/upsert têm retry com backoff.

     O motivo do retry importa para a Fase 2: esta máquina é rápida o
     bastante para que o pool HTTP do cliente pegue conexões que o Qdrant
     já fechou por keep-alive, o que chega como "connection reset by peer".
     Aconteceu 3 vezes em 67 mil requisições. O comando `embed` vai fazer
     um volume parecido — trate a mesma classe de erro lá.

═══════════════════════════════════════════════════════════
PARTE A — INFRAESTRUTURA
═══════════════════════════════════════════════════════════

A.1 ROCm
  - Pacotes necessários, grupos de usuário (render, video).
  - gfx1030 está em zona cinzenta de suporte oficial: a AMD foca as RDNA3.
    Costuma funcionar, mas frequentemente exige HSA_OVERRIDE_GFX_VERSION=10.3.0.
    VERIFIQUE se é necessário aqui e, se for, deixe persistente para o usuário
    de serviço.
  - Meu conhecimento de versões do ecossistema ROCm pode estar desatualizado.
    Verifique na máquina e nos repositórios oficiais; não confie em número fixo.

A.2 Python 3.12 em venv isolado.
    O código-fonte é escrito para 3.12+ e não usa sintaxe exclusiva de 3.13.

A.3 PyTorch com build ROCm — não a CUDA, não a CPU-only.
    Verifique a compatibilidade REAL entre o ROCm desta distribuição e as
    builds de PyTorch existentes. Me diga o que encontrou antes de instalar.

A.4 Docker + docker-compose com Qdrant, volume persistente, porta em
    127.0.0.1 apenas. O repositório já traz um docker-compose.yml pronto,
    fixado em qdrant/qdrant:v1.19.1 — use o dele em vez de escrever outro.
    O Qdrant não usa GPU: só CPU e disco.

A.5 Download e cache local do intfloat/multilingual-e5-large, com TESTE DE
    SANIDADE REAL — quero ver funcionando, não só instalado:

      ATENÇÃO, ARMADILHA: o e5 foi treinado com prefixos de instrução. Sem
      eles a qualidade de recuperação cai de forma significativa. Consulta
      leva "query: " e documento leva "passage: ".

    Gere e me mostre a similaridade de cosseno de:
      a) "query: o certificado venceu"   x  "passage: o certificado expirou"
      b) "query: o certificado venceu"   x  "passage: o pedido foi faturado"
      c) as MESMAS frases SEM os prefixos, para eu ver a diferença que fazem
    Rode na GPU e na CPU e me mostre o tempo de cada um.
    Os embeddings devem ser normalizados em L2 (a coleção usa distância
    de cosseno).

A.6 Usuário de serviço sem privilégio para os jobs, diretório de aplicação
    com permissão adequada.

A.7 Rotação de log e esqueleto de crontab COMENTADO. Os jobs reais são estes,
    já validados na outra máquina — deixe-os comentados até a Fase 2 fechar:

      */15 * * * *  ... indexer.sync run --only jira
      0 2 * * *     ... indexer.sync run --only confluence --no-count-attachments
      30 3 * * 0    ... indexer.sync reconcile --only jira

A.8 SETUP.md documentando TUDO que você instalou, com versões exatas, para
    eu conseguir reproduzir esta máquina. Inclua obrigatoriamente
    torch.__version__ e torch.version.hip — sem isso, reproduzir daqui a seis
    meses vira arqueologia.

DUAS PERGUNTAS PARA RESPONDER COM MEDIÇÃO, NÃO COM ESTIMATIVA:

  - Quanto de VRAM o e5-large consome de fato em fp16 nesta GPU e qual batch
    size ela suporta? Meça com sequência de 512 tokens (o pior caso), não com
    a mediana, senão o batch parece maior do que aguenta. São 148 mil fatias
    na carga inicial; batch mal dimensionado custa horas.

  - Com a VRAM que sobrar dos 16 GB, o que mais caberia rodando junto? Estou
    avaliando um reranker cross-encoder e, depois, um LLM local quantizado.
    Cabe simultaneamente ou vou precisar carregar e descarregar? Considere que
    o suporte de software para LLM em RDNA2 é mais limitado que em NVIDIA — me
    diga o que é realista, não o que é teoricamente possível.

═══════════════════════════════════════════════════════════
PARTE B — MIGRAÇÃO: JÁ CONCLUÍDA
═══════════════════════════════════════════════════════════

Feita remotamente antes desta sessão. Veja a seção "O QUE JÁ ESTÁ PRONTO"
acima e apenas rode os quatro comandos de confirmação listados lá.

Só siga para a PARTE C depois que a PARTE A estiver fechada.

═══════════════════════════════════════════════════════════
PARTE C — FASE 2: BUSCA HÍBRIDA
═══════════════════════════════════════════════════════════

Objetivo: popular o vetor denso e fundir com o BM25.

C.0 NÃO RECRIE A COLEÇÃO. Ela já existe com o schema correto:
      dense  -> 1024 dimensões, distância Cosine, DECLARADO E VAZIO
      bm25   -> sparse com modifier=IDF, POPULADO com 148.085 pontos
    O schema foi criado assim na Fase 1 justamente para a Fase 2 não exigir
    migração. Você vai fazer UPDATE do vetor denso nos pontos existentes.
    Se recriar a coleção, joga fora as 148 mil fatias já indexadas.

C.1 Novo módulo indexer/embeddings.py:
    - carrega intfloat/multilingual-e5-large via sentence-transformers;
    - EMBED_DEVICE trocável entre "cpu" e "cuda" por variável de ambiente,
      sem mudar código (o PyTorch ROCm usa a mesma API "cuda");
    - EMBED_BATCH_SIZE configurável, com o valor que você mediu em A;
    - offline por padrão, como o BM25 já é: se o modelo não estiver no cache,
      FALHA com mensagem dizendo qual artefato falta e como pré-cacheá-lo.
      Nunca baixar em silêncio;
    - aplica "passage: " ao indexar e "query: " ao consultar;
    - normaliza L2.

    ARMADILHA IMPORTANTE: indexer/index.py tem uma função fold_accents() que
    remove diacríticos. Ela existe porque o tokenizer do BM25 não normaliza
    acento. NÃO a aplique no vetor denso — para um transformer multilingual o
    acento carrega significado e removê-lo piora o resultado. O denso consome
    o texto ORIGINAL, que está intacto no payload de cada ponto.

C.2 Novo comando de CLI:  python -m indexer.sync embed
    - lê do document store, refatia com o mesmo indexer/chunking.py (os IDs
      são uuid5 determinísticos, então casam com os pontos que já existem);
    - faz update do vetor denso em lote;
    - salva progresso de forma incremental, como o resto do sistema faz: uma
      queda no meio não pode fazer a próxima rodada começar do zero;
    - flags --device e --batch-size sobrepondo o ambiente;
    - reporta métricas ao final: fatias por segundo, tempo total, device usado.
    O comando `run` passa a fazer extract -> index -> embed.

C.3 Busca híbrida em indexer/index.py:
    - fusão RRF (Reciprocal Rank Fusion) entre o ranking BM25 e o denso;
    - o Qdrant faz isso nativamente com a Query API (prefetch + FusionQuery);
      use o recurso do servidor em vez de fundir no cliente;
    - MANTENHA O BM25. Ele é o que acerta identificador exato — VENDAS-14993,
      PRUPSYNCPRODUTOS, ERR-4012 — e é justamente onde o denso é ruim. O denso
      resolve sinônimo, onde o BM25 é ruim. É por isso que é híbrido, e não
      substituição;
    - parâmetro para escolher o modo (bm25 | dense | hybrid), com hybrid
      como padrão, para permitir comparação A/B.

C.4 mcp_server/server.py: a ferramenta search_knowledge_base passa a usar o
    modo híbrido. As docstrings são o contrato de roteamento que o modelo lê
    para escolher a ferramenta — mantenha os blocos "USE QUANDO" e "NÃO USE
    QUANDO", e atualize o texto para refletir que agora ela também encontra
    por significado, não só por palavra.

C.5 Testes em tests/test_embeddings.py, sem depender de GPU nem de rede:
    mocke o encoder e cubra os prefixos query/passage, a normalização, a
    ausência de folding de acento no denso e o determinismo dos IDs.

═══════════════════════════════════════════════════════════
CRITÉRIOS DE ACEITE DA FASE 2
═══════════════════════════════════════════════════════════

Estes são os pares reais que HOJE FALHAM. Depois da Fase 2 os dois lados de
cada par têm que trazer o mesmo documento:

  "boleto bancário do pedido"      x  "cobrança bancária da ordem de compra"
  "erro ao sincronizar pedido"     x  "falha no envio do pedido"
  "aplicativo não gera o app"      x  "smartphone nao compila o pacote"

E, obrigatoriamente, SEM REGRESSÃO no que já funciona:

  - identificador exato continua acertando: busque VENDAS-14993 e
    PRUPSYNCPRODUTOS e confirme que o documento certo vem em primeiro lugar.
    Se o híbrido piorar isso, o peso da fusão está errado;
  - busca com e sem acento continua dando o mesmo resultado;
  - segunda execução consecutiva de `embed` processa quase nada;
  - queda no meio do `embed` não perde progresso;
  - os 72 testes da Fase 1 continuam passando;
  - escopo vazio continua abortando a execução;
  - todo resultado continua trazendo URL clicável.

Me mostre uma comparação lado a lado — bm25 puro x denso puro x híbrido — nas
mesmas consultas. Quero ver o ganho e o custo, não só ouvir que funcionou.

═══════════════════════════════════════════════════════════
PARTE D — TRANSPORTE DO MCP (decisão já tomada, só implemente)
═══════════════════════════════════════════════════════════

Vou usar clientes de IA a partir da minha estação de trabalho, não desta
máquina. A decisão é: manter o transporte stdio e alcançá-lo por SSH.

  No cliente, o comando vira:
    ssh joaquimdp@cachyos-x8664 "cd ~/Documentos/rag && PYTHONPATH=$HOME/Documentos/rag .venv/bin/python -m mcp_server.server"

O acesso à máquina é por Tailscale SSH. A ACL da tailnet precisa liberar o
usuário joaquimdp com "action": "accept" — com "check" o Tailscale exige
autenticação pelo navegador a cada sessão, e o cliente de IA não tem como
abrir navegador.

Motivo: o Qdrant continua em 127.0.0.1, não sobe serviço de rede novo, e a
autenticação é a chave SSH que já administro. Não exponha o MCP por HTTP nem
o Qdrant na rede. Documente essa configuração no SETUP.md.

═══════════════════════════════════════════════════════════
REGRAS DE CÓDIGO (as mesmas da Fase 1 — o repositório já as segue)
═══════════════════════════════════════════════════════════

- Type hints em tudo. Python 3.12+.
- Comentário só onde explica um POR QUÊ não óbvio: uma limitação de versão,
  o motivo de uma decisão de arquitetura. Não comente o que o código já diz.
- Nada de except genérico engolindo erro em silêncio. Se uma fatia falha,
  logue o id e siga. Se a autenticação falha, aborte.
- Logging estruturado em JSON, na STDERR — nunca na stdout, que é por onde o
  MCP fala JSON-RPC.
- NÃO INVENTE endpoint, parâmetro ou API. Se tiver dúvida sobre o que o ROCm,
  o sentence-transformers ou a Query API do Qdrant expõem, verifique na
  máquina ou na documentação oficial, ou ME PERGUNTE. Não assuma.
- Leia o README.md do repositório antes de escrever qualquer código: ele
  documenta as sete armadilhas do Confluence 4.2.4, as decisões de
  arquitetura e os números medidos.

═══════════════════════════════════════════════════════════
COMO VAMOS TRABALHAR
═══════════════════════════════════════════════════════════

Uma etapa por vez, esperando meu OK entre elas, nesta ordem:

  1. confirmar o estado atual (os quatro comandos da Parte B) + plano da
     Parte A para aprovação
  2. Parte A (infraestrutura), terminando com o teste de sanidade do e5
  3. Parte C (Fase 2), na ordem C.1 -> C.5
  5. critérios de aceite, com a comparação lado a lado
  6. SETUP.md

AGORA: rode os quatro comandos de confirmação da Parte B e me mostre o
resultado, junto com o plano da Parte A. Não instale nada ainda.
