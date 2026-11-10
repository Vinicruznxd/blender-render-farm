# Como contribuir

Contribuições de documentação, correções e melhorias são bem-vindas. Leia o [licenciamento](docs/LICENSING.md) antes de incorporar código ou enviar um pull request.

1. Abra uma issue descrevendo o problema ou a mudança proposta.
2. Crie um fork e uma branch descritiva.
3. Prepare o ambiente conforme [DEVELOPMENT.md](docs/DEVELOPMENT.md).
4. Faça uma mudança focada, preserve os contratos HTTP e atualize os exemplos afetados.
5. Execute as suites pertinentes com `python scripts/run_tests.py`.
6. No pull request, descreva o comportamento anterior, o novo comportamento e a validação realizada.

Ao contribuir, você declara que tem direito de enviar o conteúdo e o disponibiliza sob a licença aplicável ao arquivo ou componente de destino. Não há transferência automática de titularidade. Não copie código incompatível com a licença desse componente.

Não envie `config.json`, tokens, logs operacionais, bancos de dados, cenas/texturas de terceiros, renders, ambientes Python ou executáveis. Exemplos devem usar dados fictícios e arquivos temporários. Preserve avisos de terceiros e as licenças dos scripts de integração.

Para problemas de segurança, siga [SECURITY.md](SECURITY.md) e evite publicar detalhes exploráveis em uma issue pública antes da correção.
