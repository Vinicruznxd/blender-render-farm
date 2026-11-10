# Segurança e implantação

Esta versão destina-se a uma LAN privada com máquinas e usuários confiáveis. O Coordinator aceita operações de gerenciamento sem autenticação e usa HTTP sem TLS. Não publique suas portas na internet.

O Worker pode exigir um token bearer, mas o Master e o Coordinator incluídos não encaminham esse token. Não há autenticação integrada de ponta a ponta, isolamento entre clientes, sandbox de render ou serviço multiusuário hospedado nesta versão.

Um job pode levar o Blender a ler arquivos e gravar saídas com as permissões da conta do Worker. Processe somente cenas confiáveis, use contas com as permissões necessárias e restrinja o acesso à rede e aos compartilhamentos. Evite executar os processos como administrador. Discovery UDP não autentica a identidade das máquinas.

## Informar uma vulnerabilidade

Se a opção **Report a vulnerability** estiver habilitada na aba Security do GitHub, use esse canal privado. Caso contrário, abra uma issue solicitando um canal privado, sem incluir credenciais ou detalhes exploráveis. Não há prazo de resposta ou manutenção garantido.

## Preparar uma publicação

O `.gitignore` exclui configurações reais, logs, SQLite, backups, builds e cenas. Ele não remove arquivos já versionados: confira os arquivos preparados para commit. Dependências e recursos de terceiros conservam suas licenças. Uma revisão de publicação não equivale a uma auditoria completa de segurança.
