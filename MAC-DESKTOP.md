# Claude Desktop no Mac — 5 minutos

Passo a passo curto e testado. Se algo falhar, a seção final diz o porquê.

**Pré-requisito:** estar na rede da empresa. Confira antes, tem que responder `400`:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://10.2.1.132:8765/mcp
```

`000` ou travar = você não alcança a máquina. Pare aqui, é rede.

---

## 1. Instale o Node

```bash
node --version || brew install node
```

## 2. FECHE o Claude por completo

**Isto não é opcional e é o passo que todo mundo erra.** O aplicativo reescreve
o `claude_desktop_config.json` quando sai, apagando o que você editou. Se editar
com ele aberto, seu trabalho é descartado.

```bash
osascript -e 'quit app "Claude"'
sleep 3
pgrep -fl Claude    # não pode sobrar nada
```

## 3. Escreva a configuração

Copie o bloco inteiro de uma vez:

```bash
cd ~/Library/Application\ Support/Claude
cp claude_desktop_config.json claude_desktop_config.json.bak 2>/dev/null

python3 - <<'PY'
import json, pathlib
p = pathlib.Path("claude_desktop_config.json")
try:
    cfg = json.loads(p.read_text())
except Exception:
    cfg = {}
cfg["mcpServers"] = {
    "atlassian-kb": {
        "command": "npx",
        "args": ["-y", "mcp-remote", "http://10.2.1.132:8765/mcp", "--allow-http"],
    }
}
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
print("ok, chaves:", list(cfg))
PY
```

Isso preserva as preferências que já existiam e acrescenta o `mcpServers`.

**Não abra esse arquivo no TextEdit.** Ele troca `"` por aspas curvas e quebra o
JSON — e o Claude ignora o arquivo inteiro em silêncio quando isso acontece.

## 4. Abra o Claude

```bash
open -a Claude
```

Espere uns 20 segundos na primeira vez (ele baixa a ponte com o `npx`).

## 5. Confira

No ícone **+** ao lado da caixa de mensagem → **Connectors**. Tem que aparecer
`atlassian-kb` com quatro ferramentas.

Pergunte alguma coisa para testar:

> Por que o boleto não é gerado em alguns pedidos? Cite os links.

---

## Se não aparecer

O log diz o motivo em uma linha:

```bash
cat ~/Library/Logs/Claude/mcp-server-atlassian-kb.log
```

| o que aconteceu | causa | solução |
|---|---|---|
| o arquivo de log **não existe** | o Claude não leu a configuração | o JSON está inválido, ou você editou com o app aberto e ele sobrescreveu. Refaça do passo 2 |
| `npx: command not found` | falta o Node | `brew install node` |
| erro de conexão | rede | o `curl` do topo tem que dar `400` |
| `Expected property name...` | JSON quebrado | quase sempre TextEdit. Refaça o passo 3 |

Para conferir se o JSON está válido:

```bash
python3 -m json.tool ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

---

## Duas coisas que NÃO funcionam

**Connectors → Add custom connector.** A conexão é feita pelos servidores da
Anthropic, não pelo seu Mac, então eles não alcançam um IP privado como
`10.2.1.132`. Dá o erro "Não foi possível alcançar". Não insista, e não adianta
usar `https`: o problema não é o protocolo.

**O atalho do Chrome.** Se o que abre é
`~/Applications/Chrome Apps.localized/Claude.app`, isso é uma janela de
navegador para o claude.ai, não o aplicativo — não lê configuração nenhuma.
Confira com:

```bash
ls -d /Applications/Claude.app 2>/dev/null || echo "baixe em claude.ai/download"
```

---

## No terminal é mais simples

Se você usa **Claude Code**, esqueça tudo acima. É um comando, sem Node, sem
arquivo:

```bash
claude mcp add -s user --transport http atlassian-kb http://10.2.1.132:8765/mcp
claude mcp list      # atlassian-kb: ... - ✔ Connected
```
