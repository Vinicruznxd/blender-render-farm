# Arquitetura e extensão

Os quatro aplicativos são processos independentes. Master, Coordinator e Worker se comunicam por HTTP; o discovery usa UDP. O Worker inicia o executável Blender como subprocesso, com integração Python executada dentro do Blender.

## Fluxo de um trabalho

1. O Master envia os caminhos e o intervalo de frames ao Coordinator.
2. O Coordinator persiste o job e os frames em SQLite.
3. O agendador escolhe o primeiro job não terminal e valida os caminhos nos Workers.
4. Cada frame recebe uma reserva (`lease`) e um `assignmentId` antes de ser enviado.
5. O Worker executa o Blender e reporta estado/progresso.
6. O Coordinator verifica os arquivos de saída; um retorno HTTP sozinho não comprova que o render existe.
7. Frames ausentes ou interrompidos podem voltar à fila, respeitando os limites de tentativas.

No modo central, `blockSize` permanece no contrato, mas a distribuição é feita por frame. O modo `direct` do Master utiliza seu próprio agendador de blocos e arquivos JSON locais.

## Organização do código

| Área | Arquivos principais |
| --- | --- |
| Contrato HTTP central | `coordinator/.../app/api/routes.py`, `schemas.py` |
| Agendamento e persistência | `coordinator/.../app/core/coordinator_service.py`, `scheduler.py`, `database.py` |
| Cadastro e probes | `coordinator/.../app/core/worker_registry.py` |
| Execução de render | `worker/.../app/blender_runner.py` |
| Sessão persistente | `worker/.../app/persistent_session.py` |
| Contrato HTTP do Worker | `worker/.../app/api.py`, `models.py` |
| Master local e UI | `master/.../app/core/`, `app/ui/` |
| Seleção de imagens | `image_viewer/BlenderImageViewer/app/core/frame_selection.py` |
| Traduções | `shared/renderfarm_shared/i18n.py`, `messages.py` |

`...` representa a pasta `blender_render_farm_<componente>`.

## Implementar uma integração

Prefira criar um cliente HTTP usando [a API](API.md). Use `GET /state` para a visão central; não altere o SQLite diretamente. Preserve `jobId`, `blockId`, `assignmentId` e as informações de versão ao estender um Worker. Use o preflight antes de enfileirar cenas em uma interface própria.

Para incluir formatos, atualize validação, construção do comando, scanner de arquivos e testes juntos. Para incluir opções de GPU ou novas versões do Blender, mantenha o fallback e os campos de diagnóstico observáveis.

Os componentes têm pacotes Python com o mesmo nome (`app`). Não importe vários componentes no mesmo interpretador; execute cada suite separadamente com `scripts/run_tests.py`.

## Sessão persistente

Essa opção mantém a cena carregada e aceita uma fila limitada de frames compatíveis. O controle local usa `multiprocessing.connection` e named pipes no Windows, com autenticação interna. O Coordinator conserva afinidade e solicita drenagem quando o job termina. O modo continua opcional: valide consumo de memória, carga da cena e recuperação na sua infraestrutura.

A separação por processo não dispensa observar as licenças de cada componente. Veja [licenciamento](LICENSING.md) antes de incorporar código em outro produto.
