# Instalação e primeiro render

## Requisitos

- Windows 10/11 de 64 bits. Os scripts de empacotamento e o canal de sessão persistente têm foco em Windows.
- Python 3.11 ou 3.12 de 64 bits, incluindo Tcl/Tk. Os testes desta publicação usam Python 3.12.
- Blender instalado separadamente nos workers, com drivers adequados se usar GPU. Não existe uma matriz de versões/GPU certificada para esta publicação.
- Permissão de leitura da cena e texturas e de escrita da saída nas contas que executam os aplicativos.
- Espaço para as dependências Python e para os renders.

Clone o repositório completo. Copiar somente a pasta `app/` de um componente remove dependências compartilhadas e não é uma instalação suportada.

## Ambiente Python

Execute na raiz do repositório:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Não é preciso ativar o ambiente nem mudar a política de execução do PowerShell. Para instalar somente um aplicativo, use seu `requirements.txt` no lugar de `requirements-dev.txt`, mantendo `shared/` junto ao checkout.

## Configurações locais

Copie o `config.example.json` de cada componente que vai usar para `config.json` no mesmo diretório. Faça isso apenas no primeiro uso: copiar novamente substitui suas configurações. Os arquivos reais são ignorados pelo Git.

Os exemplos usam `language: "pt"`, localhost e descoberta desativada. Master e Worker podem iniciar na mesma máquina que o Coordinator. No Worker, preencha `blenderPath` se a detecção automática não encontrar sua instalação; o exemplo usa CPU para o primeiro teste.

## Iniciar

Abra um terminal por processo, todos na raiz:

```powershell
.\.venv\Scripts\python.exe coordinator/blender_render_farm_coordinator/main.py
.\.venv\Scripts\python.exe worker/blender_render_farm_worker/worker.py
.\.venv\Scripts\python.exe master/blender_render_farm_master/main.py
```

Para visualizar os resultados:

```powershell
.\.venv\Scripts\python.exe image_viewer/BlenderImageViewer/main.py
```

Fechar a janela do Worker ou do Coordinator encerra o respectivo servidor. Para o Coordinator sem interface, execute **dentro da pasta do Coordinator**, com o Python do ambiente da raiz:

```powershell
cd coordinator/blender_render_farm_coordinator
..\..\.venv\Scripts\python.exe -m uvicorn app.api.routes:create_app --factory --host 127.0.0.1 --port 8010
```

Nesse modo, os argumentos de host/porta do Uvicorn devem corresponder aos valores de `config.json`, pois o discovery usa a configuração para anunciar a API. O Worker fica sem interface ao definir `showRenderDeviceWindow: false`.

## Verificação local

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health
Invoke-RestMethod http://127.0.0.1:9000/health
```

No Master, adicione um Worker com endereço `127.0.0.1` e porta `9000`. Selecione uma cena pequena com um intervalo de 2 ou 3 frames e uma pasta de saída vazia. Envie para a fila, acompanhe o Coordinator e confira se os arquivos foram gerados e abrem corretamente.

O painel web está em `http://127.0.0.1:8010/dashboard`; a referência interativa da API, em `http://127.0.0.1:8010/docs`.

## Usar várias máquinas na LAN

1. Instale o repositório, o Python e o Blender em cada worker.
2. No Coordinator e nos Workers, altere `host` para o IP privado da máquina ou `0.0.0.0`.
3. No Master, defina `coordinator_url` como `http://IP-DO-COORDINATOR:8010`.
4. Cadastre os IPs dos Workers no Master/Coordinator. Como alternativa, habilite `coordinatorDiscoveryEnabled` no Coordinator e nos Workers, e deixe `coordinator_url` vazio no Master para descoberta.
5. No firewall, permita apenas o tráfego necessário entre as máquinas confiáveis da farm.

| Porta padrão | Protocolo | Finalidade |
| --- | --- | --- |
| 8010 | TCP/HTTP | API e painel do Coordinator |
| 9000 | TCP/HTTP | API de cada Worker |
| 8011 | UDP | Descoberta opcional do Coordinator |

O Image Viewer utiliza descoberta automática do Coordinator e também tenta `http://127.0.0.1:8010`. Para um Coordinator em outra máquina, habilite a descoberta; a visualização local de imagens não depende dela.

Use **um Coordinator por namespace**. A proteção contra outra instância na rede não é um mecanismo de alta disponibilidade ou consenso distribuído.

## Arquivos compartilhados

Todos os processos devem resolver os mesmos caminhos para a cena e os renders. Exemplo: `\\servidor\render\cenas\demo.blend` e `\\servidor\render\saidas\demo`.

O Coordinator precisa ler a pasta de saída para confirmar os frames. Os Workers precisam ler a cena, suas dependências e escrever os resultados. Empacote recursos no `.blend` quando apropriado, ou mantenha texturas/caches em caminhos compartilhados consistentes. Letras de unidades mapeadas podem desaparecer quando o aplicativo roda sob outra conta ou como serviço.

Não abra portas no roteador para expor os serviços à internet. Consulte [as limitações de segurança](../SECURITY.md).
