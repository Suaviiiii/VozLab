# Instalar o VozLab (Windows 10/11)

> [!CAUTION]
> **⛔ Este programa NÃO foi feito para rodar no processador (CPU)! Se você não tem placa de vídeo compatível, NÃO USE.**
> Sem placa, tudo fica extremamente lento e o PC pode travar. Antes de instalar, confira a sua placa no passo 1.

> **Estado:** só a família **AMD RX 7000** foi testada (numa RX 7600, Windows 11). As outras famílias estão marcadas **NÃO TESTADO**: se você instalar numa delas, conte como foi abrindo uma *issue*. Isso ajuda muito.

## 1. Descubra a sua família de placa
Gerenciador de Tarefas → Desempenho → GPU: veja o nome da placa.

| Placa | Família | Arquivo do PyTorch | Situação |
|---|---|---|---|
| AMD RX 7600, 7700, 7800, 7900 | AMD RX 7000 | `requirements/torch_amd_rx7000.txt` | **testado** |
| AMD RX 6500 XT, 6600, 6700, 6800, 6900 | AMD RX 6000 | `requirements/torch_amd_rx6000.txt` | NÃO TESTADO |
| NVIDIA RTX 3050, 3060, 3070 | NVIDIA RTX 3000 | `requirements/torch_nvidia_rtx3000.txt` | NÃO TESTADO |
| NVIDIA GTX 1050 Ti, 1060, 1070 | NVIDIA GTX 1000 | `requirements/torch_nvidia_gtx1000.txt` | NÃO TESTADO (só o OmniVoice) |

Outras placas podem funcionar, mas sem garantia. Sem placa compatível, o VozLab avisa e fica muito lento.

## 2. Pré-requisitos
- **Python 3.12** (https://www.python.org/downloads/), com a opção "Add to PATH" marcada.
- O **driver** mais recente da placa (AMD Adrenalin ou NVIDIA Game Ready/Studio). As GTX 1000 param no driver 580.
- Espaço em disco: ~10 GB para os ambientes Python, mais os modelos (de 0,3 a 4,3 GB cada).

## 3. Ambiente do VozLab
Abra o **Prompt de Comando** na pasta do VozLab:
```bat
py -3.12 -m venv venv_rocm
venv_rocm\Scripts\python.exe -m pip install --upgrade pip
```
**PyTorch da sua família** (escolha UMA linha):
```bat
:: AMD RX 7000 (testado): arquivos oficiais da AMD, em DUAS etapas (driver 26.2.2 ou mais novo)
venv_rocm\Scripts\python.exe -m pip install --no-cache-dir "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl" "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl" "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl" "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm-7.2.1.tar.gz"
venv_rocm\Scripts\python.exe -m pip install --no-cache-dir "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl" "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl"
:: AMD RX 6000 (NÃO TESTADO): pacote AMD TheRock; troque gfx103x pelo código da placa (ex.: gfx1032 = RX 6600)
venv_rocm\Scripts\python.exe -m pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ "torch[device-gfx103x]" torchaudio
:: NVIDIA RTX 3000 (NÃO TESTADO)
venv_rocm\Scripts\python.exe -m pip install -r requirements\torch_nvidia_rtx3000.txt --index-url https://download.pytorch.org/whl/cu128
:: NVIDIA GTX 1000 (NÃO TESTADO)
venv_rocm\Scripts\python.exe -m pip install -r requirements\torch_nvidia_gtx1000.txt --index-url https://download.pytorch.org/whl/cu126
```
> ⚠️ Os endereços dos pacotes mudam com o tempo. Confira na fonte oficial antes: [PyTorch](https://pytorch.org/get-started/locally/), [AMD ROCm para Windows](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/windows/windows_compatibility.html) e [AMD TheRock](https://github.com/ROCm/TheRock/blob/main/RELEASES.md).

O resto dos pacotes, com versões fixas:
```bat
venv_rocm\Scripts\python.exe -m pip install -r requirements\base.txt
```
> Mesmo numa placa NVIDIA, o nome da pasta continua `venv_rocm`, porque é o que o lançador procura. Para trocar, use `PythonDir` no `vozlab.ini`.

## 4. Ambiente do OmniVoice (obrigatório para usar o OmniVoice)
> [!IMPORTANT]
> **NVIDIA GTX 1000 (1050 Ti, 1060, 1070): este passo é OBRIGATÓRIO**, porque nessa placa o OmniVoice é o único modelo.
> Nas outras placas, sem este passo o VozLab funciona, mas o OmniVoice não liga.
> O OmniVoice precisa de um ambiente Python **só dele** (a pasta `OmniVoice\.venv`), com o `transformers` 5 ou mais novo. O `venv_rocm` do passo 3 usa o 4.x e **não serve** para ele.

```bat
cd OmniVoice
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
```
**PyTorch da sua família, de novo, agora no `.venv`.** É a mesma linha do passo 3, trocando `venv_rocm` por `.venv` e `requirements\` por `..\requirements\`. Exemplo na GTX 1000:
```bat
.venv\Scripts\python.exe -m pip install -r ..\requirements\torch_nvidia_gtx1000.txt --index-url https://download.pytorch.org/whl/cu126
```
Depois, o OmniVoice e o resto dos pacotes dele:
```bat
.venv\Scripts\python.exe -m pip install -e .
cd ..
```
Se a pasta `.venv` faltar ou estiver incompleta, a janela "OmniVoice Service" avisa com **[ERRO]** e diz o que fazer.

## 5. Lançador e configuração
```bat
vozlab\compilar_vozlab.bat
copy vozlab\vozlab.ini.exemplo vozlab.ini
```
No `vozlab.ini`, ajuste `ModelosDir`, que é onde os modelos serão guardados. O padrão é a pasta `modelos` ao lado do `VozLab.exe`.

## 6. Primeiro uso
1. Abra o **VozLab.exe**. A tela abre numa janela própria.
2. Na tela **Models**, veja "Sua placa: …" e quais modelos cabem.
3. Escolha uma voz, escreva o texto e clique **Gerar áudio**. Na primeira vez, o modelo é baixado da página oficial (veja [MODELOS.md](MODELOS.md)).
4. Biblioteca de vozes: baixe os pacotes na página de Releases e use **Vozes → Importar vozes**.
