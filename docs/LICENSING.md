# Licenciamento e uso não comercial

O código de gerenciamento desta render farm é oferecido sob a **PolyForm Noncommercial License 1.0.0**. O texto integral e sem modificações está em [LICENSE](../LICENSE). O titular identificado no projeto é **Vinicius D'Arcadia Cruz**. [NOTICE](../NOTICE) delimita o escopo e as exceções.

Este guia explica a intenção de uso; os textos das licenças aplicáveis prevalecem. Ele não substitui uma análise jurídica da sua situação.

## O que a licença do gerenciador permite

Você pode estudar, executar, modificar, integrar e redistribuir o código de gerenciamento para finalidades permitidas não comerciais, preservando os termos e os avisos exigidos. Isso inclui projetos pessoais e experimentação sem aplicação comercial antecipada.

| Exemplo de uso do gerenciador | Situação |
| --- | --- |
| Estudar o código ou aprender sobre renderização distribuída | Permitido para finalidade não comercial |
| Montar uma farm doméstica para hobby | Permitido para finalidade não comercial |
| Adaptar a interface ou integrar um cliente próprio para pesquisa não comercial | Permitido, respeitando os avisos e a licença |
| Compartilhar um fork para usos permitidos | Permitido, com os termos e avisos exigidos |
| Operar uma render farm comercial baseada neste código | Não autorizado pela licença do gerenciador |
| Cobrar por renderização usando este gerenciador | Não autorizado pela licença do gerenciador |
| Vender acesso, assinaturas ou um serviço hospedado baseado no gerenciador | Não autorizado pela licença do gerenciador |
| Usar o gerenciador na produção comercial de um estúdio ou em trabalhos pagos | Não autorizado pela licença do gerenciador |

A licença padrão também define usos permitidos para organizações beneficentes, instituições de ensino, pesquisa pública, saúde/segurança pública, proteção ambiental e instituições governamentais, independentemente das fontes de financiamento ou obrigações decorrentes dele. Leia a seção **Noncommercial Organizations** antes de aplicar os exemplos a essas instituições.

Remover o nome do projeto, mudar a interface ou redistribuir uma versão modificada não elimina as condições do código utilizado. Um uso comercial fora das permissões da licença exige autorização separada dos titulares pertinentes; este repositório não concede essa autorização.

## Exceção dos scripts executados dentro do Blender

Os seguintes arquivos, e os demais materiais de sua pasta, usam **GPL-3.0-or-later**:

- `worker/blender_render_farm_worker/blender_scripts/blender_session_bootstrap.py`
- `worker/blender_render_farm_worker/blender_scripts/device_override.py`

O texto integral está em [LICENSES/GPL-3.0-or-later.txt](../LICENSES/GPL-3.0-or-later.txt) e em `blender_scripts/COPYING`.

Esses programas usam `bpy` e rodam dentro de um processo Blender. A [documentação oficial do Blender](https://www.blender.org/about/license/) exige licenciamento compatível com a GPL para scripts publicados que utilizam sua API Python.

**A GPL permite uso comercial desses scripts isoladamente**, respeitadas suas próprias condições de distribuição, código-fonte e avisos. A restrição não comercial não se aplica a eles nem a alterações cobertas pela GPL. Ela continua sendo a licença oferecida para o código separado do gerenciador; a exceção não autoriza copiar o gerenciador para uma render farm comercial.

O Worker externo não importa `bpy` nem importa esses scripts como módulos. Ele inicia o Blender por linha de comando, passa parâmetros e, no modo persistente, troca mensagens por um canal local. Essa é a separação técnica adotada nesta publicação. A classificação jurídica de uma integração depende também do conteúdo da comunicação e das alterações realizadas, não só de existir um subprocesso; consulte a [FAQ da GNU sobre agregação](https://www.gnu.org/licenses/gpl-faq.html#MereAggregation) antes de criar combinações diferentes.

## Dependências, ícones e marca

Blender, Python, Qt/PySide6 e as outras bibliotecas conservam suas licenças. Os ícones Lucide/Feather e a marca Blender também não são relicenciados como não comerciais por este projeto. Veja [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

Ao distribuir executáveis que incluam bibliotecas ou scripts de terceiros, cumpra as obrigações de cada componente. A existência do `LICENSE` da raiz não substitui avisos, fontes ou permissões exigidos pelas outras licenças.

## É software livre ou open source?

O gerenciador tem **código disponível para uso não comercial** (*source available*). A restrição comercial não é compatível com a [definição de open source da OSI](https://opensource.org/osd), que exige liberdade de uso em qualquer campo de atividade. Evite descrevê-lo como MIT, Apache, GPL integral ou software livre sem restrições.

## E os arquivos renderizados?

A licença do gerenciador não reivindica a titularidade de cenas, texturas ou imagens produzidas pelo usuário. Isso não autoriza utilizar o gerenciador para uma finalidade comercial que sua licença não permita. Os direitos sobre conteúdo e recursos externos continuam pertencendo aos respectivos titulares.
