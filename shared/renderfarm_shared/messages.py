"""Shared desktop UI catalog. English is the source language."""

MESSAGES = {'Cancel': ('Cancelar', 'Cancelar'),
 'Queued': ('Em fila', 'En cola'),
 'Session ready': ('Sessão pronta', 'Sesión lista'),
 'Finishing job': ('Finalizando job', 'Finalizando trabajo'),
 'Closing session': ('Encerrando sessão', 'Cerrando sesión'),
 'Connecting': ('Conectando', 'Conectando'),
 'Waiting for next frames': ('Aguardando os próximos frames', 'Esperando los próximos frames'),
 'Preparing to switch jobs': ('Preparando a troca de job', 'Preparando el cambio de trabajo'),
 '{status} | Job: {job} | Device: {device}': ('{status} | Job: {job} | Dispositivo: {device}', '{status} | Trabajo: {job} | Dispositivo: {device}'),
 'Compatibility policy: {policy}': ('Política de compatibilidade: {policy}', 'Política de compatibilidad: {policy}'),
 'Close': ('Fechar', 'Cerrar'),
 'Free': ('Livre', 'Libre'),
 'Status': ('Status', 'Estado'),
 'Queue': ('Fila', 'Cola'),
 'Job': ('Job', 'Trabajo'),
 'Frame': ('Frame', 'Frame'),
 'Frames': ('Frames', 'Frames'),
 'Job ID': ('ID do job', 'ID del trabajo'),
 'Master ID': ('ID do Master', 'ID del Master'),
 'Host': ('Host', 'Host'),
 'Coordinator ID': ('ID do Coordinator', 'ID del Coordinator'),
 'Uptime': ('Tempo ativo', 'Tiempo activo'),
 'Discovery': ('Descoberta', 'Descubrimiento'),
 'URLs': ('URLs', 'URLs'),
 'Discovering': ('Procurando', 'Buscando'),
 'Conflict': ('Conflito', 'Conflicto'),
 'Running': ('Em execução', 'En ejecución'),
 'Stopped': ('Parado', 'Detenido'),
 'Not detected': ('Não detectado', 'No detectado'),
 'Unavailable': ('Indisponível', 'No disponible'),
 'Render device': ('Dispositivo de render', 'Dispositivo de render'),
 'Change device': ('Trocar dispositivo', 'Cambiar dispositivo'),
 'Open {url}': ('Abrir {url}', 'Abrir {url}'),
 '{count}d ago': ('há {count}d', 'hace {count}d'),
 'Blocks organize the queue; individual frames are dispatched to balance machines with different speeds.': ('Os '
                                                                                                            'blocos '
                                                                                                            'organizam '
                                                                                                            'a '
                                                                                                            'fila; '
                                                                                                            'o '
                                                                                                            'envio '
                                                                                                            'é '
                                                                                                            'feito '
                                                                                                            'frame '
                                                                                                            'a '
                                                                                                            'frame '
                                                                                                            'para '
                                                                                                            'balancear '
                                                                                                            'máquinas '
                                                                                                            'com '
                                                                                                            'velocidades '
                                                                                                            'diferentes.',
                                                                                                            'Los '
                                                                                                            'bloques '
                                                                                                            'organizan '
                                                                                                            'la '
                                                                                                            'cola; '
                                                                                                            'los '
                                                                                                            'frames '
                                                                                                            'se '
                                                                                                            'envían '
                                                                                                            'individualmente '
                                                                                                            'para '
                                                                                                            'equilibrar '
                                                                                                            'máquinas '
                                                                                                            'de '
                                                                                                            'distintas '
                                                                                                            'velocidades.'),
 'Polling failed': ('Falha na atualização', 'Error de actualización'),
 'Could not refresh workers': ('Não foi possível atualizar os workers', 'No se pudieron actualizar los workers'),
 'Path validation failed': ('Falha ao validar caminhos', 'Error al validar rutas'),
 'Global queue updated': ('Fila global atualizada', 'Cola global actualizada'),
 'Job started with warnings': ('Job iniciado com avisos', 'Trabajo iniciado con avisos'),
 'Could not pause': ('Falha ao pausar', 'No se pudo pausar'),
 'Could not resume': ('Falha ao retomar', 'No se pudo reanudar'),
 'Cancel the selected job?': ('Cancelar o job selecionado?', '¿Cancelar el trabajo seleccionado?'),
 'Could not cancel': ('Falha ao cancelar', 'No se pudo cancelar'),
 'Central rescan': ('Reescaneamento central', 'Reescaneo central'),
 'The Coordinator manages frame rescans.': ('O Coordinator gerencia o reescaneamento de frames.',
                                            'El Coordinator gestiona el reescaneo de frames.'),
 'Could not rescan': ('Falha ao reescanear', 'No se pudo reescanear'),
 'Central queue': ('Fila central', 'Cola central'),
 'The Coordinator persists the shared queue automatically.': ('O Coordinator salva a fila compartilhada '
                                                              'automaticamente.',
                                                              'El Coordinator guarda la cola compartida '
                                                              'automáticamente.'),
 'Local job loading is unavailable in Coordinator mode.': ('O carregamento local de jobs está indisponível no '
                                                           'modo Coordinator.',
                                                           'La carga local de trabajos no está disponible en modo '
                                                           'Coordinator.'),
 'Could not save': ('Erro ao salvar', 'No se pudo guardar'),
 'Job saved': ('Job salvo', 'Trabajo guardado'),
 'State saved to:\n{path}': ('Estado salvo em:\n{path}', 'Estado guardado en:\n{path}'),
 'Could not load job': ('Erro ao carregar job', 'No se pudo cargar el trabajo'),
 'Invalid worker': ('Worker inválido', 'Worker no válido'),
 'Enter an IP address or hostname for the worker.': ('Informe um IP ou hostname para o worker.',
                                                     'Introduce una IP o un nombre de host para el worker.'),
 'Could not add worker': ('Falha ao adicionar worker', 'No se pudo añadir el worker'),
 'Searching...': ('Procurando...', 'Buscando...'),
 'No worker selected': ('Nenhum worker selecionado', 'Ningún worker seleccionado'),
 'Select a worker to remove.': ('Selecione um worker para remover.', 'Selecciona un worker para eliminar.'),
 'Remove worker': ('Remover worker', 'Eliminar worker'),
 'Remove this worker?': ('Remover este worker?', '¿Eliminar este worker?'),
 'Could not remove worker': ('Falha ao remover worker', 'No se pudo eliminar el worker'),
 'Select a worker to test.': ('Selecione um worker para testar.', 'Selecciona un worker para probar.'),
 'Test failed': ('Falha no teste', 'Error en la prueba'),
 'Test completed': ('Teste concluído', 'Prueba completada'),
 'Select a worker to change its device.': ('Selecione um worker para trocar o dispositivo.',
                                           'Selecciona un worker para cambiar su dispositivo.'),
 'Action unavailable': ('Ação indisponível', 'Acción no disponible'),
 'Device changes are unavailable through this Coordinator.': ('A troca de dispositivo está indisponível neste '
                                                              'Coordinator.',
                                                              'El cambio de dispositivo no está disponible en '
                                                              'este Coordinator.'),
 'Worker not found': ('Worker não encontrado', 'Worker no encontrado'),
 'The worker could not be found.': ('Não foi possível localizar o worker.', 'No se pudo localizar el worker.'),
 'Select the render device for {name}': ('Selecione o dispositivo de render para {name}',
                                         'Selecciona el dispositivo de render para {name}'),
 'Could not change device': ('Falha ao trocar dispositivo', 'No se pudo cambiar el dispositivo'),
 'Render device updated': ('Dispositivo de render atualizado', 'Dispositivo de render actualizado'),
 '{name} now prefers {device}.': ('{name} agora prefere {device}.', '{name} ahora prefiere {device}.'),
 'Show all registered machines.': ('Mostra todas as máquinas cadastradas.',
                                   'Muestra todas las máquinas registradas.'),
 '{name}\nStatus: {status}\nWorkers in this job: {count}': ('{name}\nStatus: {status}\nWorkers neste job: {count}',
                                                            '{name}\n'
                                                            'Estado: {status}\n'
                                                            'Workers en este trabajo: {count}'),
 'Settings and runtime': ('Configurações e execução', 'Configuración y ejecución'),
 'Mode': ('Modo', 'Modo'),
 'Base folder': ('Pasta base', 'Carpeta base'),
 'Configuration': ('Configuração', 'Configuración'),
 'Jobs folder': ('Pasta de jobs', 'Carpeta de trabajos'),
 'Logs folder': ('Pasta de logs', 'Carpeta de registros'),
 'App version': ('Versão do aplicativo', 'Versión de la aplicación'),
 'Build ID': ('ID do build', 'ID de compilación'),
 'Protocol version': ('Versão do protocolo', 'Versión del protocolo'),
 'Active job': ('Job ativo', 'Trabajo activo'),
 'Coordinator status': ('Estado do Coordinator', 'Estado del Coordinator'),
 'Coordinator message': ('Mensagem do Coordinator', 'Mensaje del Coordinator'),
 'Coordinator host': ('Host do Coordinator', 'Host del Coordinator'),
 'Coordinator API': ('API do Coordinator', 'API del Coordinator'),
 'Blender found': ('Blender encontrado', 'Blender encontrado'),
 'Worker message': ('Mensagem do worker', 'Mensaje del worker'),
 'Blend file exists': ('Arquivo .blend existe', 'El archivo .blend existe'),
 'Output folder exists': ('Pasta de saída existe', 'La carpeta de salida existe'),
 'Output is writable': ('Permissão de escrita na saída', 'Permiso de escritura en la salida'),
 'Worker refresh completed.': ('Atualização de workers concluída.', 'Actualización de workers completada.'),
 'Workers found': ('Workers encontrados', 'Workers encontrados'),
 'New registrations': ('Novos cadastros', 'Nuevos registros'),
 'Updated': ('Atualizados', 'Actualizados'),
 'Endpoints checked': ('Endereços verificados', 'Direcciones comprobadas'),
 'Networks checked': ('Redes verificadas', 'Redes comprobadas'),
 'Ports checked': ('Portas verificadas', 'Puertos comprobados'),
 'No worker responded to this scan.': ('Nenhum worker respondeu a esta varredura.',
                                       'Ningún worker respondió a esta búsqueda.'),
 'Registered workers remain saved for later use.': ('Os workers cadastrados continuam salvos para uso posterior.',
                                                    'Los workers registrados siguen guardados para su uso '
                                                    'posterior.'),
 'Blender version choice': ('Escolha da versão do Blender', 'Selección de versión de Blender'),
 'This file requires Blender {version}. Available workers with the same major version are older.': ('Este arquivo '
                                                                                                    'requer '
                                                                                                    'Blender '
                                                                                                    '{version}. '
                                                                                                    'Os workers '
                                                                                                    'disponíveis '
                                                                                                    'da mesma '
                                                                                                    'versão '
                                                                                                    'principal '
                                                                                                    'são mais '
                                                                                                    'antigos.',
                                                                                                    'Este archivo '
                                                                                                    'requiere '
                                                                                                    'Blender '
                                                                                                    '{version}. '
                                                                                                    'Los workers '
                                                                                                    'disponibles '
                                                                                                    'de la misma '
                                                                                                    'versión '
                                                                                                    'principal '
                                                                                                    'son '
                                                                                                    'anteriores.'),
 'File version': ('Versão do arquivo', 'Versión del archivo'),
 'Older versions in the same major series': ('Versões anteriores da mesma série principal',
                                             'Versiones anteriores de la misma serie principal'),
 'Higher available versions': ('Versões superiores disponíveis', 'Versiones superiores disponibles'),
 'Use older version': ('Usar versão anterior', 'Usar versión anterior'),
 'Use higher version': ('Usar versão superior', 'Usar versión superior'),
 'Select which available Blender version should render this file.': ('Escolha qual versão disponível do Blender '
                                                                     'deve renderizar este arquivo.',
                                                                     'Elige qué versión disponible de Blender '
                                                                     'debe renderizar este archivo.'),
 'No higher major version is available.': ('Nenhuma versão principal superior está disponível.',
                                           'No hay una versión principal superior disponible.'),
 'Prefer {major}.x >= {version}': ('Preferir {major}.x >= {version}', 'Preferir {major}.x >= {version}'),
 'Level': ('Nível', 'Nivel'),
 'Info': ('Informação', 'Información'),
 'Warning': ('Aviso', 'Advertencia'),
 'Master log': ('Log do Master', 'Registro del Master'),
 'Worker log': ('Log do worker', 'Registro del worker'),
 'Worker log: {name}': ('Log do worker: {name}', 'Registro del worker: {name}'),
 'Worker log: no worker selected': ('Log do worker: nenhum worker selecionado',
                                    'Registro del worker: ningún worker seleccionado'),
 'Worker API is unavailable': ('A API do Worker está indisponível', 'La API del Worker no está disponible'),
 'Coordinator discovery is disabled': ('A descoberta do Coordinator está desabilitada',
                                       'El descubrimiento del Coordinator está deshabilitado'),
 'Blender: waiting for detection': ('Blender: aguardando detecção', 'Blender: esperando detección'),
 'Could not save preference: {error}': ('Não foi possível salvar a preferência: {error}',
                                        'No se pudo guardar la preferencia: {error}'),
 'Worker startup error': ('Erro ao iniciar o Worker', 'Error al iniciar el Worker'),
 'The Worker API could not start.\n\n{error}': ('Não foi possível iniciar a API do Worker.\n\n{error}',
                                                'No se pudo iniciar la API del Worker.\n\n{error}'),
 'Dashboard refresh failed: {error}': ('Falha ao atualizar o painel: {error}',
                                       'Error al actualizar el panel: {error}'),
 '{count} workers found': ('{count} workers encontrados', '{count} workers encontrados'),
 '{done}/{total} completed | rendering {rendering} | reserved {leased} | pending {pending}': ('{done}/{total} '
                                                                                              'concluídos | '
                                                                                              'renderizando '
                                                                                              '{rendering} | '
                                                                                              'reservados '
                                                                                              '{leased} | '
                                                                                              'pendentes '
                                                                                              '{pending}',
                                                                                              '{done}/{total} '
                                                                                              'completados | '
                                                                                              'renderizando '
                                                                                              '{rendering} | '
                                                                                              'reservados '
                                                                                              '{leased} | '
                                                                                              'pendientes '
                                                                                              '{pending}'),
 'File {version}': ('Arquivo {version}', 'Archivo {version}'),
 'Required {version}': ('Requerido {version}', 'Requerido {version}'),
 'Preferred {version}': ('Preferido {version}', 'Preferido {version}'),
 'Could not start the Coordinator.\n\n{error}': ('Não foi possível iniciar o Coordinator.\n\n{error}',
                                                 'No se pudo iniciar el Coordinator.\n\n{error}'),
 'Timeline': ('Linha do tempo', 'Línea de tiempo'),
 'Test connection': ('Testar conexão', 'Probar conexión'),
 'Testing connection...': ('Testando conexão...', 'Probando conexión...'),
 'Connection failed: {error}': ('Falha na conexão: {error}', 'Error de conexión: {error}'),
 'Coordinator is not connected yet.': ('O Coordinator ainda não está conectado.',
                                       'El Coordinator aún no está conectado.'),
 'A faster tomorrow, one frame at a time.': ('Um amanhã mais rápido, um frame de cada vez.',
                                             'Un mañana más rápido, un frame a la vez.'),
 'Worker discovery finished.': ('Descoberta de workers concluída.', 'Descubrimiento de workers completado.'),
 'Could not discover workers: {error}': ('Falha ao descobrir workers: {error}',
                                         'No se pudieron descubrir workers: {error}'),
 'Shutting down the Coordinator...': ('Encerrando o Coordinator...', 'Cerrando el Coordinator...'),
 'Shutdown error: {error}': ('Erro ao encerrar: {error}', 'Error al cerrar: {error}'),
 'Currently rendering an assigned frame': ('Renderizando um frame atribuído', 'Renderizando un frame asignado'),
 'This worker is not accepting new jobs': ('Este worker não está aceitando novos jobs',
                                           'Este worker no acepta nuevos trabajos'),
 'Worker needs attention': ('O Worker precisa de atenção', 'El Worker necesita atención'),
 'Device changes apply to the next frame.': ('A troca de dispositivo vale para o próximo frame.',
                                             'El cambio de dispositivo se aplica al siguiente frame.'),
 'Default compatibility policy': ('Política padrão de compatibilidade',
                                  'Política de compatibilidad predeterminada'),
 'Timeout': ('Tempo esgotado', 'Tiempo agotado')}
