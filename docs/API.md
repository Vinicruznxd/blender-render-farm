# Integração pela API

Base local do Coordinator: `http://127.0.0.1:8010`. Base local do Worker: `http://127.0.0.1:9000`. As APIs retornam JSON, salvo relatórios, páginas e recursos visuais.

O esquema completo da versão em execução está em `/openapi.json` e a interface interativa em `/docs`. Os exemplos abaixo operam em uma instalação local de teste.

## Coordinator

| Método e rota | Uso |
| --- | --- |
| `GET /health` | Saúde, versão e discovery |
| `GET /state` | Snapshot da fila, job ativo, masters, workers e métricas |
| `POST /masters/heartbeat` | Registra/atualiza a presença de um cliente |
| `GET /workers`, `POST /workers` | Lista e cadastra workers |
| `PUT /workers/{worker_id}`, `DELETE /workers/{worker_id}` | Atualiza ou remove um worker |
| `POST /workers/discover` | Procura workers na rede |
| `POST /workers/{worker_id}/test` | Testa um worker cadastrado |
| `POST /jobs/preflight` | Valida a proposta de job antes de enviá-la |
| `GET /jobs`, `POST /jobs` | Lista jobs ou adiciona à fila |
| `GET /jobs/{job_id}` | Consulta um job |
| `POST /jobs/{job_id}/pause` | Pausa o job |
| `POST /jobs/{job_id}/resume` | Retoma o job |
| `POST /jobs/{job_id}/cancel` | Cancela o job |
| `GET /jobs/{job_id}/report.pdf` | Relatório em PDF |
| `GET /logs` | Log e eventos centrais |

### Cadastrar um worker

```powershell
$worker = @{ name = 'WORKER-01'; ip = '127.0.0.1'; port = 9000; enabled = $true }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/workers -ContentType 'application/json' -Body ($worker | ConvertTo-Json)
```

### Validar e enviar um job

Substitua os caminhos por arquivos que realmente existem e são acessíveis à farm:

```powershell
$job = @{
    jobName = 'Teste de 3 frames'
    blendFile = 'C:\RenderFarm\cena.blend'
    outputFolder = 'C:\RenderFarm\saida'
    startFrame = 1
    endFrame = 3
    format = 'PNG'
    ignoreRenderedFrames = $true
}
$body = $job | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/jobs/preflight -ContentType 'application/json' -Body $body
# Confira o diagnóstico antes de executar a chamada que enfileira:
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/jobs -ContentType 'application/json' -Body $body
Invoke-RestMethod http://127.0.0.1:8010/state
```

`outputPattern` é opcional; normalmente deriva de `outputFolder/frame_####`. Consulte os schemas para roteamento de versão (`requiredBlenderVersion`, `preferredBlenderMajor`, `versionRoutingPolicy`), seleção GPU e identificação do cliente.

### Consultar com Python

```python
import requests

response = requests.get('http://127.0.0.1:8010/state', timeout=10)
response.raise_for_status()
state = response.json()
print(state)
```

## Worker

| Método e rota | Uso |
| --- | --- |
| `GET /health`, `GET /status`, `GET /capabilities` | Saúde, progresso e capacidade |
| `POST /validate-paths` | Verifica cena, saída e compatibilidade do Blender |
| `POST /render-block` | Aceita um bloco/frame para execução |
| `GET /render-device`, `POST /render-device` | Consulta/altera CPU/GPU |
| `POST /cancel` | Cancela o trabalho local |
| `POST /job-drain` | Solicita drenagem da sessão persistente |
| `GET /logs` | Consulta logs |

Use o Coordinator para distribuição central. Enviar `/render-block` manualmente enquanto ele agenda a mesma máquina pode gerar conflitos de propriedade do trabalho.

Se `authToken` estiver configurado no Worker, envie o cabeçalho `Authorization: Bearer <token>`. Nunca registre o token em exemplos públicos. O Coordinator não exige esse cabeçalho e os clientes incluídos não o encaminham aos Workers. Veja [segurança](../SECURITY.md).

Trate falhas HTTP, timeouts e estados não terminais explicitamente. O endpoint de criação não oferece uma chave de idempotência documentada; não repita cegamente um `POST /jobs` após timeout, pois pode criar duplicatas. Consulte a fila primeiro.
