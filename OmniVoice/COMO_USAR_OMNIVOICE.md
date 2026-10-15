# Como usar o OmniVoice (com o VozLab)

> Manual reescrito em 14/10/2026 no fim da Missão OmniVoice (fase OV-8). Ele descreve **só o que existe e foi testado**.

## 1. O que é

O **OmniVoice** é um modelo de voz em **mais de 600 idiomas**, com 0,6B (600 milhões de parâmetros). Ele faz três coisas:
- **clona** uma voz a partir de um áudio curto (3 a 10 s é o ideal);
- **cria** uma voz nova a partir de uma descrição (gênero, idade, tom, estilo);
- fala com uma **voz automática** própria.

Ele roda num **programa à parte**, o serviço OmniVoice, com o Python dele (`OmniVoice\.venv`), separado do VozLab. Os dois funcionam **juntos e separados**.

## 2. Os dois jeitos de usar

| Jeito | Quando | Onde |
|---|---|---|
| **Pelo VozLab (o normal)** | sempre que o VozLab estiver aberto | aba **OmniVoice 🌍**, ou **Modelo = OmniVoice 0.6B** na tela Generate |
| **Modo de emergência** | com o VozLab **fechado** | `INICIAR_OMNIVOICE_WEBUI.bat`, que abre `http://127.0.0.1:17495/painel` |

### 2.1 Aba OmniVoice do VozLab (o painel completo)

1. **Voz**, escolhendo um dos três modos:
   - **Clonar voz**, com três origens:
     - **Biblioteca**: todas as vozes do VozLab, com busca e filtro por idioma;
     - **Catálogo**: a pasta `OmniVoice\catalogo_vozes`;
     - **Meu arquivo**: um áudio seu. Ele **não** entra na biblioteca; fica em `<dados do VozLab>\cache\omnivoice_referencias_avulsas`.

     O **texto da referência** (o que é falado no áudio) é opcional. Vazio, o OmniVoice transcreve sozinho, o que usa ~1,6 GB a mais da placa na 1ª vez.
   - **Criar voz**: gênero, idade, tom e estilo. O **sotaque só funciona com fala em inglês** (é assim que o modelo funciona).
   - **Voz automática**: sem áudio nem descrição.
2. **Texto**: o idioma (ou Automático) e as **Pausas entre frases**:
   - **Curtas / Padrão / Longas**: o VozLab divide o texto em blocos de ~220 caracteres, gera cada um, põe a pausa entre eles e iguala o volume em −19 LUFS (volume percebido). São as **mesmas pausas** da tela Generate.
   - **Do modelo**: o texto vai inteiro, e o OmniVoice divide e decide as pausas sozinho.
3. **Gerar.** A tela espera o tempo que for preciso, mostra o andamento ("bloco 2 de 5 · 47 s") e tem **Cancelar**. A fila fica livre em até ~3 s.
4. **Nesta sessão:** os áudios gerados, até fechar o VozLab. Baixe os que quiser guardar.

Na aba **Vozes** do VozLab, o botão **Usar no OmniVoice** abre esta aba com a voz já escolhida.

### 2.2 Modelo = OmniVoice 0.6B (tela Generate)

O OmniVoice funciona como os outros modelos: usa a voz da biblioteca e o áudio vai para o **histórico**. Ele vale também para o **texto longo**:
- blocos com semente;
- pausas e −19 LUFS;
- retomar depois de uma queda, refazer um bloco e cancelar dentro do bloco;
- **modo qualidade** (o Whisper confere cada bloco).

Os **Ajustes do OmniVoice** (passos e CFG) ficam recolhidos logo abaixo do Modelo.

### 2.3 Modo de emergência (VozLab fechado)

O `INICIAR_OMNIVOICE_WEBUI.bat` liga o serviço, se for preciso, e abre o painel próprio em **português**:
- clonar da biblioteca do VozLab (**só leitura**), do catálogo ou de um arquivo;
- criar voz;
- todos os ajustes.

**Não tem** as pausas do VozLab (elas dependem dele). O painel original da k2-fsa, em inglês e chinês, está guardado: para vê-lo, ponha `"montar_painel_original": true` em `config_servico.json`, e ele aparece em `/painel-original`.

## 3. Ligar, desligar e a placa de vídeo

- **Ligar:**
  - o VozLab liga o serviço sozinho ao gerar (~15 s);
  - na aba OmniVoice há o botão **Ligar o OmniVoice**;
  - fora do VozLab, use `iniciar_omnivoice_service.bat` ou `INICIAR_OMNIVOICE_WEBUI.bat`.
- **Desligar tudo:** `DESLIGAR_OMNIVOICE_SERVICE.bat` fecha o serviço, o processo que carrega o modelo e o painel antigo da porta 7860, se ele estiver aberto.
- **O modelo e a placa:**
  - o modelo só entra na placa na **1ª geração**;
  - **Liberar placa** (na aba) tira o modelo, e a placa volta a **0 MB**;
  - se você esquecer, ele **sai sozinho depois de 10 minutos parado** (`"inatividade_min"` em `config_servico.json`; 0 desliga).
- **Revezamento com o VozLab:**
  - os dois **nunca usam a placa ao mesmo tempo para gerar voz**: quem chega depois espera, e nenhuma geração é cortada;
  - um texto longo do VozLab segura a placa até terminar;
  - uma linha pequena na tela avisa quando a placa está com o outro programa.
- **Memória medida** (RX 7600, 8 GB): uma cópia do modelo ocupa ~2 GB; gerando, ~3,4 a 3,8 GB. O OmniVoice e o Whisper do modo qualidade juntos chegaram a 7,2 GB.
- **Outros programas na placa** (ex.: Rescript, Transcritor, jogos, que chegam a 3,7 GB) deixam tudo mais lento. Feche-os para gerar textos grandes.

## 4. Ajustes (o que cada um faz)

| Ajuste | Padrão | O que faz |
|---|---|---|
| Passos de difusão | 16 (32 no modo de emergência) | mais passos = som mais polido e mais lento |
| CFG | 2,0 | o quanto segue a voz e o texto; alto demais soa tenso |
| Velocidade | 1,0 | mais rápido (>1) ou mais lento (<1); ignorada se houver Duração |
| Duração | automática | força o áudio a durar X segundos |
| **Semente** | aleatória | **mesmo número + mesmo texto = mesmo áudio** (testado amostra por amostra) |
| Reduzir ruído | ligado | limpa o ruído de fundo |
| Preparar a referência | ligado | tira silêncios do áudio da voz |
| Tirar silêncios longos | ligado | no resultado |
| t_shift | 0,1 | onde o modelo gasta os passos (avançado) |
| Temperatura da posição | 5 | variedade na ordem de montagem do som (avançado) |
| Temperatura do som | 0 | 0 = mais estável; mais alto = mais variado (avançado) |
| Penalidade por camada | 5 | equilíbrio entre as camadas de som (avançado) |
| Divisão interna | 15 s a partir de 30 s | só com as pausas "Do modelo" (avançado) |

## 5. Catálogo de vozes (`OmniVoice\catalogo_vozes`)

- Ponha o áudio (de preferência **.wav**, de 3 a 10 s) nesta pasta, com um nome descritivo.
- Opcional: um `.txt` com **o mesmo nome**, contendo o que é falado, evita a transcrição automática.
- No VozLab, a voz aparece na origem **Catálogo** da aba OmniVoice. No modo de emergência, clique em **Atualizar listas de vozes**.

## 6. Problemas comuns

| O que aparece | O que fazer |
|---|---|
| "Desligado" na aba | clique em **Ligar o OmniVoice** ou só gere: ele liga sozinho. Registro em `<dados do VozLab>\logs\omnivoice_service.log` |
| "Aguardando o VozLab terminar…" | o VozLab está gerando; o OmniVoice começa quando ele acabar |
| A 1ª geração demora | o modelo está carregando (~15 s) e a voz está sendo preparada pela 1ª vez. Depois fica no cache (`cache_prompts`, reconhecido pelo **conteúdo** do áudio). Tamanhos de áudio novos também são mais lentos na 1ª vez |
| Voz com mais de 12 s | o OmniVoice usa uma cópia dos primeiros ~10 s (a original não muda). Referências longas chegavam a 7,3 GB de placa |
| Números e horários lidos errado ("08h45") | escreva por extenso no texto ("oito e quarenta e cinco") |

## 7. Esconder o OmniVoice do VozLab

- **Settings → Painel OmniVoice → Esconder painel OmniVoice** (vale só para a tela).
- Para nem registrar as rotas, inicie o VozLab com a variável de ambiente `VOICEBOX_DISABLE_OMNIVOICE=1`.

## 8. Para quem for mexer nos arquivos

| Arquivo | Para quê |
|---|---|
| `omnivoice_service.py` | o serviço (porta 17495): fila única, árbitro da placa, rotas antigas (`/health`, `/generate`, `/status`, `/audio`, `/cancel`, `/load`, `/unload`, `/models`, `/modes`) e novas (`/v2/gerar_bloco`, `/v2/cancelar`, `/v2/info`, `/gpu/estado`, `/gpu/ceder`) |
| `omnivoice_trabalhador.py` | o processo filho, o **único** que carrega o modelo na placa |
| `omnivoice_painel_ptbr.py` | o painel do modo de emergência (`/painel`) |
| `config_servico.json` | tempo de inatividade, endereço do VozLab, espera máxima e painéis |
| `omnivoice/models/omnivoice.py` | o modelo; foi alterado só em **4 linhas** (a verificação de cancelamento entre passos) |

- **Versões:** torch 2.9.1+rocm7.2.1, transformers 5.19, omnivoice 0.2.1 e `HSA_OVERRIDE_GFX_VERSION=11.0.0`. **Não atualize pacotes sem um backup do `.venv`.**
