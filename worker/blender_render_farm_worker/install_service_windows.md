# Instalar o Worker como Servico no Windows

O caminho mais simples nesta primeira versao e usar o NSSM.

## Opcao recomendada: NSSM

### 1. Preparar o worker

No diretorio do projeto:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Teste manualmente antes:

```powershell
python worker.py
```

Se `/health` e `/status` responderem, siga para o servico.

Antes de instalar como serviço, defina `showRenderDeviceWindow` como `false`
em `config.json`. Confira também `host`, permissões de acesso aos arquivos e
[as limitações de segurança](../../SECURITY.md). Use o checkout completo,
incluindo `../../shared/` e a pasta `blender_scripts/`.

Se quiser escolher `CPU` ou `GPU` pela janela local, faca isso antes de instalar como servico ou rode o worker manualmente ao menos uma vez para salvar a preferencia em `config.json`.

Em execucao manual no desktop, essa janela e a propria instancia visual do worker. Se ela for fechada, o worker inteiro encerra.

### 2. Baixar o NSSM

- Baixe o NSSM no site oficial.
- Extraia, por exemplo, em `C:\tools\nssm\`.

### 3. Instalar o servico

Abra PowerShell ou Prompt de Comando como administrador e execute:

```powershell
C:\tools\nssm\win64\nssm.exe install BlenderRenderFarmWorker
```

Na janela do NSSM, preencha:

- `Path`: `C:\Users\SEU_USUARIO\Documents\blender\worker\blender_render_farm_worker\.venv\Scripts\python.exe`
- `Startup directory`: `C:\Users\SEU_USUARIO\Documents\blender\worker\blender_render_farm_worker`
- `Arguments`: `worker.py`

Depois clique em `Install service`.

### 4. Configurar inicio automatico

```powershell
C:\tools\nssm\win64\nssm.exe set BlenderRenderFarmWorker Start SERVICE_AUTO_START
```

### 5. Iniciar o servico

```powershell
C:\tools\nssm\win64\nssm.exe start BlenderRenderFarmWorker
```

### 6. Verificar

```powershell
curl http://localhost:9000/health
curl http://localhost:9000/status
curl http://localhost:9000/render-device
```

## Conta do servico e acesso a rede

Esse item e critico para `.blend` em compartilhamentos.

Se o servico for executado como `LocalSystem`, ele pode nao ter:

- acesso ao compartilhamento de rede
- unidade `X:\` mapeada
- permissoes equivalentes ao seu usuario

Quando houver arquivos em rede, prefira uma destas abordagens:

1. Configure o servico para rodar com um usuario especifico que ja tenha acesso ao compartilhamento.
2. Use caminho UNC, como `\\SERVIDOR\Projetos\Cena01\cena.blend`, no lugar de `X:\Projetos\Cena01\cena.blend`.
3. Mapeie a unidade de rede no contexto do servico antes de iniciar o worker.

Depois valide com:

```powershell
curl -X POST http://localhost:9000/validate-paths -H "Content-Type: application/json" -d "{\"blendFile\":\"\\\\SERVIDOR\\Projetos\\Cena01\\cena.blend\",\"outputFolder\":\"\\\\SERVIDOR\\Projetos\\Cena01\\renders\"}"
```

Se a resposta trouxer algo como:

```json
{
  "ok": false,
  "message": "Drive X: nao esta montado no contexto do processo do worker. O usuario logado pode enxergar essa unidade, mas o processo atual do worker nao. Execute o worker no mesmo usuario que mapeou a unidade ou use caminho UNC."
}
```

isso confirma que o servico nao esta enxergando a unidade mapeada no contexto atual.

## Preferencia local de CPU/GPU em modo servico

Servicos do Windows normalmente nao mostram a janela local de selecao de `CPU` e `GPU`.

Por isso, em modo servico, ajuste a preferencia por um destes meios.

### Opcao 1: editar `config.json`

Exemplo para CPU:

```json
"renderDevicePreference": "CPU"
```

Exemplo para GPU:

```json
"renderDevicePreference": "GPU"
```

### Opcao 2: alterar pela API

Para CPU:

```powershell
curl -X POST http://localhost:9000/render-device -H "Content-Type: application/json" -d "{\"renderDevicePreference\":\"CPU\"}"
```

Para GPU:

```powershell
curl -X POST http://localhost:9000/render-device -H "Content-Type: application/json" -d "{\"renderDevicePreference\":\"GPU\"}"
```

Depois de salvo, o valor fica persistido para os proximos boots da maquina.

## Logs do servico

O projeto ja grava:

- `logs/worker.log`
- `logs/blender_job_<jobId>_<blockId>.log`

Se quiser logs adicionais do proprio NSSM:

```powershell
C:\tools\nssm\win64\nssm.exe set BlenderRenderFarmWorker AppStdout C:\Users\SEU_USUARIO\Documents\blender\worker\blender_render_farm_worker\logs\nssm_stdout.log
C:\tools\nssm\win64\nssm.exe set BlenderRenderFarmWorker AppStderr C:\Users\SEU_USUARIO\Documents\blender\worker\blender_render_farm_worker\logs\nssm_stderr.log
```

## Reiniciar ou parar

```powershell
C:\tools\nssm\win64\nssm.exe restart BlenderRenderFarmWorker
C:\tools\nssm\win64\nssm.exe stop BlenderRenderFarmWorker
```

## Remover o servico

```powershell
C:\tools\nssm\win64\nssm.exe remove BlenderRenderFarmWorker confirm
```

## Firewall

Libere a porta do worker, por exemplo:

```powershell
New-NetFirewallRule -DisplayName "Blender Render Farm Worker 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow
```

## Energia e operacao headless

Para maquinas de render:

- desative suspensao e hibernacao
- use plano de energia de alto desempenho
- mantenha drivers de GPU atualizados
- considere dummy HDMI plug em maquinas sem monitor, se a GPU perder aceleracao sem saida ativa

## Alternativas

Voce tambem pode usar:

- WinSW
- `pywin32`

Mas para uma primeira instalacao simples e reproduzivel, NSSM costuma ser a opcao mais rapida.
