# SG Fibra Técnico

Esta cópia é preparada para a operação da SG Fibra, usando o SGP configurado
em `SGP_BASE_URL`. O módulo VOIP da EBF foi removido desta edição; nenhuma
credencial da Agil Telecom deve ser configurada neste projeto.

Versão reduzida do painel, integrada ao SGP somente para:

- saldo de materiais do técnico e da equipe;
- pontuação diária, semanal e mensal;
- consulta de contratos e conferência de CTO.

Dentro desses módulos, o superadmin também pode:

- transferir materiais de um estoque de origem para o saldo escolhido;
- selecionar ordens repetidas e estornar a pontuação perdida.

Essas operações são manuais, possuem confirmação e não criam consultas
periódicas.

O login e todas as rotas privadas são exclusivos para usuários cadastrados
localmente com a função `superadmin`. Sessões antigas de técnico ou
administrador são invalidadas automaticamente.

## Regra de consumo da API

O sistema não possui sincronização do SGP em segundo plano, prefetch, polling,
notificações de rota ou atualização automática. No Render, um keepalive leve
consulta somente o próprio `/healthz` a cada 10 minutos; ele não acessa o SGP
nem o banco. Defina `RENDER_KEEPALIVE=0` para desativá-lo.

Uma consulta ao SGP só acontece após uma ação explícita:

- entrar;
- abrir ou atualizar o saldo;
- abrir ou atualizar a pontuação;
- clicar em **Analisar contratos**;
- clicar em **Consultar PON agora**.

O botão **Bater PON por 8 min** é a única exceção controlada: após confirmação
do superadmin, registra as ONUs offline e faz no máximo uma leitura por minuto
durante oito minutos. Quando o técnico conecta um cabo, a ONU que sai da lista
de offline identifica o cliente e o contrato. A observação é cancelada
imediatamente ao sair da tela.

A tela inicial não executa consultas. A análise de atenuação só começa após o
superadmin informar um contrato e clicar no botão; o diagnóstico compara apenas
clientes com a mesma etiqueta de CTO no mesmo slot e PON.

Os resultados de saldo e pontuação usam Valkey como cache para reduzir chamadas
repetidas. A conferência de CTO é feita uma vez por clique.

## Endereço e manutenção

Endereço principal:
`https://ebf-tecnico-gateway.testefoxfibra.workers.dev`

Enquanto `SYSTEM_DISABLED=1`, Cloudflare e FastAPI respondem em modo de
manutenção e nenhuma rota do SGP é executada.

Controles de emergência:

- `LIGAR SISTEMA PUBLICO.bat`: publica `SYSTEM_DISABLED=0` no gateway;
- `DESLIGAR SISTEMA PUBLICO.bat`: publica `SYSTEM_DISABLED=1` e bloqueia o
  endereço usado pelos celulares;
- `INICIAR SOMENTE LOCAL.bat`: executa apenas em `127.0.0.1`, sem túnel.

## Iniciar no computador

Execute `INICIAR SOMENTE LOCAL.bat`. Ele:

- libera o sistema apenas no processo local;
- escuta exclusivamente em `127.0.0.1:8000`;
- não inicia ngrok, Cloudflare ou qualquer túnel;
- abre `http://127.0.0.1:8000` no navegador;
- encerra o servidor quando a janela é fechada ou ao pressionar `CTRL+C`.

Não use `INICIAR SERVIDOR HIBRIDO.bat` durante o diagnóstico da API.

## Render

- Build: `pip install -r requirements.txt`
- Start:
  `uvicorn app_fastapi.main:app --host 0.0.0.0 --port $PORT --workers 2 --proxy-headers`
- Health check: `/healthz`

O `/healthz` apresenta `background_sync: false` para confirmar que não existe
processo periódico consultando o SGP, e `render_keepalive` informa se o ping
leve do deploy está ativo.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_fastapi.FastApiHttpTests -v
node --check static\essential.js
.\.venv\Scripts\python.exe -m py_compile app_fastapi\main.py
```

## Segurança

- credenciais do SGP permanecem criptografadas na sessão;
- o navegador recebe cookie `HttpOnly`;
- segredos não são armazenados no GitHub;
- respostas de negócio ficam em cache por tempo controlado;
- o aplicativo cancela a consulta anterior quando o usuário muda rapidamente
  para outra tela.
