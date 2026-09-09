#!/usr/bin/env bash
# Ponto de entrada UNICO das chaves de cliente MCP.
#
# Usado como forced command no ~/.ssh/authorized_keys. O SSH exporta o que o
# cliente pediu em SSH_ORIGINAL_COMMAND e este script IGNORA aquilo de
# proposito: a chave nao serve para rodar comando arbitrario, so para falar
# JSON-RPC com o servidor MCP pela stdin/stdout.
#
# Sem isto, uma chave em authorized_keys da shell completo como joaquimdp, o
# que inclui ler o .env com o PAT do Jira e a senha do Confluence.
set -euo pipefail

RAG_HOME=/home/joaquimdp/Documentos/rag
cd "$RAG_HOME"
export PYTHONPATH="$RAG_HOME"

# stdout e reservada ao JSON-RPC. Todo log vai para stderr, que o SSH entrega
# separado e nao corrompe o transporte.
exec .venv/bin/python -m mcp_server.server
