# Levantamento de Cortes em Tokens Suspeitos de Abreviação (Caça a Lacunas)

**Data:** 11 de outubro de 2026  
**Objetivo:** Identificar todo corte nas 12 saídas em que o último token tem até 5 letras, começa com maiúscula e termina em ponto, e o bloco seguinte começa com maiúscula.  
**Status:** Somente listagem analítica para revisão do usuário (sem alteração de código além de `Univ.` já corrigido no item 1).  

---

## 1. Resumo Quantitativo

- **Total de Ocorrências Encontradas:** 6 cortes nas 12 saídas.
- **Tokens Únicos Distintos:** 3 (`Hugo., Maria., Paris.`).

### Avaliação Preliminar dos Tokens Encontrados

1. **`Maria.`** — Nome próprio em fim de frase de diálogo (`"Talvez", disse a Sra. Maria.`). Encerra frase legitimamente antes de `O Sr. João, porém, discordou.`. Não é abreviação.
2. **`Paris.`** — Nome de cidade em fim de frase descritiva (`...rue de la Paix, 75002 Paris.`). Encerra frase legitimamente antes de `Ils sont arrivés à 8 h 15...`. Não é abreviação.
3. **`Hugo.`** — Parte final de nome composto (`Victor-Hugo.`). Encerra frase legitimamente antes de `Puis ils sont partis.`. Não é abreviação.
4. *(Caso Histórico)* **`Univ.`** — Ocorrência em `saida_modo1_v2_unpack_tc02.txt` e `saida_modoA_v2_unpack_tc02.txt` (`...diretora da Univ.` | `Federal, abriu...`) **era um falso término de frase (corte inaceitável)**. Foi formalmente corrigido no Item 1 da Fase 2f com a adição de `univ.` à Classe A de abreviações do português.

---

## 2. Lista Completa de Ocorrências (50 caracteres antes e depois)

| # | Arquivo de Saída | Corte | Token Final | 50 Caracteres Antes do Corte | 50 Caracteres Depois do Corte | Natureza do Corte |
| :---: | :--- | :---: | :---: | :--- | :--- | :--- |
| 1 | `saida_modo1_v2_unpack_tc01.txt` | 21 -> 22 | `Hugo.` | ` à 18 h 30, c.-à-d. en retard, avenue Victor-Hugo.` | `Puis ils sont partis.` | Fim legítimo de frase (Nome próprio / Topônimo) |
| 2 | `saida_modo2_v2_unpack_tc01.txt` | 21 -> 22 | `Hugo.` | ` à 18 h 30, c.-à-d. en retard, avenue Victor-Hugo.` | `Puis ils sont partis.` | Fim legítimo de frase (Nome próprio / Topônimo) |
| 3 | `saida_modo1_v2_unpack_tc02.txt` | 12 -> 13 | `Maria.` | `"Talvez", disse a Sra. Maria.` | `O Sr. João, porém, discordou.` | Fim legítimo de frase (Nome próprio / Topônimo) |
| 4 | `saida_modo1_v2_unpack_tc02.txt` | 36 -> 37 | `Paris.` | `fèvre habitent au 12, rue de la Paix, 75002 Paris.` | `Ils sont arrivés à 8 h 15, p. ex. avant les autres` | Fim legítimo de frase (Nome próprio / Topônimo) |
| 5 | `saida_modoA_v2_unpack_tc02.txt` | 12 -> 13 | `Maria.` | `"Talvez", disse a Sra. Maria.` | `O Sr. João, porém, discordou.` | Fim legítimo de frase (Nome próprio / Topônimo) |
| 6 | `saida_modoA_v2_unpack_tc02.txt` | 36 -> 37 | `Paris.` | `fèvre habitent au 12, rue de la Paix, 75002 Paris.` | `Ils sont arrivés à 8 h 15, p. ex. avant les autres` | Fim legítimo de frase (Nome próprio / Topônimo) |

---

## 3. Detalhamento por Caso

### Token `Hugo.` (2 ocorrências)

- **Arquivo:** `saida_modo1_v2_unpack_tc01.txt` (Corte 21 -> 22)
  - **Texto Anterior Completo:** `M. Dupont et Mme Martin sont arrivés à 18 h 30, c.-à-d. en retard, avenue Victor-Hugo.`
  - **Texto Seguinte Completo:** `Puis ils sont partis.`
- **Arquivo:** `saida_modo2_v2_unpack_tc01.txt` (Corte 21 -> 22)
  - **Texto Anterior Completo:** `[fr] M. Dupont et Mme Martin sont arrivés à 18 h 30, c.-à-d. en retard, avenue Victor-Hugo.`
  - **Texto Seguinte Completo:** `Puis ils sont partis.`

### Token `Maria.` (2 ocorrências)

- **Arquivo:** `saida_modo1_v2_unpack_tc02.txt` (Corte 12 -> 13)
  - **Texto Anterior Completo:** `"Talvez", disse a Sra. Maria.`
  - **Texto Seguinte Completo:** `O Sr. João, porém, discordou.`
- **Arquivo:** `saida_modoA_v2_unpack_tc02.txt` (Corte 12 -> 13)
  - **Texto Anterior Completo:** `"Talvez", disse a Sra. Maria.`
  - **Texto Seguinte Completo:** `O Sr. João, porém, discordou.`

### Token `Paris.` (2 ocorrências)

- **Arquivo:** `saida_modo1_v2_unpack_tc02.txt` (Corte 36 -> 37)
  - **Texto Anterior Completo:** `Mme Dubois et M. Lefèvre habitent au 12, rue de la Paix, 75002 Paris.`
  - **Texto Seguinte Completo:** `Ils sont arrivés à 8 h 15, p. ex. avant les autres.`
- **Arquivo:** `saida_modoA_v2_unpack_tc02.txt` (Corte 36 -> 37)
  - **Texto Anterior Completo:** `Mme Dubois et M. Lefèvre habitent au 12, rue de la Paix, 75002 Paris.`
  - **Texto Seguinte Completo:** `Ils sont arrivés à 8 h 15, p. ex. avant les autres.`
