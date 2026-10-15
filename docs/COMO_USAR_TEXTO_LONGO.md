# Como usar o Texto Longo no VozLab

Manual da Missão Texto Longo (Fase 11, outubro de 2026). Linguagem simples, sem precisar saber programar.
O mesmo conteúdo, resumido, fica na aba **Guia** da interface.

---

## 1. O que é o Texto Longo

Quando o texto é grande, o VozLab não manda tudo de uma vez para o modelo de voz. Ele:

1. **Divide o texto em blocos** de até cerca de 220 caracteres, cortando de preferência no fim de uma frase. Nunca corta depois de abreviações ("Sr.", "Dra.", "etc.") e nunca junta dois parágrafos no mesmo bloco.
2. **Gera e grava cada bloco separado.** Se algo der errado, os blocos prontos ficam salvos.
3. **Emenda os blocos** num áudio só, com pausas naturais e volume final de −19 LUFS (unidade de volume percebido).

O que os testes da Fase 10 provaram (5 páginas, Qwen 1.7B e Chatterbox):
- WER (taxa de erro de palavras) **menor** que antes da missão;
- **nenhum tempo a mais** de geração (Qwen: 2678 s contra 2797 s antes; Chatterbox: 2102 s contra 2384 s);
- servidor derrubado no bloco 34 de 102: a retomada **não refez nenhum** bloco pronto;
- com a opção desligada, o áudio sai **idêntico** ao de antes, amostra por amostra.

---

## 2. Gerar um texto longo

1. Abra o `VozLab.exe` (na pasta onde você instalou o VozLab). A tela abre sozinha no navegador.
2. Escolha a voz na coluna da esquerda.
3. Cole o texto (até 50.000 caracteres), confira **Idioma do texto** e **Modelo**.
4. Escolha as **Pausas entre frases** (seção 5) e, se quiser, o **Modo qualidade** (seção 6).
5. Clique em **✨ Gerar áudio** (ou `Ctrl+Enter` na caixa de texto).

Com 2 ou mais blocos, aparece o painel **Trabalho em blocos** embaixo do texto, com:
- **barra de progresso:** quantos blocos já ficaram prontos;
- **lista de blocos:** cada um com ▶ Ouvir bloco e ↻ Regerar este bloco;
- no fim, o áudio final entra no **Histórico** (coluna da direita).

Dica: em placa de vídeo de 8 GB, gere uma coisa de cada vez. Não use o OmniVoice ou jogos ao mesmo tempo.

---

## 3. Cancelar e retomar

- **■ Cancelar:** para **depois do bloco atual**. Os blocos prontos ficam salvos.
- **▶ Retomar:** gera **só os blocos que faltam**. Funciona também depois de:
  - fechar e abrir o VozLab;
  - o computador desligar ou o servidor cair no meio.

Para retomar um trabalho antigo, clique nele no Histórico. O painel abre com o botão ▶ Retomar.

### Caso raro (conhecido, anotado)
Se o servidor cair **depois do último bloco** e **antes** de montar o áudio final:
- o Histórico mostra "Interrompido: pode ser retomado";
- mas o painel abre como "Concluído", sem botão Retomar e sem áudio final.

**Como resolver:** abra **Remontar o áudio final** (seção 7) e clique em **Remontar áudio final**. Todos os blocos já estão gravados; em segundos o áudio final aparece. (Testado na Fase 10. Uma correção no painel fica para o polimento final do programa.)

---

## 4. Regerar um bloco

Se um trecho saiu estranho (palavra errada, sotaque escorregou, ruído):

1. No painel, abra a lista **Blocos** e clique em **▶ Ouvir bloco** para achar o trecho.
2. Clique em **↻ Regerar este bloco**.
3. Só aquele bloco é gerado de novo, com outra semente (número que muda a "interpretação" da voz). O resto **não muda**.
4. O áudio antigo do bloco fica **guardado**, não é apagado.
5. O áudio final é remontado com o bloco novo.

A marca **🎧** na lista indica um bloco que o Whisper achou suspeito (só aparece com o Modo qualidade ligado).

---

## 5. Ajustar as pausas

Antes de gerar, escolha em **Pausas entre frases (texto longo)**:

| Opção | Vírgula | Ponto | Parágrafo |
|---|---|---|---|
| Curtas | 90 ms | 180 ms | 450 ms |
| **Padrão** (escolhido por você na Fase 4) | 140 ms | 300 ms | 775 ms |
| Longas | 160 ms | 350 ms | 900 ms |

- Vale para textos com 2 ou mais blocos.
- Não muda a voz, só o silêncio entre os blocos.
- Já gerou e quer mudar? **Não precisa gerar de novo**: use **Remontar** (seção 7) com os números que quiser.

---

## 6. Modo qualidade

Painel recolhido, **tudo desligado por padrão**. As escolhas ficam guardadas neste computador.

| Chave | O que faz | Custo |
|---|---|---|
| **Revisar blocos** | O Whisper (programa que transcreve fala) ouve cada bloco logo depois de gerado e marca 🎧 os suspeitos. Não troca nada sozinho. | +9% a 18% de tempo |
| **Refazer sozinho** | Só com a revisão ligada. Bloco reprovado é gerado de novo até 2 vezes. Fica sempre a **melhor** versão, nunca uma pior. | depende de quantos blocos falham |
| **Voz com sotaque** | Para voz de um idioma lendo outro (ex.: voz francesa lendo português). O sotaque engana o Whisper; com esta chave ele **não refaz nada**, só marca o que foge muito do resto. A tela sugere ligar quando os idiomas são diferentes. | nenhum extra |
| **Nivelar volume** | Compressor suave no áudio final: **abaixa os trechos que ficaram altos demais**, deixando o volume mais uniforme. | segundos |

Observação honesta: o Whisper erra de 14% a 22% das palavras mesmo em áudio bom. Por isso ele compara cada bloco com os outros, e não com uma nota fixa. A marca 🎧 é um "ouça este", não uma certeza.

---

## 7. Remontar o áudio final (sem regerar a voz)

No painel do trabalho, abra **Remontar o áudio final**. Ele junta de novo os blocos já gravados. Usa só o processador e leva segundos.

| Campo | Padrão | O que muda |
|---|---|---|
| Pausa na vírgula | 140 ms | silêncio entre orações |
| Pausa no ponto | 300 ms | silêncio entre frases |
| Pausa no parágrafo | 775 ms | silêncio entre parágrafos |
| Volume final (LUFS) | −19 | −16 deixa mais alto (padrão de YouTube/podcast); −19 é mais limpo |
| Nivelar volume | como está | liga ou desliga o nivelador só nesta montagem |

Campos vazios usam o padrão. Dá para remontar quantas vezes quiser, comparando antes e depois.

---

## 8. Limpar temporários

Cada trabalho guarda os blocos em `%APPDATA%\sh.voicebox.app\jobs\<id do trabalho>\`.

- No painel do trabalho: **Limpar temporários deste trabalho** (pede confirmação).
- Em **Settings** você vê quanto espaço os temporários ocupam.

**Atenção:** depois de limpar, o áudio final **continua** no Histórico, mas **não dá mais** para regerar blocos nem remontar. Limpe só quando o áudio estiver aprovado.

Excluir uma geração no Histórico (✕) também apaga os blocos dela.

---

## 9. As opções do arquivo de configuração

Arquivo: `%APPDATA%\sh.voicebox.app\config\texto_longo.json`
(no Windows: `%APPDATA%\sh.voicebox.app\config\texto_longo.json`)

Na sua instalação ele está assim desde 13/10/2026:

```json
{
  "texto_longo_v2": true,
  "persistencia_jobs": true,
  "emendas_v2": true
}
```

| Opção | O que faz | Se ficar `false` |
|---|---|---|
| `texto_longo_v2` | **Chave mestra.** Divisor novo (respeita abreviações e parágrafos) e pipeline novo. | O VozLab funciona exatamente como antes da missão. As outras duas ficam sem efeito. |
| `persistencia_jobs` | Grava cada bloco no disco: permite Cancelar, Retomar, Regerar bloco e Remontar. | Sem painel de blocos; se cair, perde o que estava gerando. |
| `emendas_v2` | Pausas por tipo (vírgula/ponto/parágrafo), emendas suaves, volume em −19 LUFS e Nivelar. | Emendas como antes; pausas personalizadas são recusadas. |

Desde 14/10/2026 (`ATIVAR-V2-PADRAO`) as três vêm **`true` de fábrica** no código: quem não tiver esse arquivo já usa o Texto Longo novo. Para o modo antigo, é preciso escrever `false` no arquivo (seção 10).

Outras opções (avançado, raramente precisa mexer): `max_chunk_chars` (tamanho máximo do bloco, 220), `pausas_ms` e `volume.target_lufs`. Também dá para acrescentar abreviações próprias no mesmo arquivo.

Depois de editar o arquivo, **feche e abra o VozLab** para valer.

---

## 10. Como desligar o Texto Longo

Abra o `texto_longo.json` no Bloco de Notas e troque `"texto_longo_v2": true` por `"texto_longo_v2": false`. Salve. (Se o arquivo não existir, crie-o com o conteúdo `{"texto_longo_v2": false}`.)

**Atenção:** renomear ou apagar o arquivo **não** desliga mais. Desde 14/10/2026 o modo novo é o padrão de fábrica, então, sem o arquivo, o VozLab usa o Texto Longo novo.

Feche e abra o VozLab. A Fase 10 provou que, desligado, o áudio sai idêntico ao de antes da missão (6 de 6 frases curtas e 24 de 24 blocos longos, amostra por amostra).

Os trabalhos antigos e o Histórico **não são apagados** ao desligar.

---

## 11. Como voltar atrás no código (se algo sério acontecer)

O código anterior está guardado no Git (sistema que guarda versões do código):

| Nome | O que é |
|---|---|
| tag `antes-texto-longo` | o Voicebox **antes** da missão começar |
| tag `fase2-aprovada` | o ponto aprovado na Fase 2 (divisor novo) |
| branch `texto-longo` | todo o trabalho da missão, commit por commit |

Para voltar, peça ao Claude Code: `REVERTER FASE N` (desfaz uma fase) ou "voltar para a tag `antes-texto-longo`". Nada é apagado: a versão atual continua guardada na branch.

Antes de mexer no código, **experimente primeiro desligar a opção** (seção 10). Resolve quase tudo sem tocar em nada.

---

## 12. Problemas comuns

- **Parou no meio:** abra o trabalho no Histórico e clique **▶ Retomar**.
- **Painel diz "Concluído" mas não tem áudio final:** use **Remontar áudio final** (seção 3, caso raro).
- **Um trecho saiu estranho:** **↻ Regerar este bloco** (seção 4).
- **Volume baixo comparado a outros vídeos:** **Remontar** com −16 LUFS.
- **Voz levemente robotizada no Chatterbox:** é do próprio modelo, não do Texto Longo (confirmado na sua escuta da Fase 10).
- **Lento ou falta de memória:** feche o OmniVoice, jogos e outros programas de IA; use **Models → tirar da memória**.
