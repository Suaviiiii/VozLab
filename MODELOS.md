# Modelos de IA usados pelo VozLab e suas licenças

O VozLab **não inclui** nenhum modelo. Cada modelo é baixado da página **oficial** do autor no Hugging Face na primeira vez em que é usado, ou pelo botão "Baixar" da tela Models.

## Conferência tripla dos links (15/10/2026)
Cada link do código foi conferido de três formas:
1. a lista de repositórios no código (`backend/backends/__init__.py` e os `*_backend.py`);
2. a API do Hugging Face: o repositório existe, o `id` confere, o autor é a organização oficial, sem acesso restrito, e a licença foi lida na página do modelo;
3. a comparação com o Voicebox original (`jamiepine/voicebox`, `backend/backends/__init__.py`). O F5-TTS não existe no original: foi conferido direto na página do autor.

| Modelo | Repositório oficial | Autor | Licença dos pesos | Uso comercial? | Igual ao Voicebox original |
|---|---|---|---|---|---|
| Qwen3-TTS 1.7B / 0.6B (Base) | `Qwen/Qwen3-TTS-12Hz-1.7B-Base`, `...-0.6B-Base` | Qwen | Apache-2.0 | sim | sim |
| Qwen3-TTS Custom Voice 1.7B / 0.6B | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `...-0.6B-CustomVoice` | Qwen | Apache-2.0 | sim | sim |
| Chatterbox | `ResembleAI/chatterbox` | ResembleAI | MIT | sim | sim |
| Chatterbox Turbo | `ResembleAI/chatterbox-turbo` | ResembleAI | MIT | sim | sim |
| **OmniVoice** | `k2-fsa/OmniVoice` | k2-fsa | **CC-BY-NC** (o código é Apache-2.0; os pesos são CC-BY-NC por causa dos dados de treino) | **não** | sim |
| **F5-TTS** | `SWivid/F5-TTS` | SWivid | **CC-BY-NC-4.0** | **não** | não existe no original |
| LuxTTS | `YatharthS/LuxTTS` | YatharthS | Apache-2.0 | sim | sim |
| Kokoro | `hexgrad/Kokoro-82M` | hexgrad | Apache-2.0 | sim | sim |
| Whisper base/small/medium/large-v3 | `openai/whisper-*` | OpenAI | Apache-2.0 | sim | sim |
| Whisper large-v3-turbo | `openai/whisper-large-v3-turbo` | OpenAI | MIT | sim | sim |
| LLM Qwen3 0.6B / 1.7B / 4B | `Qwen/Qwen3-*` | Qwen | Apache-2.0 | sim | sim |

## O que "não comercial" quer dizer na prática
- **OmniVoice e F5-TTS** usam a licença CC-BY-NC: o áudio gerado por eles **não deve ser usado com fins comerciais** (por exemplo, vídeos monetizados, anúncios ou trabalhos pagos), salvo autorização dos autores.
- Para uso comercial, prefira os modelos Apache-2.0 ou MIT: Qwen3-TTS, Chatterbox, Kokoro e LuxTTS.
- Este texto **não é** aconselhamento jurídico. Leia a licença na página de cada modelo antes de usar.
- **Clonar a voz de alguém exige a permissão dessa pessoa.**
