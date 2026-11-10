# Avisos de terceiros

O [LICENSE](LICENSE) da raiz aplica-se ao código original de gerenciamento, conforme [NOTICE](NOTICE). Não substitui as licenças abaixo.

## Scripts Blender

A pasta `worker/blender_render_farm_worker/blender_scripts/` contém os programas executados dentro do Blender sob **GPL-3.0-or-later**. Seus fontes e a licença estão incluídos. Não há restrição não comercial adicional sobre esses programas.

## Blender

O Blender é instalado separadamente e não está incluído neste repositório. Consulte a [licença do Blender](https://www.blender.org/about/license/). Blender e seu logotipo pertencem aos respectivos titulares; os recursos identificados em `img/ASSETS.json` devem observar as [diretrizes oficiais da marca](https://www.blender.org/about/logo/). O projeto não alega patrocínio, afiliação ou autoria do Blender.

## Lucide e Feather

Os conjuntos de ícones em `master/.../img/icons/`, `coordinator/.../img/icons/` e `worker/.../img/icons/` derivam de **Lucide 0.468.0**, com material originário de Feather. Os arquivos `img/ASSETS.json` registram URLs e hashes das fontes; cada componente inclui `img/LUCIDE_LICENSE.txt`, que deve ser preservado com os recursos.

- Lucide: licença ISC; copyright dos Lucide Contributors.
- Partes originárias de Feather: licença MIT; copyright de Cole Bemis.

As cores e tamanhos rasterizados não removem as obrigações de atribuição. Veja o [repositório Lucide](https://github.com/lucide-icons/lucide) e a [licença Feather](https://github.com/feathericons/feather/blob/main/LICENSE).

## Dependências Python

As bibliotecas são instaladas pelo usuário a partir dos respectivos `requirements.txt`; seus binários não fazem parte desta publicação de fontes. Consulte os metadados e arquivos de licença da versão efetivamente instalada.

| Componente | Referência de licença/projeto |
| --- | --- |
| Qt / PySide6 | [Licenciamento Qt for Python](https://doc.qt.io/qtforpython-6.8/licenses.html): opções LGPL/GPL/comercial conforme componente |
| FastAPI | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Uvicorn | [BSD-3-Clause](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| Pydantic | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| HTTPX | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| Requests | [Apache-2.0](https://github.com/psf/requests/blob/main/LICENSE) |
| Pillow | [Texto de licença Pillow/PIL](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| psutil | [BSD-3-Clause](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| python-zstandard | [Licenças do wrapper e bibliotecas incluídas](https://github.com/indygreg/python-zstandard/blob/main/LICENSE) |
| pytest | [MIT](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |

A tabela não é um inventário completo de dependências transitivas. Ao distribuir um pacote PyInstaller, inclua os avisos e fontes/requisitos aplicáveis a todas as dependências efetivamente empacotadas. Em especial, mantenha os direitos exigidos pela LGPL ao distribuir Qt/PySide6; não aplique a restrição comercial do gerenciador às bibliotecas.

## Textos das licenças

PolyForm Noncommercial foi obtida do [texto oficial](https://polyformproject.org/licenses/noncommercial/1.0.0.txt), sem alterações. O texto GPLv3 foi obtido do [catálogo SPDX](https://github.com/spdx/license-list-data/blob/main/text/GPL-3.0-or-later.txt); a opção de versões posteriores está indicada nos cabeçalhos dos scripts. Consulte também [a licença GNU original](https://www.gnu.org/licenses/gpl-3.0.html).
