"""
Normalizador de texto em Português para cálculo justo de WER.
Cópia de ferramentas_regressao/normalizador_wer.py (Fase 7: o revisor do backend
não importa nada de ferramentas_regressao). Só serve para COMPARAR texto com a
transcrição; nunca muda o texto que vai para a voz.
Converte moedas, horários, decimais, porcentagens, frações, unidades e abreviações
por extenso antes de tokenizar e calcular o Levenshtein.
"""

import re
import unicodedata
import num2words


def expand_numbers_pt(text: str) -> str:
    """Expande números, moedas, horas, unidades e abreviações por extenso em PT-BR."""
    if not text:
        return ""

    # 1. Moedas (R$ 1.200,00, R$ 45,50, R$ 350.000,00)
    def _curr(m):
        val_str = m.group(1).replace(".", "").replace(",", ".")
        try:
            val = float(val_str)
            return num2words.num2words(val, lang="pt_BR", to="currency")
        except Exception:
            return m.group(0)
    text = re.sub(r"R\$\s*([0-9\.,]+)", _curr, text)

    # 2. Horários (14h30, 08h45, 18h00, 19h15)
    def _hora(m):
        h = int(m.group(1))
        minuto = m.group(2)
        h_ext = num2words.num2words(h, lang="pt_BR")
        if minuto and int(minuto) > 0:
            m_ext = num2words.num2words(int(minuto), lang="pt_BR")
            return f"{h_ext} horas e {m_ext} minutos"
        return f"{h_ext} horas"
    text = re.sub(r"(\d{1,2})h(\d{2})?", _hora, text)

    # 3. Frações
    text = re.sub(r"\b1/4\b", "um quarto", text)
    text = re.sub(r"\b2/3\b", "dois terços", text)
    text = re.sub(r"\b1/2\b", "um meio", text)
    text = re.sub(r"\b3/4\b", "três quartos", text)

    # 4. Porcentagens (0,05%, 12,45%, 98,7%, 10,25%)
    def _pct(m):
        num_str = m.group(1).replace(".", "").replace(",", ".")
        try:
            n = float(num_str) if "." in num_str else int(num_str)
            return f"{num2words.num2words(n, lang='pt_BR')} por cento"
        except Exception:
            return m.group(0)
    text = re.sub(r"([0-9\.,]+)\s*%", _pct, text)

    # 5. Decimais avulsos (ex: 3,5 ou 23,8 ou 0,028)
    def _dec(m):
        num_str = m.group(0).replace(",", ".")
        try:
            n = float(num_str)
            # Converte partes inteira e decimal
            parts = m.group(0).split(",")
            int_p = num2words.num2words(int(parts[0]), lang="pt_BR")
            dec_p = num2words.num2words(int(parts[1]), lang="pt_BR")
            return f"{int_p} vírgula {dec_p}"
        except Exception:
            return m.group(0)
    text = re.sub(r"\b\d+,\d+\b", _dec, text)

    # 6. Unidades de medida e acústica
    text = re.sub(r"\bdB\s*SPL\b", "decibéis de pressão sonora", text, flags=re.I)
    text = re.sub(r"\bdB\b", "decibéis", text)
    text = re.sub(r"\bkHz\b", "quilohertz", text, flags=re.I)
    text = re.sub(r"\bHz\b", "hertz", text, flags=re.I)
    text = re.sub(r"\bms\b", "milissegundos", text)
    text = re.sub(r"\bLUFS\b", "lufs", text, flags=re.I)
    text = re.sub(r"\bkbps\b", "kilobits por segundo", text, flags=re.I)
    text = re.sub(r"ºC", "graus celsius", text)

    # 7. Abreviaturas comuns em português
    text = re.sub(r"\bdra\b\.?", "doutora", text, flags=re.I)
    text = re.sub(r"\bdr\b\.?", "doutor", text, flags=re.I)
    text = re.sub(r"\bsra\b\.?", "senhora", text, flags=re.I)
    text = re.sub(r"\bsr\b\.?", "senhor", text, flags=re.I)
    text = re.sub(r"\bprof\b\.?", "professor", text, flags=re.I)
    text = re.sub(r"\beng\b\.?", "engenheiro", text, flags=re.I)
    text = re.sub(r"\bpág\b\.?", "página", text, flags=re.I)
    text = re.sub(r"\bav\b\.?", "avenida", text, flags=re.I)
    text = re.sub(r"\betc\b\.?", "et cetera", text, flags=re.I)
    text = re.sub(r"\bs\.?a\.?\b", "sociedade anônima", text, flags=re.I)
    text = re.sub(r"\bn[ºo]\b\.?", "número", text, flags=re.I)

    # 8. Números inteiros restantes
    def _num(m):
        try:
            return num2words.num2words(int(m.group(0)), lang="pt_BR")
        except Exception:
            return m.group(0)
    text = re.sub(r"\b\d+\b", _num, text)

    # 9. Limpeza final: minúsculas, pontuação vira espaço, remoção de caracteres estranhos
    text = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = text.split()
    return " ".join(tokens)


if __name__ == "__main__":
    teste = "Gastamos R$ 1.200,00 nos novos sensores às 14h30 com o Dr. Arnaldo na Av. Paulista, com ganho de 3,5 dB e 12,45% na pág. 42 etc."
    print("Original:   ", teste)
    print("Normalizado:", expand_numbers_pt(teste))
