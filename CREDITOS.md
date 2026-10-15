# Créditos e licenças de terceiros

O **VozLab** é um "mod" gratuito que junta e adapta dois projetos de código aberto. Todo o crédito do trabalho original vai para os autores deles.

| Projeto | O que o VozLab usa | Licença | Endereço |
|---|---|---|---|
| **Voicebox** (Jamie Pine e colaboradores) | Base do servidor e dos motores de voz, modificada (interface nova em português, texto longo, perfis de placa, MP3 junto etc.) | MIT. O aviso original está em `LICENSE` | https://github.com/jamiepine/voicebox |
| **OmniVoice** (k2-fsa) | Pasta `OmniVoice/`, modificada (serviço HTTP, trabalhador que libera a placa, painel em português) | Apache-2.0 (`OmniVoice/LICENSE`) | https://github.com/k2-fsa/OmniVoice |

## Bibliotecas de vozes (amostras de referência)
Elas são distribuídas à parte da página de Releases, e não dentro do código:

| Base | Licença | O que exige | Endereço |
|---|---|---|---|
| Google **FLEURS** | CC-BY 4.0 | dar o crédito | https://huggingface.co/datasets/google/fleurs |
| **Open-Swara** | CC-BY-SA 4.0 | dar o crédito e manter a mesma licença | https://huggingface.co/datasets/jaymunshi/open-swara |

Vozes sem origem registrada **não** são publicadas.

## Modelos de IA
Veja `MODELOS.md`: os modelos não vêm no repositório e alguns são "não comercial".

## Mudanças principais em relação aos originais
- Interface nova em português brasileiro (`frontend/novo`) e o lançador `VozLab.exe` (`vozlab/`).
- Texto longo com blocos, retomada, emendas e revisão pelo Whisper.
- VozLab e OmniVoice ligados por um árbitro de placa de vídeo (um usa a placa por vez, sem cortar geração).
- Perfil por família de placa de vídeo (AMD RX 6000/7000, NVIDIA GTX 1000 de 4 GB e RTX 3000), com avisos de quais modelos cabem.
- "MP3 junto": todo áudio baixado pode vir também em MP3.
