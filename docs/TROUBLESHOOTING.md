# Solução de problemas

| Sintoma | Verificação |
| --- | --- |
| `No module named renderfarm_shared` | Preserve a estrutura completa do checkout, incluindo `shared/`; execute o ponto de entrada do componente |
| `No module named app` ou coleta errada de testes | Use `scripts/run_tests.py`; não execute todas as suites no mesmo interpretador |
| Não encontra o Blender | Configure `blenderPath` com o caminho de `blender.exe` e teste sua execução sob a mesma conta |
| Worker não aparece | Verifique `host`, porta `9000`, firewall e cadastro manual; os exemplos desativam discovery |
| Master não encontra o Coordinator | Confira `coordinator_url`, porta `8010` e `/health`; discovery depende de UDP `8011` e da rede permitir broadcast |
| Segundo Coordinator não inicia | Encerre a instância duplicada; mantenha um Coordinator por namespace |
| Job fica `blocked` | Confira os caminhos em cada worker, compatibilidade do Blender e diagnóstico de preflight; a fila FIFO não passa à frente desse job |
| Arquivo acessível manualmente, mas não no Worker | Confira a conta do processo/serviço, permissões SMB e unidades mapeadas; prefira UNC |
| GPU não é utilizada | Confira preferência local, backend, drivers, dispositivos e compatibilidade do renderizador da cena |
| Render acaba, mas progresso não confirma | Verifique nome/padrão/formato de saída e acesso do Coordinator à pasta; arquivos vazios ou ainda sendo gravados não comprovam conclusão |
| HTTP 401 no Worker | Há um `authToken` configurado; os clientes incluídos não encaminham esse token |
| Sessão persistente demora a iniciar | Aumente o tempo de abertura para cenas grandes ou desative a opção para comparar com o modo padrão |
| Aplicativo não abre | Leia `startup_error.txt` do Coordinator e os logs de cada componente; confirme as dependências do ambiente usado |

Ao reportar um problema, informe versões do Windows/Python/Blender, componente, modo (`direct` ou `coordinator`), passos de reprodução e o erro. Remova tokens, nomes de clientes, caminhos privados e endereços internos desnecessários dos logs.
