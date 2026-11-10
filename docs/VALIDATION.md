# Validação da publicação

Data: **10 de novembro de 2026**.

Ambiente local: Windows, Python **3.12.10** de 64 bits e ambiente virtual isolado, instalado a partir dos requisitos do repositório. PySide6 **6.8.3**, FastAPI **0.162.4**, Pydantic **2.14.3**, pytest **8.4.2**, psutil **6.1.1** e zstandard **0.25.0**.

| Suite | Resultado |
| --- | --- |
| Coordinator | 87 testes aprovados |
| Worker, após separar os scripts GPL | 96 testes aprovados |
| Master | 73 testes aprovados |
| Image Viewer | 6 testes aprovados |
| **Total** | **262 testes aprovados** |

Comandos executados na raiz, com o Python do ambiente de validação:

```powershell
python scripts/run_tests.py
# Após alterar a localização dos scripts de integração:
python scripts/run_tests.py worker
python -m pip check
```

O teste completo anterior à separação aprovou 260 testes; a suite Worker foi repetida após a alteração, com dois novos testes do contrato de configuração JSON e localização de scripts em um bundle. `pip check` não encontrou dependências incompatíveis.

O comando documentado `uvicorn app.api.routes:create_app --factory` também foi iniciado em uma porta temporária de localhost. `/health`, `/docs`, `/openapi.json`, `/state` e `/dashboard` responderam HTTP 200, sem enviar jobs ou executar renders.

Há avisos de depreciação de FastAPI/Starlette (`on_event` e uso de HTTPX no TestClient). Eles não causaram falhas e continuam como manutenção futura.

## Limites

- Testes de render usam processos simulados/mocks. Não foi realizado um render real com Blender nesta preparação.
- Não foram certificados drivers, modelos de GPU, versões de Blender ou compartilhamentos SMB.
- Não foram gerados/validados executáveis PyInstaller. Os specs incluem os scripts GPL e avisos de licença, mas exigem teste de distribuição próprio.
- A revisão de arquivos públicos excluiu configurações reais, bancos, logs, backups e builds. A varredura de padrões de credenciais não encontrou correspondências; isso não equivale a uma auditoria completa de segurança.
- O fluxo CI do GitHub executa as suites em Windows/Python 3.12. Consulte a aba Actions para o resultado do commit específico.
