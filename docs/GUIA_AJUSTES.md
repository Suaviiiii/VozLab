# Ajustes para o VozLab rodar melhor

## Antes de gerar
- **Feche os programas que usam a placa de vídeo:** jogos, editores de vídeo, outro gerador de IA e, se a placa for de 4–6 GB, até o navegador com aceleração de hardware.
- **Um modelo por vez:** o VozLab e o OmniVoice revezam a placa sozinhos. Não abra o painel antigo do OmniVoice com o VozLab aberto.
- **Voz de referência curta:** de 5 a 12 segundos é o ideal. Referências longas gastam muito mais memória (no OmniVoice, uma de 21 s chegou a 7,3 GB).

## Drivers
- **AMD:** Adrenalin **26.2.2 ou mais novo**, que é o exigido pelo PyTorch ROCm 7.2.1.
- **NVIDIA RTX 3000:** o driver Game Ready ou Studio mais recente.
- **NVIDIA GTX 1000:** o driver **580**, o último dessas placas.

## Windows
- **Memória virtual (arquivo de paginação):** deixe "gerenciado pelo sistema". Quando a placa enche, o Windows usa a RAM do PC, e ela precisa ter espaço.
- **Plano de energia:** "Alto desempenho" ou "Desempenho máximo" enquanto gera textos longos.
- **Agendamento de GPU acelerado por hardware** (Configurações → Sistema → Tela → Gráficos): pode ajudar ou não; teste com e sem.
- **Espaço em disco:** os modelos ocupam de 0,3 a 4,3 GB cada. Deixe a pasta de modelos num SSD.

## Dentro do VozLab
- **Texto longo:** blocos menores gastam menos memória. Se um bloco falhar por falta de memória, use o **Retomar**: ele continua de onde parou.
- **Revisão com o Whisper:** ligue só se precisar. Ela gasta mais ~0,5 a 1 GB.
- **"Carregar na placa agora":** troca o modelo antes de você gerar, e a 1ª geração não espera.
- **"MP3 junto":** ligado, todo download vem também em MP3. Desligue se não precisar.

## Quando der errado
1. Feche e abra o VozLab: isso libera a memória presa na placa.
2. Veja na tela Models se o modelo está "não utilizável nesta placa".
3. Peça ajuda a uma IA seguindo o [GUIA_PARA_IA.md](GUIA_PARA_IA.md).
