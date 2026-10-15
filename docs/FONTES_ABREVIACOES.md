# Fontes e Origem das Listas de Abreviação do Voicebox

**Data:** 11 de outubro de 2026  
**Módulo:** `backend/utils/texto_longo_defaults.py`  
**Esquema:** `ABBREVIATIONS_VERSION = "pt_v2.0_20261010"`  
**Licença do Código:** Licença do Projeto Voicebox (código próprio)

---

## 1. Declaração de Metodologia e Integridade

Nenhum arquivo de código-fonte ou base de dados externa foi baixado, clonado ou copiado para o repositório durante o desenvolvimento da Fase 2.

Todas as listas de abreviações e regras de segmentação sintática em `texto_longo_defaults.py` foram **elaboradas de memória, não verificadas contra fonte externa**, pela equipe de desenvolvimento com base em conhecimento geral de regras tipográficas e gramaticais.

Nenhum arquivo do **Moses Tokenizer** ou de qualquer outra biblioteca externa foi baixado para o ambiente local. Por conseguinte, não se fazem afirmações factuais sobre o conteúdo exato ou a presença/ausência de arquivos específicos nos repositórios externos de referência.

---

## 2. Origem das Listas por Idioma

| Idioma | Código | Origem da Lista | Status de Verificação Externa |
| :--- | :---: | :--- | :--- |
| **Português** | `pt` | Elaborada de memória | Não verificada contra fonte externa |
| **Inglês** | `en` | Elaborada de memória | Não verificada contra fonte externa |
| **Espanhol** | `es` | Elaborada de memória | Não verificada contra fonte externa |
| **Francês** | `fr` | Elaborada de memória | Não verificada contra fonte externa |
| **Alemão** | `de` | Elaborada de memória | Não verificada contra fonte externa |
| **Italiano** | `it` | Elaborada de memória | Não verificada contra fonte externa |
| **Russo** | `ru` | Elaborada de memória | Não verificada contra fonte externa |
| **Norueguês** | `no` | Elaborada de memória | Não verificada contra fonte externa |
| **Japonês** | `ja` | Elaborada de memória | Não verificada contra fonte externa (tipografia CJK) |
| **Universal** | `univ` | Elaborada de memória | Não verificada contra fonte externa |

---

## 3. Tarefa Pendente (Auditoria Externa Futura)

- **Condição:** Quando houver permissão e acesso de rede externo para download de documentação.
- **Ação:** Baixar as referências públicas consagradas (como os prefixos não-terminais do Moses e corpora abertos de teste) exclusivamente para **LEITURA** e auditoria analítica.
- **Objetivo:** Comparar item a item, auditar eventuais omissões legítimas e registrar diferenças conceituais em relatório comparativo, **sem cópia literal**, preservando integralmente o código autoral e a independência de licenças do Voicebox.

---

## 4. Conformidade Legal e Licenciamento

1. **Inexistência de Cópia Literal:** Nenhum arquivo com licença restritiva ou viral foi copiado para esta base de código.
2. **Independência Algorítmica:** O particionador do Voicebox (`backend/utils/chunked_tts.py`) opera através de um motor próprio de varredura sintática e classificação em três classes (A: sempre protegidas, B: protegidas antes de numerais, C: ambíguas protegidas antes de minúsculas), com tratamento integrado de aspas, travessões de diálogo, URLs longas e tags multimodais.
3. **Liberdade de Distribuição:** Todas as tabelas em `texto_longo_defaults.py` são código original do projeto Voicebox, compatíveis com a distribuição comercial ou de código aberto do projeto e para geração de executáveis únicos (`.exe`).
