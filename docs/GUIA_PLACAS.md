# Guia por família de placa de vídeo

O VozLab foi feito para rodar na **placa de vídeo**. Ao abrir, ele descobre a placa e a memória dela (VRAM) e mostra, no seletor "Modelo" e na tela Models, o que cabe:
- sem marca: roda bem;
- **⚠**: roda, com aviso;
- **"(não testado nesta placa)"**: nunca foi medido nessa placa;
- **"— não utilizável nesta placa"**: falta memória.

Os avisos só informam: nada é bloqueado.

## Como os números foram medidos
- Numa **AMD RX 7600 de 8 GB** (Windows 11, ROCm 7.2.1, PyTorch 2.9.1), em 15/10/2026.
- As placas menores foram **simuladas** de dois jeitos:
  - limitando a memória do PyTorch a 4 e 6 GB;
  - ocupando a placa com um programa à parte até sobrar 3,6 GB.

  A simulação mostra **o que cabe**, não a velocidade de outras placas.
- O limite de memória **não muda a voz**: os áudios saíram idênticos, amostra por amostra.

## Memória usada (pico, gerando)
| Modelo | Carregado | Pico (blocos curtos) | Pico (texto longo) |
|---|---|---|---|
| Qwen3-TTS 1.7B | 4,0 GB | 5,8 GB | 6,6 GB |
| Qwen3-TTS 0.6B | 2,1 GB | 4,0 GB | 4,8 GB |
| Qwen Custom Voice 1.7B | 4,0 GB | 5,8 GB | não medido |
| Qwen Custom Voice 0.6B | 2,1 GB | 3,7 GB | não medido |
| Chatterbox | 3,1 GB | 4,3 GB | 4,6 GB |
| OmniVoice (float16) | ~1,9 GB | 3,2–3,4 GB | depende da voz de referência (use até ~10 s) |
| OmniVoice (float32) | — | 4,5–4,7 GB | — |
| Kokoro | 0,3 GB | 1,5 GB | — |
| Whisper-Small (revisão) | 0,5 GB | 0,9 GB | — |

O Windows e o "motor" da placa usam mais ~0,5 a 1 GB. Depois de descarregar um modelo, o VozLab ainda segura ~1,3 a 2 GB: é o motor do ROCm, liberado quando o VozLab fecha.

## O que cada família roda
| Família | Placas comuns | Situação | Modelos |
|---|---|---|---|
| **AMD RX 7000** | RX 7600 8 GB | **testada** | os 6 principais; os 1.7B transbordam ~1 GB para a RAM (mais lento) |
| **AMD RX 6000** (8 GB) | RX 6600/6650 XT | simulada | os 6 principais |
| **NVIDIA RTX 3000** (8 GB) | RTX 3050 8 GB, 3060 Ti, 3070 | simulada | os 6 principais |
| **6 GB** (qualquer família) | RTX 3050 6 GB | simulada | 0.6B, Chatterbox e OmniVoice ok; **1.7B com aviso** (pode ficar lento ou falhar em blocos longos) |
| **4 GB AMD** | RX 6500 XT, RX 6400 | simulada | só Custom Voice 0.6B e OmniVoice, apertados (voz de referência curta, sem outros programas na placa) |
| **NVIDIA GTX 1000** (4 GB) | GTX 1050 Ti | **não testada** | **só o OmniVoice**, em float16 e mais lento. Em float32 (o único modo dos outros nessa placa), nenhum dos outros cabe |
| Fora das famílias | RX 9000, RTX 4000, outras | sem garantia | podem funcionar; o VozLab avisa |
| Sem placa compatível | — | não suportado | o VozLab avisa que precisa de uma placa de vídeo |

## Dicas por família
- **AMD:** o VozLab escolhe sozinho o "disfarce" do ROCm (`HSA_OVERRIDE_GFX_VERSION`): RX 7000 = 11.0.0 e RX 6000 = 10.3.0. Para forçar outro, use `HsaOverride` no `vozlab.ini`.
- **OmniVoice:** a 1ª geração de cada tamanho de áudio é mais lenta (a placa "aprende" a forma de calcular e guarda em disco). O botão "Carregar na placa agora" já aquece o modelo.
- **GTX 1000:** a NVIDIA parou os drivers novos dessas placas no driver 580, e o PyTorch para de lançar versões para elas depois da 2.14. Use as versões fixas do `INSTALAR.md`.
