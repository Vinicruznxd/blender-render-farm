# Configuração

Os aplicativos leem `config.json` ao lado do arquivo de entrada, ou do executável quando empacotados. Use os `config.example.json` versionados como ponto de partida. IDs vazios são preenchidos no primeiro uso; não distribua IDs ou configurações reais entre máquinas.

## Coordinator

| Campo | Uso |
| --- | --- |
| `host`, `port` | Endereço de escuta e porta HTTP; exemplo local: `127.0.0.1:8010` |
| `coordinatorId` | Identidade persistente; deixe vazio na instalação inicial |
| `databasePath`, `logsDir` | Caminhos relativos à pasta do Coordinator ou absolutos |
| `coordinatorDiscoveryEnabled` | Habilita os anúncios e probes UDP |
| `coordinatorDiscoveryPort` | Porta UDP, normalmente `8011` |
| `coordinatorDiscoveryNamespace` | Nome da rede lógica, padrão `blender_render_farm` |
| `schedulerIntervalSeconds` | Intervalo do agendador |
| `leaseDurationSeconds` | Prazo da reserva de um frame; ajuste para cargas lentas |
| `maxFrameAttempts` | Limite de tentativas de um frame |
| `autoUnblockBlockedJobs` | Tenta recuperar o primeiro job bloqueado |
| `language` | `pt`, `en` ou `es` |

A fila é FIFO: um job `blocked` pode impedir o início dos seguintes. A implementação completa dos valores padrão está em `app/core/settings.py`.

## Worker

| Campo | Uso |
| --- | --- |
| `host`, `port` | Escuta HTTP, normalmente porta `9000` |
| `workerName` | Nome exibido; vazio usa o hostname |
| `blenderPath` | Caminho do executável; vazio tenta detecção automática |
| `renderDevicePreference` | `CPU` ou `GPU`; a preferência local tem precedência sobre `useGpu` legado |
| `gpuBackend` | `AUTO`, `OPTIX`, `CUDA`, `HIP`, `ONEAPI` ou `METAL`; disponibilidade depende da instalação |
| `gpuDeviceIds`, `gpuEnableAll`, `gpuIncludeCpu` | Seleção de dispositivos para Cycles |
| `allowNewerMajorBlender` | Permite considerar versões principais mais novas; não garante compatibilidade da cena |
| `showRenderDeviceWindow` | Abre a interface local; `false` mantém somente o serviço HTTP |
| `allowJobs` | Controla se aceita novos renders |
| `persistentSessionEnabled` | Reutiliza o Blender por job; padrão `false` |
| `persistentSessionIdleTimeoutSeconds` | Fecha sessão ociosa após o prazo |
| `persistentSessionMaxQueueDepth` | Limita a fila local da sessão |
| `persistentSessionOpenTimeoutSeconds` | Tempo para carregar a cena e conectar o canal de controle |
| `coordinatorDiscoveryEnabled`, `coordinatorDiscoveryPort` | Descoberta opcional e registro no Coordinator |
| `authToken` | Token bearer opcional do Worker; veja a limitação abaixo |

Se ativar `authToken`, as chamadas ao Worker exigem `Authorization: Bearer ...`. Os clientes incluídos não têm configuração para encaminhar esse token. Ativá-lo sem adaptar os clientes impede o funcionamento da farm integrada. A versão atual requer isolamento por rede/firewall; o token não fornece autenticação ao Coordinator.

## Master e Viewer

O Master usa nomes em `snake_case`: `mode`, `coordinator_url`, `master_id`, `master_name`, `workers`, `default_block_size`, `max_block_retries`. `mode: "coordinator"` usa a fila central; `direct` controla os Workers diretamente. URL vazia em modo Coordinator ativa a descoberta.

O Viewer armazena `language`, `last_image_folder`, `last_blend_file`, `last_output_folder` e `grid_columns` (1 a 4). Esses caminhos pertencem ao usuário local e não devem entrar no repositório. O Viewer descobre o Coordinator pela rede e também tenta `http://127.0.0.1:8010`.

## Dados e backup

- Coordinator: `app/data/coordinator.db`, acompanhado dos arquivos WAL/SHM quando ativos.
- Master: `app/data/jobs/` e `app/data/logs/`.
- Worker: `logs/`.
- Viewer: `app/data/logs/` e preferências em `config.json`.

Para um backup simples e consistente, encerre o Coordinator antes de copiar seu banco e configuração. Não copie apenas o `.db` enquanto houver gravações em WAL. Não substitua instalações ou configurações durante um render.
