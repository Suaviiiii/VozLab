# Pedindo ajuda a uma IA (ChatGPT, Claude, Gemini…) sem perder as suas vozes

Copie o bloco abaixo **no começo** da conversa com a IA e depois descreva o problema.

```text
Estou usando o VozLab (versão modificada do Voicebox + OmniVoice) no Windows.
Regras de segurança OBRIGATÓRIAS para qualquer comando ou passo que você me sugerir:
1. NUNCA apagar, mover ou sobrescrever a pasta %APPDATA%\sh.voicebox.app (lá ficam minhas vozes,
   o histórico e o banco voicebox.db). Antes de mexer em qualquer coisa ali, me peça para fazer uma cópia.
2. NUNCA apagar a pasta de modelos (ModelosDir no vozlab.ini). Baixar de novo custa muitos GB.
3. NUNCA rodar "pip uninstall", "pip install --upgrade" ou trocar a versão do torch sem me explicar antes.
   O VozLab usa versões FIXAS por família de placa (requirements/ e INSTALAR.md).
4. Preferir sempre: ler arquivos de registro, mostrar o comando e explicar o que ele faz antes de eu rodar.
5. Explique em português simples; eu não sou programador.

Informações do meu PC:
- Placa de vídeo (Gerenciador de Tarefas → Desempenho → GPU): ...
- O que a tela Models do VozLab mostra em "Sua placa": ...
- O que eu fiz e o que aconteceu: ...
- Trecho do registro %APPDATA%\sh.voicebox.app\logs\vozlab.log (as últimas 40 linhas): ...
```

## Onde estão as coisas
| O quê | Onde |
|---|---|
| Vozes, histórico, banco | `%APPDATA%\sh.voicebox.app` (**faça cópia antes de qualquer conserto**) |
| Registro do VozLab | `%APPDATA%\sh.voicebox.app\logs\vozlab.log` |
| Registro do OmniVoice | `%APPDATA%\sh.voicebox.app\logs\omnivoice_service.log` |
| Configuração do lançador | `vozlab.ini`, ao lado do `VozLab.exe` |
| Modelos | a pasta `ModelosDir` do `vozlab.ini` |
| Situação da placa | `http://127.0.0.1:17493/gpu/perfil`, com o VozLab aberto |

## Fazer uma cópia das vozes (antes de qualquer conserto)
Feche o VozLab e copie a pasta inteira `%APPDATA%\sh.voicebox.app` para outro lugar (por exemplo, um pendrive).
