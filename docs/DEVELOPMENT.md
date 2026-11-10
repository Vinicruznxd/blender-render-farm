# Desenvolvimento, testes e pacotes

## Preparar o ambiente

Na raiz do repositório:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/run_tests.py
```

Para uma suite específica:

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py worker
```

O runner usa subprocessos porque cada componente chama seu pacote principal de `app`. Um único `pytest` na raiz pode misturar importações. Os testes de Qt usam `QT_QPA_PLATFORM=offscreen`; alguns testes de Tk precisam de uma sessão gráfica Windows e podem abrir janelas brevemente. Os testes usam arquivos temporários e renderização simulada. Não substituem um teste de render real.

Os resultados da preparação desta publicação ficam em [VALIDATION.md](VALIDATION.md).

## Mudanças

Preserve os contratos HTTP usados pelos outros componentes. Adicione testes de comportamento ao corrigir agendamento, leases, retomada, validação de caminhos ou seleção de Blender. Coloque textos de interface no mecanismo de tradução compartilhado. Não inclua cenas de clientes, configurações reais ou dados operacionais.

## Build Windows

Cada script de build espera uma `.venv` **dentro do diretório do componente**. O ambiente da raiz usado para desenvolvimento não atende a esse caminho automaticamente.

Exemplo para o Worker, a partir da raiz:

```powershell
cd worker/blender_render_farm_worker
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pyinstaller
# Em uma cópia limpa, sem config.json preexistente:
Copy-Item config.example.json config.json
```

O empacotador central trabalha com destinos novos e separados. A partir da raiz:

```powershell
python build_support/package_release.py worker --build-root build/release-test --output releases
```

Ele usa a `.venv` do componente, gera o portable e seu ZIP, higieniza a configuração distribuída e registra SHA-256. Instale as dependências de build de cada componente antes: Master e Viewer têm `requirements-build.txt`; Coordinator e Worker utilizam PyInstaller. Para outra aplicação, substitua `worker` por `coordinator`, `master` ou `viewer`.

Todos os `.spec` leem `config.json`. Faça builds de publicação em uma cópia limpa com os arquivos de exemplo, nunca a partir de uma configuração operacional com tokens. Os scripts legados `build_portable.ps1` permanecem disponíveis para desenvolvimento, mas alguns copiam a configuração local; não publique seus resultados sem revisão. Um pacote binário também precisa carregar as licenças e avisos de todas as bibliotecas incluídas, inclusive Qt/PySide6, e os fontes correspondentes quando exigidos. O repositório inicial distribui código-fonte, não binários certificados.

Antes de compartilhar um pacote, verifique: inicialização em máquina limpa, scripts de integração disponíveis, licenças incluídas, ausência de segredos/dados locais e um render pequeno com a configuração real de GPU e rede.
