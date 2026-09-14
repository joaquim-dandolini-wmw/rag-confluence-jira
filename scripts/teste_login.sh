#!/usr/bin/env bash
# Sobe uma SEGUNDA instância do servidor MCP, com autenticação ligada, numa
# porta separada — para testar o login sem encostar na que está em produção.
#
#   ./scripts/teste_login.sh            # usa o grupo do .env
#   GRUPO=cn=pulse,ou=groups,dc=wmw,dc=com,dc=br ./scripts/teste_login.sh
#
# Ela lê o mesmo .env (Jira, Confluence, Qdrant), e sobrescreve só o que é de
# teste: a porta, o endereço público e o grupo. O Qdrant é o mesmo, e isso não
# é problema: a busca é somente leitura.
#
# O endereço é http://localhost — o OAuth 2.1 abre essa exceção para loopback,
# e é por isso que o acesso ao teste é por túnel SSH e não pela rede.
set -euo pipefail
cd "$(dirname "$0")/.."

PORTA="${PORTA:-8799}"
export MCP_TRANSPORT=streamable-http
export MCP_HOST=127.0.0.1
export MCP_PORT="$PORTA"
export MCP_PATH=/
export MCP_AUTH_ENABLED=1
export MCP_PUBLIC_URL="http://localhost:$PORTA"
export MCP_AUTH_DB=./data/auth-teste.sqlite3
export MCP_ALLOWED_HOSTS="localhost:*,127.0.0.1:*,http://localhost:$PORTA,http://127.0.0.1:$PORTA"
if [ -n "${GRUPO:-}" ]; then
  export LDAP_GROUP_DN="$GRUPO"
fi

echo "instância de teste em http://localhost:$PORTA/"
echo "grupo exigido: ${LDAP_GROUP_DN:-<do .env>}"
echo "banco de tokens: $MCP_AUTH_DB (separado do de produção)"
echo
exec env PYTHONPATH="$PWD" .venv/bin/python -m mcp_server.server
