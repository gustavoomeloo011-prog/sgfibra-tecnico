# VOIP AGIL (supervisores)

Configurar no ambiente do backend/Render, nunca em arquivos públicos:

- `AGIL_VOIP_USER`: login da revenda.
- `AGIL_VOIP_PASSWORD`: senha da revenda.
- `AGIL_VOIP_URL`: opcional; padrão HTTPS do portal AGIL /fox/index.php?E=3.

Menu VOIP disponível somente para `superadmin`, com a mesma validação nos três endpoints.
GET /api/voip lê o snapshot PostgreSQL. POST /api/voip/refresh consulta a AGIL,
valida a quantidade de registros e salva somente os metadados. Erros não substituem
o snapshot anterior. A data da consulta é exibida; não existe atualização automática.
POST /api/voip/{number}/password faz uma consulta autenticada e revela somente a
senha solicitada, com no-store, sem persistir senhas SIP. A tela oculta após 30s.

Duplicatas são agrupadas pelo número. Qualquer IP marca em uso. Sem IP, contas
inativas, senhas conflitantes e observações diferentes de vazio/VAGO/SEM USO/
DISPONIVEL/LIVRE ficam em revisão. Não há reserva automática ou atribuição de número.
A lista para clientes inclui somente números candidatos disponíveis e data da consulta.
Confirmar disponibilidade no portal antes de efetivar contratação.

O botão "Vincular contrato" é exclusivo de supervisores. Ele aceita o ID do
contrato (ou o ID antigo do serviço de internet), preserva as TAGS existentes,
adiciona VOIP e a observação SIP no serviço de internet, cria o serviço de
telefonia com contrato INTERNET + VOIP e plano Telefone_fixo, e registra o ID do
contrato na observação da AGIL. O fluxo é idempotente para não criar o mesmo
serviço de telefonia duas vezes.
O bloqueio concorrente atual é por processo (adequado ao serviço de um worker).
Ao escalar para múltiplos workers, usar bloqueio distribuído antes de ampliar consultas.
