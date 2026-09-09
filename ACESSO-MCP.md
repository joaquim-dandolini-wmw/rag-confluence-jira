# Conectar a busca ao seu cliente de IA

Servidor: **`http://10.2.1.132:8765/mcp`** — rede interna, sem senha, sem chave.

Quatro ferramentas no chat: buscar conteúdo no Jira e no Confluence por assunto
ou por significado, consultar JQL ao vivo, abrir uma issue e ler uma página
inteira. Todo resultado traz o link da origem.

---

## Antes de tudo: você alcança o servidor?

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://10.2.1.132:8765/mcp
```

**`400` é a resposta certa** — significa que chegou lá (o `curl` não fala MCP).
Se travar ou der `000`, é rede: você precisa estar na rede da empresa. Nesse
caso pare aqui, configurar o cliente não vai adiantar.

---

## Claude Code (terminal) — o mais simples

Um comando, em qualquer sistema, sem instalar nada:

```bash
claude mcp add -s user --transport http atlassian-kb http://10.2.1.132:8765/mcp
claude mcp list      # atlassian-kb: ... - ✔ Connected
```

Pronto. O resto deste documento é só para quem usa o **aplicativo** Claude Desktop.

---

## Claude Desktop

O aplicativo não conecta direto num servidor da rede interna (ver "O que não
funciona" no fim). Usa-se uma ponte local, que precisa de **Node.js**.

### Passo 1 — Node.js

```bash
node --version
```

Se não existir: **macOS** `brew install node` · **Windows/Linux**
[nodejs.org](https://nodejs.org) (versão LTS).

### Passo 2 — FECHE o Claude por completo

**É o passo que todo mundo erra.** O aplicativo reescreve o arquivo de
configuração quando sai: se você editar com ele aberto, sua alteração é
descartada e nada funciona.

| sistema | como fechar de verdade |
|---|---|
| macOS | `osascript -e 'quit app "Claude"'` — ou `Cmd+Q`, não só fechar a janela |
| Windows | botão direito no ícone da bandeja → *Quit* — fechar a janela não basta |
| Linux | feche a janela e confirme com `pkill -f claude` |

### Passo 3 — escreva a configuração

O arquivo é o `claude_desktop_config.json`:

| sistema | caminho |
|---|---|
| macOS | `~/Library/Application Support/Claude/` |
| Windows | `%APPDATA%\Claude\` |
| Linux | `~/.config/Claude/` |

**macOS e Linux** — este bloco preserva o que já existe e acrescenta o servidor:

```bash
cd ~/Library/Application\ Support/Claude   # no Linux: cd ~/.config/Claude
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("claude_desktop_config.json")
try: cfg = json.loads(p.read_text())
except Exception: cfg = {}
cfg["mcpServers"] = {"atlassian-kb": {
    "command": "npx",
    "args": ["-y", "mcp-remote", "http://10.2.1.132:8765/mcp", "--allow-http"]}}
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
print("ok:", list(cfg))
PY
```

**Windows** (PowerShell):

```powershell
cd $env:APPDATA\Claude
$f = "claude_desktop_config.json"
$cfg = if (Test-Path $f) { Get-Content $f -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$srv = @{ "atlassian-kb" = @{ command = "npx"; args = @("-y","mcp-remote","http://10.2.1.132:8765/mcp","--allow-http") } }
$cfg | Add-Member -Name mcpServers -Value $srv -MemberType NoteProperty -Force
$cfg | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding utf8
Get-Content $f
```

> **Não edite esse arquivo no TextEdit (macOS) nem no Word.** Eles trocam `"` por
> aspas curvas, o JSON quebra e o Claude ignora o arquivo **em silêncio**.
> Se precisar editar à mão, use um editor de código ou `nano`.

### Passo 4 — abra e confira

Abra o Claude, espere ~20 s na primeira vez (ele baixa a ponte). No **+** ao
lado da caixa de mensagem → **Connectors**, tem que aparecer `atlassian-kb`
com quatro ferramentas.

Teste:

> Por que o boleto não é gerado em alguns pedidos? Cite os links.

---

## Como usar bem

A busca **escolhe sozinha** entre exata e semântica, conforme o que você digita:

| você escreve | o que acontece |
|---|---|
| `VENDAS-14993`, `PRUPSYNCPRODUTOS` | busca exata pelo termo |
| "por que o boleto não é gerado" | busca por significado |
| "erro no PRUPSYNCPRODUTOS ao sincronizar" | as duas |

Descreva o problema **com as suas palavras** — não precisa adivinhar o
vocabulário de quem escreveu a página. "Dados da fatura para pagamento em banco"
encontra a página que fala em "boleto bancário".

Contagem e listagem (“quantos bugs abertos”) não saem do índice: o cliente usa a
consulta ao vivo no Jira sozinho, você não precisa escolher.

---

## Quando não funcionar

```bash
# macOS/Linux
cat ~/Library/Logs/Claude/mcp-server-atlassian-kb.log
# Windows
type "%APPDATA%\Claude\logs\mcp-server-atlassian-kb.log"
```

| sintoma | causa | solução |
|---|---|---|
| o log **não existe** | o Claude não leu a configuração | JSON inválido, ou você editou com o app aberto. Refaça do passo 2 |
| `npx: command not found` | falta Node.js | passo 1 |
| `EHOSTUNREACH` / `fetch failed` | rede | o `curl` do topo tem que dar `400`. No Wi-Fi isso oscila |
| `Expected property name...` | JSON quebrado | quase sempre TextEdit. Refaça o passo 3 |
| conectou e parou depois | a ponte não reconecta sozinha | feche e abra o Claude |

Validar o JSON:

```bash
python3 -m json.tool claude_desktop_config.json
```

---

## O que NÃO funciona

**Connectors → Add custom connector.** A conexão parte dos servidores da
Anthropic, não da sua máquina, e eles não alcançam um IP privado como
`10.2.1.132`. Dá "Não foi possível alcançar". Não adianta trocar para `https`:
o problema não é o protocolo.

**O atalho do Chrome para o claude.ai.** Ele se chama "Claude" e parece o
aplicativo, mas é uma janela de navegador e não lê configuração nenhuma.
No macOS, confira com `ls -d /Applications/Claude.app`.

---

## Segurança

O acesso é **aberto na rede interna, sem autenticação**:

- quem alcança a porta lê tudo que está indexado — 8.662 páginas do Confluence
  e 24.930 issues do Jira;
- **as permissões por espaço do Confluence não são aplicadas**: o índice foi
  construído por um usuário de serviço e a busca devolve o que ele via;
- não há registro de quem perguntou o quê;
- em compensação é **somente leitura**, e o firewall só aceita `10.0.0.0/8` e
  `192.168.10.0/24` — nada vindo da internet.

O `10.2.1.132` vem de DHCP: se mudar, **todos os clientes param juntos**. Vale
reservar o IP antes de espalhar o endereço.
