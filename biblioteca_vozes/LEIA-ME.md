# Biblioteca nativa de vozes

As vozes de referência do VozLab vêm em **pacotes .zip**, um por idioma. Elas ficam na página de **Releases** do GitHub, e não no código, porque somam algumas centenas de MB.

## Como instalar
1. Baixe o pacote do idioma que quiser na página de Releases.
2. No VozLab: **Vozes → Importar vozes** e escolha o `.zip`.
3. Revise a prévia e importe. As vozes que você já tem nunca são alteradas.

## De onde vêm as vozes (e as licenças)
| Pacote | Base | Licença | Crédito |
|---|---|---|---|
| `vozes_fleurs_<idioma>.zip` | Google FLEURS | CC-BY 4.0 | Conneau et al., "FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech" — https://huggingface.co/datasets/google/fleurs |
| `vozes_open_swara.zip` | Open-Swara | CC-BY-SA 4.0 (os áudios continuam sob a mesma licença) | https://huggingface.co/datasets/jaymunshi/open-swara |

- Cada voz traz na descrição a base de onde veio.
- Vozes sem origem registrada **não** são distribuídas.
- **Use as vozes com responsabilidade.** Elas servem de referência para clonar o timbre. Não use para se passar por outra pessoa.
