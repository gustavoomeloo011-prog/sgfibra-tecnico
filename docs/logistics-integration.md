# Integração de logística — setembro de 2026

## Fluxo publicado

- Estoque → saldo / movimentos / materiais usados / devoluções → divergências.
- Técnico: abre uma divergência e acompanha ou complementa a resposta. Não modifica o saldo por esse fluxo.
- Supervisor: consulta todas as divergências, responde, resolve ou reabre. O escritório permanece sem acesso.
- Divergências verificáveis da conciliação e materiais ausentes em instalações geram pendências deduplicadas. Resolver não altera automaticamente o SGP.
- Histórico registra ator, horário e dados de solicitações/resultados de compras, transferências, lançamentos de materiais, correções e confirmações de recebimento. Dados e mensagens são criptografados pelo mecanismo existente.
- Histórico e conversas ficam em tabelas próprias, sem a expiração de quatro dias do cache. Não há exclusão automática desses registros.

## Regras e confiabilidade

- Classificação única de conectores, somente pelo tipo da OS: corretiva ou mudança no mesmo lugar = devolver; instalação de kit ou outra mudança = dispensado; demais = conferir.
- Instalação de kit sem lançamento de modem ou conector gera revisão, não cobrança automática. Exceções precisam ser analisadas.
- Abertura inferida do saldo atual não comprova igualdade: esperado e diferença ficam vazios. Uma referência anterior independente permite a conferência.
- Leitura com movimento concorrente não recebe selo de saldo conferido nem serve de referência futura.
- Versão global de cache evita reutilizar consultas anteriores às operações de estoque feitas pelo app, inclusive lotes parcialmente processados.
- Consultas salvas continuam disponíveis como históricas e avisam quando houve alteração pelo app. Alterações externas no SGP exigem nova consulta.
- Falha de cache depois da operação não deve transformar uma operação concluída em erro 500.

## Limites importantes

- O SGP não fornece uma transação atômica conjunta de saldo e movimentos; leituras concorrentes exigem nova consulta.
- O histórico local não cobre ações feitas diretamente no SGP nem recupera retroativamente ações anteriores a esta atualização.
- O registro de uma tentativa não confirma o resultado de uma chamada que sofreu timeout. Conferir no SGP antes de repetir; ainda não existe garantia distribuída de execução única entre o app e o SGP.
- A confirmação de recebimento físico continua sendo responsabilidade do supervisor. Não foi criada baixa automática nem cobrança de técnico.
- Validação automatizada usa dados simulados. Nenhuma compra, transferência ou devolução real foi executada como teste.
