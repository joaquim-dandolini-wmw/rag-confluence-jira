# Como usar a busca no seu cliente de IA

Guia para conectar o servidor MCP `atlassian-kb` no Claude e em outros clientes.
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

## O endereço

```
http://10.2.1.132:8765/mcp
```

É só isso. **Não precisa de chave, senha, token nem instalar nada.** Basta estar
na rede da empresa.

Teste antes de configurar o cliente:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://10.2.1.132:8765/mcp
```

Qualquer resposta HTTP (inclusive `400` ou `406`) significa que você alcança o
servidor — o erro é esperado, porque o `curl` não fala MCP. Se der
`Connection refused` ou travar, veja a seção de problemas no fim.

---

## Claude Code (linha de comando)

Um comando:

```bash
claude mcp add --transport http atlassian-kb http://10.2.1.132:8765/mcp
```

Confira:

```bash
claude mcp list
# atlassian-kb: http://10.2.1.132:8765/mcp (HTTP) - ✔ Connected
```

Isso instala **só no projeto onde você rodou o comando**. Para ter a busca em
qualquer pasta, use `-s user`:

```bash
claude mcp add -s user --transport http atlassian-kb http://10.2.1.132:8765/mcp
```

Para remover: `claude mcp remove atlassian-kb`.

## Claude Desktop (aplicativo)

Abra **Settings → Developer → Edit Config**, que abre o
`claude_desktop_config.json`. Se preferir editar na mão:

```
Windows   %APPDATA%\Claude\claude_desktop_config.json
macOS     ~/Library/Application Support/Claude/claude_desktop_config.json
```

Acrescente o bloco `atlassian-kb` dentro de `mcpServers`:

```json
{
  "mcpServers": {
    "atlassian-kb": {
      "type": "http",
      "url": "http://10.2.1.132:8765/mcp"
    }
  }
}
```

Se já existirem outros servidores, só acrescente a chave `"atlassian-kb"` ao
lado deles — não substitua o `mcpServers` inteiro. Reinicie o aplicativo.

## Outros clientes

Qualquer cliente que aceite MCP por **HTTP** funciona: aponte para a URL. Alguns
chamam esse transporte de *streamable HTTP*, outros só de *HTTP*.

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

- **velocidade:** a primeira pergunta depois de o servidor subir leva ~6 s,
  porque ele carrega os modelos na GPU. Depois disso fica em **~260 ms** em
  prosa e **~5 ms** em identificador, para todo mundo — os modelos ficam
  carregados no serviço;
- **contagem, listagem e filtro por status não saem da busca.** O índice é
  atualizado por cron (Jira a cada 15 min, Confluence de madrugada), então tem
  atraso. Para "quantos bugs abertos" o cliente usa `search_jira_jql`, que fala
  com o Jira ao vivo. Isso já está no contrato das ferramentas; você não precisa
  escolher.

Todo resultado traz a URL de origem — página do Confluence ou issue em
`/browse/`. Peça para o modelo citar o link.

---

## Quando não funcionar

| sintoma | causa provável e o que fazer |
|---|---|
| `Connection refused` | o serviço caiu. No servidor: `systemctl status rag-mcp` |
| a conexão trava sem responder | você está fora da rede interna, ou o IP mudou. Confirme com `ping 10.2.1.132` |
| erro de *Host* não permitido | o IP da máquina mudou. Veja "quando o IP muda" |
| `Connected` mas a busca dá erro | o Qdrant não está no ar. No servidor: `docker compose up -d` |
| a primeira busca estoura o tempo | é a carga dos modelos. Repita, ou aumente o timeout do cliente |

Diagnóstico no servidor, do mais rápido ao mais completo:

```bash
systemctl status rag-mcp
journalctl -u rag-mcp -n 30

cd ~/Documentos/rag
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync status
PYTHONPATH=$PWD .venv/bin/python -m indexer.sync search "boleto do pedido"
```

O `status` tem que mostrar `pendentes idx: 0`, `pendentes denso: 0` e o mesmo
número em `pontos` e `pontos c/ denso`.

### Quando o IP muda

O `10.2.1.132` vem de DHCP. Se a máquina reiniciar ou trocar de rede, **todos os
clientes param juntos** e é preciso reconfigurar cada um. Duas providências:

- reservar o IP no DHCP, ou usar um nome que o DNS interno resolva;
- a máquina precisa estar com o **cabo de rede conectado**. Só no Wi-Fi ela cai
  em outro endereço, que ninguém da empresa alcança.

O servidor descobre o próprio IP ao subir e só aceita requisições endereçadas a
ele — é a proteção contra DNS rebinding do SDK. Depois de trocar de IP, reinicie
o serviço para ele reaprender:

```bash
sudo systemctl restart rag-mcp
```

---

## Segurança — leia antes de divulgar o endereço

O acesso hoje é **aberto na rede interna, sem autenticação**. Foi uma decisão
consciente para o piloto, e tem consequências que precisam estar claras:

- **quem alcança a porta lê tudo que está indexado**: 8.662 páginas do
  Confluence e 24.930 issues do Jira, dos espaços e projetos listados no `.env`;
- **as permissões por espaço do Confluence NÃO são aplicadas.** O índice foi
  construído por um usuário de serviço, e a busca devolve o que ele via. Se
  algum espaço indexado tem conteúdo restrito, ele fica legível por qualquer um
  na rede;
- **não há registro de quem perguntou o quê.** Sem autenticação, as consultas
  são anônimas;
- em compensação, o acesso é **somente leitura** — nenhuma ferramenta escreve no
  Jira ou no Confluence — e o Qdrant continua em `127.0.0.1`, fora da rede.

O que limita o alcance hoje:

```
ufw: 8765/tcp  ALLOW  10.0.0.0/8         (rede da empresa)
     8765/tcp  ALLOW  192.168.10.0/24    (Wi-Fi local)
     tudo o mais: negado
```

Não está exposto à internet — mas está exposto a **toda** a rede interna, o que
inclui qualquer dispositivo nela.

### Se quiser fechar depois

Três caminhos, do mais simples ao mais restritivo:

1. **estreitar o firewall** para a faixa de quem realmente usa, em vez de
   `10.0.0.0/8` inteiro;
2. **exigir um token**: o SDK aceita `token_verifier` no servidor e os clientes
   mandam `Authorization: Bearer ...`. Simples, mas é segredo compartilhado —
   sem revogação individual;
3. **voltar para stdio por SSH**, com uma chave por pessoa e revogação
   individual. O `deploy/mcp-stdio.sh` já existe para isso: é um *forced command*
   que tranca a chave no servidor MCP — sem shell, sem túnel e sem leitura do
   `.env`, testado. Bem mais seguro, e configuração por pessoa.

Para desligar tudo agora:

```bash
sudo systemctl disable --now rag-mcp
sudo ufw delete allow from 10.0.0.0/8 to any port 8765 proto tcp
```
