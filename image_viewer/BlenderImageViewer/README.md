# Blender Render Farm Image Viewer

Visualizador de imagens com seleção de frames e reenvio de grupos ao Coordinator.

[Visão geral da suite](../../README.md) · [Instalação](../../docs/INSTALLATION.md) · [Configuração](../../docs/CONFIGURATION.md) · [API](../../docs/API.md)

## Instalar e executar

Mantenha o checkout completo: este componente depende de `../../shared/`.
No diretório deste componente, com Python 3.11 ou 3.12 de 64 bits no Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Somente no primeiro uso; não substitua configurações existentes:
Copy-Item config.example.json config.json
.\.venv\Scripts\python.exe main.py
```

O Viewer procura o Coordinator por descoberta UDP e também tenta `http://127.0.0.1:8010`. Pastas recentes e preferências ficam no `config.json` local.

O exemplo começa em modo local, quando aplicável. Para a LAN, siga o guia de instalação; não exponha as APIs à internet. Confira [SECURITY.md](../../SECURITY.md).

## Desenvolvimento

Na raiz do repositório, instale `requirements-dev.txt` e execute `python scripts/run_tests.py` com o Python do ambiente virtual. Cada suite roda em um processo separado. Para empacotar, veja [DEVELOPMENT.md](../../docs/DEVELOPMENT.md).

## Licença

Veja [LICENSE](../../LICENSE), [NOTICE](../../NOTICE) e [o escopo de licenciamento](../../docs/LICENSING.md). Os avisos de terceiros e as exceções indicadas nesses arquivos também se aplicam.
