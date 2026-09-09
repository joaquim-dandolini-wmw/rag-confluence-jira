# Como usar a busca no seu cliente de IA

Guia para instalar o servidor MCP `atlassian-kb` no Claude e em outros clientes.
Todos os comandos daqui foram **executados e validados** nesta configuração.

O que você ganha: quatro ferramentas para consultar o Jira e o Confluence
internos direto do chat, com link clicável em todo resultado.

| ferramenta | para quê |
|---|---|
| `search_knowledge_base` | achar conteúdo por assunto ou por significado |
| `search_jira_jql` | contagem, listagem e filtro por status, ao vivo |
| `get_jira_issue` | estado atual de uma issue |
| `get_confluence_page` | texto completo de uma página |

---

## Antes de começar

| requisito | valor |
|---|---|
| servidor, rede da empresa | **`10.2.1.132`** — usuário `joaquimdp` |
| porta | 22/tcp, liberada para `10.0.0.0/8` (empresa) e `192.168.10.0/24` |
| autenticação | **chave SSH** — senha não serve, o cliente de IA não tem como digitar |

Você precisa de um cliente SSH. No **Windows 10/11 e no macOS já vem instalado**;
no Linux é o pacote `openssh`.

Teste primeiro se você alcança a máquina:

```bash
ping 10.2.1.132
```

> **O IP é DHCP.** Se a máquina reiniciar ou trocar de rede, ele muda e todos os
> clientes param de conectar. Reserve o IP no DHCP, ou use o nome
> `cachyos-x8664` se o DNS interno resolver.
>
> A máquina precisa estar com o **cabo de rede conectado**. Só no Wi-Fi ela cai
> em `10.2.1.132`, que o pessoal da empresa não alcança.

---

## Passo 1 — criar sua chave SSH

Na **sua** máquina, não no servidor. Uma vez só, para sempre.

**Windows (PowerShell)** · **macOS** · **Linux** — o comando é o mesmo:

```bash
ssh-keygen -t ed25519 -C "seu.nome@wmw.com.br"
```

Aceite o caminho padrão apertando Enter. A senha (passphrase) pode ficar vazia —
se você puser uma, o cliente de IA vai travar pedindo a senha a cada conexão, a
não ser que você use um agente de chaves.

Isso cria dois arquivos. O `.pub` é público e pode ser enviado por chat; o outro
é a sua chave privada e **nunca sai da sua máquina**:

```
~/.ssh/id_ed25519.pub     <- este você envia
~/.ssh/id_ed25519         <- este NUNCA
```

## Passo 2 — autorizar sua chave no servidor

**Linux e macOS:**

```bash
ssh-copy-id joaquimdp@10.2.1.132
```

Ele vai pedir a senha do `joaquimdp` uma única vez.

**Windows (PowerShell)** — não existe `ssh-copy-id`, use isto:

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh joaquimdp@10.2.1.132 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

**Sem senha em mãos:** mande o conteúdo do seu `.pub` para o administrador da
máquina, que acrescenta em `~/.ssh/authorized_keys`.

## Passo 3 — testar a conexão

```bash
ssh joaquimdp@10.2.1.132 "hostname"
```

Tem que responder `cachyos-x8664` **sem pedir senha**. Se pedir senha, a chave
não foi autorizada — volte ao passo 2. Não siga adiante enquanto isso não passar:
o cliente de IA não tem como responder a um pedido de senha.

## Passo 4 — instalar no cliente

### Claude Code (linha de comando)

Um comando:

```bash
claude mcp add atlassian-kb -- ssh joaquimdp@10.2.1.132 "cd ~/Documentos/rag && PYTHONPATH=/home/joaquimdp/Documentos/rag .venv/bin/python -m mcp_server.server"
```

Confira:

```bash
claude mcp list
# atlassian-kb: ssh joaquimdp@10.2.1.132 ... - ✔ Connected
```

O padrão instala **só no projeto onde você rodou o comando**. Para ter a busca
em qualquer pasta, acrescente `-s user`:

```bash
claude mcp add atlassian-kb -s user -- ssh joaquimdp@10.2.1.132 "cd ~/Documentos/rag && PYTHONPATH=/home/joaquimdp/Documentos/rag .venv/bin/python -m mcp_server.server"
```

Existe também `-s project`, que grava num `.mcp.json` versionado junto do
repositório — útil se o time quiser a configuração no próprio projeto em vez de
cada um instalar na sua máquina.

Para remover: `claude mcp remove atlassian-kb`.

### Claude Desktop (aplicativo)

Abra **Settings → Developer → Edit Config**, que abre o
`claude_desktop_config.json`. Se preferir editar na mão, ele fica em:

```
Windows   %APPDATA%\Claude\claude_desktop_config.json
macOS     ~/Library/Application Support/Claude/claude_desktop_config.json
```

Acrescente o bloco `atlassian-kb` dentro de `mcpServers`:

```json
{
  "mcpServers": {
    "atlassian-kb": {
      "command": "ssh",
      "args": [
        "joaquimdp@10.2.1.132",
        "cd ~/Documentos/rag && PYTHONPATH=/home/joaquimdp/Documentos/rag .venv/bin/python -m mcp_server.server"
      ]
    }
  }
}
```

Se já existirem outros servidores no arquivo, só acrescente a chave
`"atlassian-kb"` ao lado deles — não substitua o `mcpServers` inteiro. Reinicie
o aplicativo depois de salvar.

### Outros clientes

Qualquer cliente que aceite MCP por **stdio** funciona: o padrão é sempre
`command` = `ssh` e o resto como argumentos. Não há servidor HTTP para apontar.

---

## Uma armadilha de aspas que vale conhecer

O comando desta documentação usa o caminho **literal**
`/home/joaquimdp/Documentos/rag`, e não `$HOME/Documentos/rag`, de propósito.

Com aspas duplas, o shell da **sua** máquina substitui `$HOME` antes de mandar a
linha para o servidor:

```bash
# ERRADO se digitado num terminal: o $HOME é o SEU, não o do servidor
ssh joaquimdp@10.2.1.132 "PYTHONPATH=$HOME/Documentos/rag ..."
#   vira PYTHONPATH=/Users/seu.nome/Documentos/rag   -> o servidor não acha nada
```

Como o pessoal usa Windows e macOS, o caminho literal evita o problema em todos
os casos. Se preferir usar `$HOME`, aspas **simples** também resolvem, porque aí
a expansão acontece no servidor.

---

## Como usar bem

A ferramenta `search_knowledge_base` **escolhe sozinha** como buscar, conforme o
que você escreve:

| você escreve | o que acontece |
|---|---|
| `VENDAS-14993`, `PRUPSYNCPRODUTOS` | busca exata, pelo termo literal |
| "por que o boleto não é gerado no pedido" | busca por significado |
| "erro no PRUPSYNCPRODUTOS ao sincronizar" | as duas juntas |

Então **descreva o problema com as suas palavras**. Não é preciso adivinhar o
vocabulário de quem escreveu a página: perguntar por "dados da fatura para
pagamento em banco" encontra a página que fala em "boleto bancário".

Duas coisas que vale saber:

- **a primeira pergunta de cada sessão demora ~6 segundos**, porque o servidor
  carrega os modelos na GPU. As seguintes levam de 0,1 a 0,6 segundo;
- **contagem, listagem e filtro por status não saem da busca.** O índice é
  atualizado por cron (Jira a cada 15 min, Confluence de madrugada), então tem
  atraso. Para "quantos bugs abertos" o cliente vai usar `search_jira_jql`, que
  fala com o Jira ao vivo. Isso já está escrito no contrato das ferramentas; você
  não precisa escolher.

Todo resultado traz a URL de origem — página do Confluence ou issue em
`/browse/`. Peça para o modelo citar o link.

---

## Quando não funcionar

| sintoma | causa provável e o que fazer |
|---|---|
| `Permission denied (publickey)` | a chave não está autorizada. Refaça o passo 2 e confira o passo 3 |
| pede senha | mesma coisa. O cliente de IA não digita senha; a chave é obrigatória |
| `Connection timed out` | o IP mudou, ou o cabo de rede da máquina caiu. Confirme com `ping 10.2.1.132` |
| `Connection refused` | o `sshd` do servidor caiu: `systemctl status sshd` |
| `✔ Connected` mas a busca dá erro | o Qdrant não está no ar: `docker compose up -d` no servidor |
| `Failed to connect` sem detalhe | rode o comando do passo 3 na mão; o erro do SSH aparece ali |
| a primeira busca estoura o tempo | é a carga dos modelos. Aumente o timeout do cliente ou repita |

Diagnóstico no servidor, do mais rápido ao mais completo:

```bash
cd ~/Documentos/rag
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync status
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync search "boleto do pedido"
tail -20 logs/sync-jira.log
```

O `status` tem que mostrar `pendentes idx: 0`, `pendentes denso: 0` e o mesmo
número em `pontos` e `pontos c/ denso`.

---

## Segurança, em uma tela

- o acesso ao Jira e ao Confluence é **somente leitura**, sempre;
- o Qdrant escuta **só em `127.0.0.1`** e não é exposto na rede;
- o MCP **não sobe serviço de rede**: fala por stdio dentro do túnel SSH;
- a porta 22 está liberada apenas para a rede interna (`10.0.0.0/8` e
  `192.168.10.0/24`), nunca para a internet;
- a autenticação é chave SSH, revogável tirando a linha correspondente de
  `~/.ssh/authorized_keys` no servidor;
- o índice contém conteúdo interno dos espaços e projetos configurados. Quem tem
  acesso ao MCP vê tudo que está indexado, **sem** as permissões por espaço do
  Confluence. O escopo do que entra no índice está em `.env`
  (`CONFLUENCE_SPACES` e `JIRA_PROJECTS`) e é deliberadamente explícito.

Para tirar o acesso de alguém, remova a chave dele:

```bash
ssh joaquimdp@10.2.1.132
nano ~/.ssh/authorized_keys     # apague a linha com o comentário dele
```
