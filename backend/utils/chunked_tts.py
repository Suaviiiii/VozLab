"""
Chunked TTS generation utilities.

Splits long text into sentence-boundary chunks, generates audio per-chunk
via any TTSBackend, and concatenates with crossfade.  All logic is
engine-agnostic — it wraps the standard ``TTSBackend.generate()`` interface.

Short text (≤ max_chunk_chars) uses the single-shot fast path with zero
overhead.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from .texto_longo_defaults import (
    DEFAULT_CONFIG,
    get_abbreviation_classes_for_language,
    get_abbreviations_for_language,
    load_texto_longo_config,
    normalize_lang_code,
)

logger = logging.getLogger("voicebox.chunked-tts")

# Default chunk size in characters. Lowered from 800 to 220 to prevent
# O(N^2) attention blowup and autoregressive infinite loops on consumer GPUs.
DEFAULT_MAX_CHUNK_CHARS = 220
MAX_RUNAWAY_RETRIES = 2
MIN_RUNAWAY_RETRY_CHARS = 50

# Common abbreviations that should NOT be treated as sentence endings.
# Lowercase for case-insensitive matching.
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "ave",
        "blvd",
        "inc",
        "ltd",
        "corp",
        "dept",
        "est",
        "approx",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "a.m",
        "p.m",
        "u.s",
        "u.s.a",
        "u.k",
    }
)

# Paralinguistic tags used by Chatterbox Turbo.  The splitter must never
# cut inside one of these.
_PARA_TAG_RE = re.compile(r"\[[^\]]*\]")


def split_text_into_chunks(text: str, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> List[str]:
    """Split *text* at natural boundaries into chunks of at most *max_chars*.

    Priority: sentence-end (``.!?`` not preceded by an abbreviation and not
    inside brackets) → clause boundary (``;:,—``) → whitespace → hard cut.

    Paralinguistic tags like ``[laugh]`` are treated as atomic and will not
    be split across chunks.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text

    while remaining:
        remaining = remaining.lstrip()
        if not remaining:
            break
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        segment = remaining[:max_chars]

        # Try to split at the last real sentence ending
        split_pos = _find_last_sentence_end(segment)
        if split_pos == -1:
            split_pos = _find_last_clause_boundary(segment)
        if split_pos == -1:
            split_pos = segment.rfind(" ")
        if split_pos == -1:
            # Absolute fallback: hard cut but avoid splitting inside a tag
            split_pos = _safe_hard_cut(segment, max_chars)

        chunk = remaining[: split_pos + 1].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_pos + 1 :]

    return chunks


def _find_last_sentence_end(text: str) -> int:
    """Return the index of the last sentence-ending punctuation in *text*.

    Skips periods that follow common abbreviations (``Dr.``, ``Mr.``, etc.)
    and periods inside bracket tags (``[laugh]``).  Also handles CJK
    sentence-ending punctuation (``。！？``).
    """
    best = -1
    # ASCII sentence ends
    for m in re.finditer(r"[.!?](?:\s|$)", text):
        pos = m.start()
        char = text[pos]
        # Skip periods after abbreviations
        if char == ".":
            # Walk backwards to find the preceding word
            word_start = pos - 1
            while word_start >= 0 and text[word_start].isalpha():
                word_start -= 1
            word = text[word_start + 1 : pos].lower()
            if word in _ABBREVIATIONS:
                continue
            # Skip decimal numbers (digit immediately before the period)
            if word_start >= 0 and text[word_start].isdigit():
                continue
        # Skip if we're inside a bracket tag
        if _inside_bracket_tag(text, pos):
            continue
        best = pos
    # CJK sentence-ending punctuation
    for m in re.finditer(r"[\u3002\uff01\uff1f]", text):
        if m.start() > best:
            best = m.start()
    return best


def _find_last_clause_boundary(text: str) -> int:
    """Return the index of the last clause-boundary punctuation."""
    best = -1
    for m in re.finditer(r"[;:,\u2014](?:\s|$)", text):
        pos = m.start()
        # Skip if inside a bracket tag
        if _inside_bracket_tag(text, pos):
            continue
        best = pos
    return best


def _inside_bracket_tag(text: str, pos: int) -> bool:
    """Return True if *pos* falls inside a ``[...]`` tag."""
    for m in _PARA_TAG_RE.finditer(text):
        if m.start() < pos < m.end():
            return True
    return False


def _safe_hard_cut(segment: str, max_chars: int) -> int:
    """Find a hard-cut position that doesn't split a ``[tag]``."""
    cut = max_chars - 1
    # Check if the cut falls inside a bracket tag; if so, move before it
    for m in _PARA_TAG_RE.finditer(segment):
        if m.start() < cut < m.end():
            return m.start() - 1 if m.start() > 0 else cut
    return cut


# =========================================================================
# SISTEMA DE TEXTO LONGO V2 — DIVISOR INTELIGENTE COM PRESERVAÇÃO DE SINTAXE
# =========================================================================

# Pontuações finais de frase
_SENTENCE_END_PUNCT = ".!?"
_CJK_SENTENCE_END_PUNCT = "\u3002\uff01\uff1f"  # 。！？

# Pontuações de oração / cláusula
_CLAUSE_PUNCT = ";:,\u2014\u2013"  # ; : , — –
_CJK_CLAUSE_PUNCT = "\u3001\uff1a\uff1b"  # 、 ： ；


@dataclass
class ChunkV2:
    text: str
    boundary_type: str  # "oracao" | "frase" | "paragrafo"
    paragraph_idx: int  # 1-based index

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "boundary_type": self.boundary_type,
            "paragraph_idx": self.paragraph_idx,
            "chars": len(self.text),
        }


COMPOUND_UNITS_REGEX = re.compile(
    r'(?:'
    r'https?://[^\s]+|www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?|'
    r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+|'
    r'\b(?:d\.\s*c\.|a\.\s*c\.|c\.-à-d\.|c\.-a-d\.|p\.\s*ex\.|p\.\s*ej\.|c\.\s*p\.|s\.\s*f\.|e\.\s*g\.|i\.\s*e\.|z\.\s*b\.|d\.\s*h\.|u\.\s*a\.|f\.\s*eks\.|bl\.\s*a\.|dott\.ssa|s\.a\.|u\.s\.a\.|u\.s\.|u\.k\.|a\.\s*m\.|p\.\s*m\.|т\.\s*[ееддпп]\.|и\s+т\.\s*[дп]\.)|'
    r'\b[nN]\s*\.\s*[º°ª](?:\s*\d+)?|'
    r'\b\d+\s*\.\s*[ºª°]|'
    r'\d+(?:[.,]\d+)+'
    r')',
    re.IGNORECASE,
)

CLOSING_CHARS = frozenset("\"'”’»›)]}」』）］")


def _is_closing_char(text: str, pos: int) -> bool:
    """Verifica se o caractere na posição pos é um fecho legítimo no contexto anterior."""
    ch = text[pos]
    if ch in CLOSING_CHARS:
        if ch in "\"'”’»›":
            return _is_quote_open_at(text, pos)
        return True
    if ch == "«" and text[:pos].count("»") > text[:pos].count("«"):
        return True
    if ch == "“" and text[:pos].count("„") > (text[:pos].count("“") + text[:pos].count("”")):
        return True
    return False


def _absorb_trailing_closures(text: str, start_pos: int) -> int:
    """Avança start_pos absorvendo fechos de aspas/parênteses/colchetes e espaços intermediários (ex: ' ? »')."""
    n = len(text)
    pos = start_pos
    while pos < n:
        if _is_closing_char(text, pos):
            pos += 1
            continue
        if text[pos] in " \t\u00a0\u202f":
            if not _is_quote_open_at(text, pos):
                break
            look = pos
            while look < n and text[look] in " \t\u00a0\u202f":
                look += 1
            if look < n and _is_closing_char(text, look):
                pos = look + 1
                continue
            break
        break
    if pos < n and text[pos] == ",":
        pos += 1
    return pos


def _is_quote_open_at(text: str, pos: int) -> bool:
    """Verifica se na posição pos existe alguma aspa aberta sem fecho."""
    sub = text[:pos]
    if sub.count('"') % 2 != 0:
        return True
    g_open = sub.count("«")
    g_close = sub.count("»")
    if g_open > g_close:
        return True
    if sub.count("»") > sub.count("«") and "«" in text[pos:]:
        return True
    if sub.count("„") > (sub.count("“") + sub.count("”")):
        return True
    if sub.count("“") > sub.count("”") and sub.count("„") == 0:
        return True
    if sub.count("「") > sub.count("」"):
        return True
    if sub.count("『") > sub.count("』"):
        return True
    single_open = sub.count("‘")
    single_close = len(re.findall(r"(?<![a-zA-ZÀ-ÿ\u0400-\u04ff])’|’(?![a-zA-ZÀ-ÿ\u0400-\u04ff])", sub))
    if single_open > single_close:
        return True
    single_straight = len(re.findall(r"(?<![a-zA-ZÀ-ÿ\u0400-\u04ff])'|'(?![a-zA-ZÀ-ÿ\u0400-\u04ff])", sub))
    if single_straight % 2 != 0:
        return True
    return False


_ROMAN_NUMERALS = re.compile(r'^(?:X{0,3}(?:IX|IV|V?I{0,3}))(?:\b|$)', re.IGNORECASE)


def _find_protected_spans_v2(text: str) -> List[Tuple[int, int]]:
    """Identifica trechos atômicos que nunca devem ser partidos por pontuação interna."""
    spans: List[Tuple[int, int]] = []
    for m in _PARA_TAG_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in COMPOUND_UNITS_REGEX.finditer(text):
        spans.append((m.start(), m.end()))
    return spans


def _is_inside_spans_v2(pos: int, spans: List[Tuple[int, int]]) -> bool:
    """Verifica se uma posição cai dentro de um span protegido."""
    for s, e in spans:
        if s <= pos < e:
            return True
    return False


def _find_sentence_boundaries_v2(
    text: str,
    language: str = "pt",
    config: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """Retorna os índices onde terminam sentenças reais no texto usando 3 classes de abreviações."""
    spans = _find_protected_spans_v2(text)
    ca, cb, cc = get_abbreviation_classes_for_language(language, config, text=text)

    bounds: List[int] = []
    i = 0
    n = len(text)

    while i < n:
        if _is_inside_spans_v2(i, spans):
            i += 1
            continue

        c = text[i]

        # Pontuação CJK (。！？)
        if c in "\u3002\uff01\uff1f":
            end_pos = _absorb_trailing_closures(text, i + 1)
            after_cjk = text[end_pos:].lstrip()
            if after_cjk and _is_dialogue_tag(after_cjk):
                i = end_pos
                continue
            bounds.append(end_pos)
            i = end_pos
            continue

        # Ponto de exclamação ou interrogação
        if c in "!?":
            end_pos = i + 1
            while end_pos < n and text[end_pos] in "!?":
                end_pos += 1
            end_pos = _absorb_trailing_closures(text, end_pos)
            if _is_quote_open_at(text, end_pos):
                m_close = re.search(r'["”’»«」』\']', text[end_pos:])
                if m_close:
                    close_pos = end_pos + m_close.end()
                    last_b = bounds[-1] if bounds else 0
                    max_limit_q = int((config.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS) if config else DEFAULT_MAX_CHUNK_CHARS) * 1.2)
                    if close_pos - last_b <= max_limit_q:
                        i += 1
                        continue
            after_excl = text[end_pos:].lstrip()
            if after_excl:
                if after_excl[0] in ",;:" or _is_dialogue_tag(after_excl):
                    i = end_pos
                    continue
            bounds.append(end_pos)
            i = end_pos
            continue

        # Ponto final
        if c == ".":
            # Reticências (... ou ..)
            if (i > 0 and text[i - 1] == ".") or (i + 1 < n and text[i + 1] == "."):
                i += 1
                continue

            after_chars = text[i + 1:].lstrip()

            # Ordinais: dígito + "." + indicador ordinal (º, ª, °)
            if i > 0 and text[i - 1].isdigit():
                after_raw = text[i + 1:]
                if after_raw.lstrip().startswith(("º", "ª", "°")):
                    i += 1
                    continue

            # Abreviações de número: n.º, n.ª, n.°
            if i > 0 and text[i - 1].lower() == "n" and (i == 1 or not text[i - 2].isalnum()):
                after_raw = text[i + 1:]
                if after_raw.lstrip().startswith(("º", "ª", "°")):
                    i += 1
                    continue

            # Se a próxima for vírgula, ponto-e-vírgula ou dois-pontos, não é fim de frase!
            if after_chars and after_chars[0] in ",;:":
                i += 1
                continue

            # Travessão de fala nova (— Maiúscula) vs travessão de oração (— minúscula)
            if after_chars and after_chars[0] in "—–-":
                sub = after_chars[1:].lstrip()
                if sub and (sub[0].isupper() or sub[0] in "\"'“«"):
                    end_pos = _absorb_trailing_closures(text, i + 1)
                    bounds.append(end_pos)
                    i = end_pos
                    continue
                else:
                    i += 1
                    continue

            # Marcador de lista numerada: ex "\n1. ", "\n2. "
            w_start = i - 1
            while w_start >= 0 and text[w_start].isdigit():
                w_start -= 1
            if w_start < i - 1:
                before_num = text[:w_start + 1].rstrip(" \t")
                if not before_num or before_num.endswith(("\n", "\r", ":")):
                    i += 1
                    continue

            # Palavra imediatamente precedente
            w_start = i - 1
            while w_start >= 0 and (text[w_start].isalnum() or text[w_start] in "ºª_-éáíóúâêîôûãõçüöäëïàèìòù"):
                w_start -= 1
            word = text[w_start + 1:i].lower().strip("ºª.")
            raw_word = text[w_start + 1:i]

            # Inicial maiúscula isolada de nome próprio (ex: J. K. Souza)
            if len(raw_word) == 1 and raw_word.isupper():
                if after_chars and (after_chars[0].isupper() or after_chars[0] in "0123456789"):
                    i += 1
                    continue

            # Conflito militar Cap. vs capítulo cap.
            if raw_word == "Cap":  # Militar (Capitão) -> Classe A (sempre protegido)
                i += 1
                continue
            elif raw_word == "cap":  # Capítulo -> Classe B (protegido apenas antes de numerais)
                if after_chars:
                    first_tok = after_chars.split()[0].rstrip(",.;:)")
                    if any(ch.isdigit() for ch in first_tok) or first_tok.endswith(("º", "ª", "°")) or _ROMAN_NUMERALS.match(first_tok):
                        i += 1
                        continue

            # Classe A: Sempre protegida (nunca encerra frase)
            if word in ca:
                i += 1
                continue

            # Classe B: Protegida apenas se seguida de número/ordinal/romano
            if word in cb or word.endswith("str"):
                if after_chars:
                    first_tok = after_chars.split()[0].rstrip(",.;:)")
                    if any(ch.isdigit() for ch in first_tok) or first_tok.endswith(("º", "ª", "°")) or _ROMAN_NUMERALS.match(first_tok):
                        i += 1
                        continue

            # Classe C: Ambígua (protegida se próxima for minúscula ou pontuação de continuação)
            if word in cc:
                if after_chars and (after_chars[0].islower() or after_chars[0] in ",;:—–-"):
                    i += 1
                    continue

            # Absorve fechos imediatos (aspas, parênteses, colchetes, vírgulas de etiqueta)
            end_pos = _absorb_trailing_closures(text, i + 1)

            # Verificação de aspas abertas: se após o fecho ainda houver aspas abertas, estamos no meio de uma citação
            if _is_quote_open_at(text, end_pos):
                m_close = re.search(r'["”’»«」』\']', text[end_pos:])
                if m_close:
                    close_pos = end_pos + m_close.end()
                    last_b = bounds[-1] if bounds else 0
                    max_limit_q = int((config.get("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS) if config else DEFAULT_MAX_CHUNK_CHARS) * 1.2)
                    if close_pos - last_b <= max_limit_q:
                        i += 1
                        continue

            # Se o texto após o fecho for etiqueta de diálogo (em minúscula, vírgula, travessão ou partícula japonesa),
            # absorve como parte da mesma sentença:
            after_end = text[end_pos:].lstrip()
            if after_end and _is_dialogue_tag(after_end):
                i = end_pos
                continue

            # Fim de sentença legítimo
            bounds.append(end_pos)
            i = end_pos
            continue

        i += 1

    return bounds


_URL_OR_PATH_RE = re.compile(
    r'(?:https?://[^\s]+|www\.[^\s]+|[a-zA-Z]:\\[^\s,;:"\'<>]+|/[a-zA-Z0-9_.-]+/[^\s,;:"\'<>]+)',
    re.IGNORECASE,
)


def _split_long_url_or_path(url_text: str, max_limit: int) -> List[str]:
    """Divide URLs/caminhos longos cortando SOMENTE em '/', '?', '&' ou '=' sem partir nomes/parâmetros."""
    if len(url_text) <= max_limit:
        return [url_text]
    pieces: List[str] = []
    rem = url_text
    while len(rem) > max_limit:
        segment = rem[:max_limit]
        best_pos = -1
        for delim in ["&", "?", "/"]:
            pos = segment.rfind(delim)
            if pos > best_pos and pos > 20:
                best_pos = pos
        if best_pos == -1:
            pos = segment.rfind("=")
            if pos > 20:
                best_pos = pos
        if best_pos != -1:
            cut_pos = best_pos + 1
            pieces.append(rem[:cut_pos])
            rem = rem[cut_pos:]
        else:
            pieces.append(rem[:max_limit])
            rem = rem[max_limit:]
    if rem:
        pieces.append(rem)
    return pieces


def _split_into_clauses_v2(text: str, max_chars: int) -> List[str]:
    """Divide sentenças longas em orações respeitando cláusulas sem deixar pontuação no início."""
    if len(text) <= max_chars:
        return [text]

    pieces: List[str] = []
    remaining = text.strip()

    while len(remaining) > max_chars:
        # Tratamento de URL/caminho: indivisível até 2x max_chars; acima disso, corta em delimitadores seguros
        m_url = _URL_OR_PATH_RE.search(remaining)
        if m_url:
            u_start, u_end = m_url.start(), m_url.end()
            url_str = remaining[u_start:u_end]
            if u_start > 0 and u_start <= max_chars:
                piece = remaining[:u_start].strip()
                rest = remaining[u_start:].lstrip()
                if piece:
                    pieces.append(piece)
                remaining = rest
                continue
            elif u_start == 0:
                url_pieces = _split_long_url_or_path(url_str, 2 * max_chars)
                for up in url_pieces[:-1]:
                    if up:
                        pieces.append(up)
                pieces.append(url_pieces[-1])
                remaining = remaining[u_end:].lstrip()
                continue

        segment = remaining[:max_chars]

        best_pos = -1
        for m in re.finditer(r"[;:,\u2014\u2013\u3001\uff1a\uff1b](?:\s|$)", segment):
            pos = m.start()
            if segment[pos] == ",":
                if pos > 0 and segment[pos - 1].isdigit() and pos + 1 < len(segment) and segment[pos + 1].isdigit():
                    continue
            # Regra (a): nunca cortar logo após ":" quando segue aspa de abertura ou travessão de fala
            if segment[pos] in ":\uff1a":
                after_colon = remaining[pos + 1:].lstrip()
                if after_colon.startswith(('"', "“", "«", "„", "「", "『", "—", "–", "-")):
                    continue
            # Regra (c): japonês: nunca cortar entre 」 e partículas de citação
            if segment[pos] in "」』":
                after_close = remaining[pos + 1:].lstrip()
                if after_close.startswith(("と", "って", "という", "など")):
                    continue
            best_pos = pos

        if best_pos != -1:
            cut_pos = best_pos + 1
            piece = remaining[:cut_pos].strip()
            rest = remaining[cut_pos:].lstrip()
            while rest and rest[0] in ",;:.-" and not rest.startswith(("— ", "– ", "- ")):
                piece += rest[0]
                rest = rest[1:].lstrip()
            if piece:
                pieces.append(piece)
            remaining = rest
            continue

        space_pos = segment.rfind(" ")
        if space_pos > 20:
            piece = remaining[:space_pos].strip()
            rest = remaining[space_pos:].lstrip()
            while rest and rest[0] in ",;:.-" and not rest.startswith(("— ", "– ", "- ")):
                piece += rest[0]
                rest = rest[1:].lstrip()
            if piece:
                pieces.append(piece)
            remaining = rest
            continue

        cut = max_chars - 1
        piece = remaining[:cut].strip()
        rest = remaining[cut:].lstrip()
        while rest and rest[0] in ",;:.-" and not rest.startswith(("— ", "– ", "- ")):
            piece += rest[0]
            rest = rest[1:].lstrip()
        if piece:
            pieces.append(piece)
        remaining = rest

    if remaining:
        pieces.append(remaining)

    return pieces


def _is_new_dialogue_speech(text: str) -> bool:
    """Verifica se o texto inicia uma nova fala de diálogo (travessão/aspas seguido de maiúscula)."""
    t = text.strip()
    if not t:
        return False
    if re.match(r'^[—–-]\s*[A-ZÀ-ÖØ-Ý]', t):
        return True
    if re.match(r'^["“«»]\s*[A-ZÀ-ÖØ-Ý]', t):
        return True
    return False


def _is_dialogue_tag(text: str) -> bool:
    """Verifica se o texto é uma etiqueta/inciso de diálogo (travessão, vírgula ou verbo de elocução em minúscula)."""
    t = text.strip()
    if not t:
        return False
    # Travessão seguido de minúscula: ex "— disse ela"
    if re.match(r'^[—–-]\s*[a-zà-öø-ÿа-яё]', t):
        return True
    # Vírgula seguida de minúscula ou travessão: ex ", disse ela", ", fragte sie", ", — спросила"
    if re.match(r'^,\s*(?:[—–-]\s*)?[a-zà-öø-ÿа-яё]', t):
        return True
    # Início direto em minúscula (verbos de elocução ou continuação): ex "demanda-t-elle", "she asked", "disse ela"
    if re.match(r'^[a-zà-öø-ÿа-яё]', t):
        return True
    # Japonês: partículas/verbos de citação: ex "と彼女は", "と言った", "って", "という", "など"
    if re.match(r'^(?:と|って|という|など)', t):
        return True
    return False


def split_text_into_chunks_v2(
    text: str,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    min_chars: int = 40,
    language: str = "pt",
    as_objects: bool = True,
    config: Optional[Dict[str, Any]] = None,
    empacotar_frases: Optional[bool] = None,
) -> List[ChunkV2] | List[str]:
    """
    Divisor v2 de Texto Longo com preservação estrita de sintaxe, sem perda de texto.
    """
    text = text.strip().replace("\r\n", "\n")
    if not text:
        return []

    if config is None:
        config = load_texto_longo_config()

    if empacotar_frases is None:
        empacotar_frases = bool(config.get("empacotar_frases", True))

    raw_paragraphs = re.split(r"\n\s*\n+", text)
    chunks_result: List[ChunkV2] = []

    for p_idx, raw_p in enumerate(raw_paragraphs):
        p_text = raw_p.strip()
        if not p_text:
            continue
        p_num = p_idx + 1

        bounds = _find_sentence_boundaries_v2(p_text, language=language, config=config)

        # 1. Extrai sentenças mantendo exatamente os offsets originais
        raw_sentences: List[str] = []
        if not bounds:
            raw_sentences = [p_text]
        else:
            last = 0
            for b in bounds:
                seg = p_text[last:b].strip()
                if seg:
                    raw_sentences.append(seg)
                last = b
            tail = p_text[last:].strip()
            if tail:
                raw_sentences.append(tail)

        # 2. Divide sentenças longas em orações se excederem max_chars
        sentence_units: List[str] = []
        for s in raw_sentences:
            if len(s) <= max_chars:
                sentence_units.append(s)
            else:
                sentence_units.extend(_split_into_clauses_v2(s, max_chars))

        # 3. Trata etiqueta de diálogo: junta com a fala anterior permitindo até +20% (ou corte em vírgula)
        merged_units: List[str] = []
        max_tag_chars = int(max_chars * 1.2)  # Permite até +20% exclusivamente para acomodar etiqueta
        for unit in sentence_units:
            is_tag = _is_dialogue_tag(unit)
            # Se for início direto em minúscula (sem travessão nem vírgula), só é etiqueta se a fala anterior terminou com pontuação de fala ou aspas
            if is_tag and not re.match(r'^[,—–-]', unit.strip()) and not re.match(r'^(?:と|って|という|など)', unit.strip()):
                if not (merged_units and any(merged_units[-1].rstrip(" \t,").endswith(end_ch) for end_ch in ['"', "'", "”", "’", "“", "»", "«", "›", "」", "』", "!", "?", "—", "–", "-"])):
                    is_tag = False

            should_merge_tag = (
                merged_units
                and is_tag
            )
            if should_merge_tag:
                prev = merged_units[-1]
                idx_prev = p_text.find(prev)
                idx_tag = p_text.find(unit, idx_prev + len(prev)) if idx_prev != -1 else -1
                sep_tag = p_text[idx_prev + len(prev):idx_tag] if (idx_prev != -1 and idx_tag != -1) else " "

                if len(prev) + len(sep_tag) + len(unit) <= max_tag_chars:
                    # Cabe na tolerância de +20%
                    merged_units[-1] = prev + sep_tag + unit
                else:
                    # Excede +20%: corta a fala anterior na última vírgula precedente
                    comma_pos = prev.rfind(",")
                    if comma_pos != -1 and comma_pos > 20:
                        speech_part1 = prev[:comma_pos + 1].strip()
                        speech_part2 = prev[comma_pos + 1:].strip()
                        merged_units[-1] = speech_part1
                        merged_units.append(speech_part2 + sep_tag + unit)
                    else:
                        space_pos = prev.rfind(" ", 0, max(20, max_chars - len(unit) - 5))
                        if space_pos != -1:
                            speech_part1 = prev[:space_pos].strip()
                            speech_part2 = prev[space_pos:].strip()
                            merged_units[-1] = speech_part1
                            merged_units.append(speech_part2 + sep_tag + unit)
                        else:
                            # Fallback: funde para nunca deixar a etiqueta isolada
                            merged_units[-1] = prev + sep_tag + unit
            else:
                merged_units.append(unit)

        # 4. Empacotamento de frases (empacotar_frases)
        final_pieces: List[str] = []
        if not empacotar_frases:
            final_pieces = merged_units
        else:
            curr_text = ""
            for unit in merged_units:
                if not curr_text:
                    curr_text = unit
                    continue

                # Regra: NUNCA juntar falas diferentes (nova fala inicia novo bloco)
                if _is_new_dialogue_speech(unit):
                    final_pieces.append(curr_text)
                    curr_text = unit
                    continue

                # Determina o separador original dentro de p_text entre curr_text e unit
                idx_curr = p_text.find(curr_text)
                idx_unit = p_text.find(unit, idx_curr + len(curr_text)) if idx_curr != -1 else -1
                if idx_curr != -1 and idx_unit != -1:
                    sep = p_text[idx_curr + len(curr_text):idx_unit]
                else:
                    sep = "" if any("\u3040" <= ch <= "\u9fff" for ch in curr_text[-1:]) else " "

                if len(curr_text) + len(sep) + len(unit) <= max_chars:
                    curr_text = curr_text + sep + unit
                else:
                    final_pieces.append(curr_text)
                    curr_text = unit
            if curr_text:
                final_pieces.append(curr_text)

        # 5. Classifica tipo de fronteira e higieniza início de bloco
        total = len(final_pieces)
        for idx, piece in enumerate(final_pieces):
            clean_piece = piece.strip()
            while clean_piece and (
                (clean_piece[0] in ",;:.-" and not clean_piece.startswith(("— ", "– ", "- ")))
                or (clean_piece[0] in "»”’)]}」』›ºª°" and not (clean_piece[0] == "»" and "«" in clean_piece))
            ):
                clean_piece = clean_piece[1:].lstrip()

            if idx == total - 1:
                b_type = "paragrafo"
            elif any(clean_piece.endswith(p) for p in [".", "!", "?", "。", "！", "？", ".\"", ".\" ", "!'", "?'", ".»", "!»", "?»", ".”", "!”", "?”", "。」", "！」", "？」"]):
                b_type = "frase"
            else:
                b_type = "oracao"

            chunks_result.append(ChunkV2(text=clean_piece, boundary_type=b_type, paragraph_idx=p_num))

    if as_objects:
        return chunks_result
    return [c.text for c in chunks_result]



def concatenate_audio_chunks(
    chunks: List[np.ndarray],
    sample_rate: int,
    crossfade_ms: int = 50,
) -> np.ndarray:
    """Concatenate audio arrays with a short crossfade to eliminate clicks.

    Each chunk is expected to be a 1-D float32 ndarray at *sample_rate* Hz.
    """
    if not chunks:
        return np.array([], dtype=np.float32)
    if len(chunks) == 1:
        return chunks[0]

    crossfade_samples = int(sample_rate * crossfade_ms / 1000)
    result = np.array(chunks[0], dtype=np.float32, copy=True)

    for chunk in chunks[1:]:
        if len(chunk) == 0:
            continue
        overlap = min(crossfade_samples, len(result), len(chunk))
        if overlap > 0:
            fade_out = np.linspace(1.0, 0.0, overlap, dtype=np.float32)
            fade_in = np.linspace(0.0, 1.0, overlap, dtype=np.float32)
            result[-overlap:] = result[-overlap:] * fade_out + chunk[:overlap] * fade_in
            result = np.concatenate([result, chunk[overlap:]])
        else:
            result = np.concatenate([result, chunk])

    return result


async def generate_single_chunk(
    backend,
    chunk_text: str,
    voice_prompt: dict,
    language: str = "en",
    chunk_seed: int | None = None,
    instruct: str | None = None,
    trim_fn=None,
    runaway_detector=None,
    crossfade_ms: int = 50,
    retry_depth: int = 0,
) -> tuple[np.ndarray, int]:
    """Generate audio for a single text chunk with timeout, runaway detection and trimming."""
    try:
        chunk_audio, chunk_sr = await asyncio.wait_for(
            backend.generate(
                chunk_text,
                voice_prompt,
                language,
                chunk_seed,
                instruct,
            ),
            timeout=300.0,
        )
    except asyncio.TimeoutError:
        logger.error(f"Chunk generation timed out (300s) for text: {chunk_text[:40]}...")
        raise RuntimeError("TTS chunk generation timed out (exceeded 300s).")

    if runaway_detector is not None and runaway_detector(chunk_audio, chunk_sr):
        if retry_depth >= MAX_RUNAWAY_RETRIES or len(chunk_text) <= MIN_RUNAWAY_RETRY_CHARS:
            raise RuntimeError(
                "TTS output remained unstable after retrying smaller text chunks"
            )

        retry_max_chars = max(MIN_RUNAWAY_RETRY_CHARS, len(chunk_text) // 2)
        retry_chunks = split_text_into_chunks(chunk_text, retry_max_chars)
        if len(retry_chunks) <= 1:
            raise RuntimeError("Unable to split unstable TTS output for retry")

        logger.warning(
            "Detected unstable TTS output for %d chars; retrying as %d smaller chunks",
            len(chunk_text),
            len(retry_chunks),
        )
        retry_audio: list[np.ndarray] = []
        for i, retry_text in enumerate(retry_chunks):
            retry_seed = (
                chunk_seed + ((retry_depth + 1) * 1000) + i
                if chunk_seed is not None
                else None
            )
            audio, sample_rate = await generate_single_chunk(
                backend=backend,
                chunk_text=retry_text,
                voice_prompt=voice_prompt,
                language=language,
                chunk_seed=retry_seed,
                instruct=instruct,
                trim_fn=trim_fn,
                runaway_detector=runaway_detector,
                crossfade_ms=crossfade_ms,
                retry_depth=retry_depth + 1,
            )
            retry_audio.append(np.asarray(audio, dtype=np.float32))

        return (
            concatenate_audio_chunks(
                retry_audio,
                sample_rate,
                crossfade_ms=crossfade_ms,
            ),
            sample_rate,
        )

    if trim_fn is not None:
        chunk_audio = trim_fn(chunk_audio, chunk_sr)
    return np.asarray(chunk_audio, dtype=np.float32), chunk_sr


async def generate_chunked(
    backend,
    text: str,
    voice_prompt: dict,
    language: str = "en",
    seed: int | None = None,
    instruct: str | None = None,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    crossfade_ms: int = 50,
    trim_fn=None,
    runaway_detector=None,
    progress_cb=None,
) -> Tuple[np.ndarray, int]:
    """Generate audio with automatic chunking for long text.

    For text shorter than *max_chunk_chars* this is a thin wrapper around
    ``backend.generate()`` with zero overhead.

    For longer text the input is split at natural sentence boundaries,
    each chunk is generated independently, optionally trimmed (useful for
    Chatterbox engines that hallucinate trailing noise), and the results
    are concatenated with a crossfade (or hard cut if *crossfade_ms* is 0).

    Parameters
    ----------
    backend : TTSBackend
        Any backend implementing the ``generate()`` protocol.
    text : str
        Input text (may be arbitrarily long).
    voice_prompt, language, seed, instruct
        Forwarded to ``backend.generate()`` verbatim.
    max_chunk_chars : int
        Maximum characters per chunk (default 800).
    crossfade_ms : int
        Crossfade duration in milliseconds between chunks.  0 for a hard
        cut with no overlap (default 50).
    trim_fn : callable | None
        Optional ``(audio, sample_rate) -> audio`` post-processing
        function applied to each chunk before concatenation (e.g.
        ``trim_tts_output`` for Chatterbox engines).
    runaway_detector : callable | None
        Optional ``(audio, sample_rate) -> bool`` detector. When it flags
        unstable output, the affected text is split in half and retried.

    Returns
    -------
    (audio, sample_rate) : Tuple[np.ndarray, int]
    """
    async def generate_one(
        chunk_text: str,
        chunk_seed: int | None,
        retry_depth: int = 0,
    ) -> tuple[np.ndarray, int]:
        return await generate_single_chunk(
            backend=backend,
            chunk_text=chunk_text,
            voice_prompt=voice_prompt,
            language=language,
            chunk_seed=chunk_seed,
            instruct=instruct,
            trim_fn=trim_fn,
            runaway_detector=runaway_detector,
            crossfade_ms=crossfade_ms,
            retry_depth=retry_depth,
        )

    cfg = load_texto_longo_config()
    usar_v2 = cfg.get("texto_longo_v2", False)

    if usar_v2:
        chunks_objs = split_text_into_chunks_v2(
            text,
            max_chars=max_chunk_chars,
            min_chars=cfg.get("min_chunk_chars", 40),
            language=language,
            as_objects=True,
            config=cfg,
        )
        chunks = [c.text for c in chunks_objs]
    else:
        chunks = split_text_into_chunks(text, max_chunk_chars)

    def _report(done: int):
        if progress_cb is None:
            return
        try:
            progress_cb(done, len(chunks), [len(c) for c in chunks])
        except Exception:
            logger.debug("progress_cb failed", exc_info=True)

    _report(0)

    if len(chunks) <= 1:
        # Short text — single-shot fast path
        result = await generate_one(text, seed)
        _report(1)
        return result

    # Long text — chunked generation
    logger.info(
        "Splitting %d chars into %d chunks (max %d chars each)",
        len(text),
        len(chunks),
        max_chunk_chars,
    )
    audio_chunks: List[np.ndarray] = []
    sample_rate: int | None = None

    for i, chunk_text in enumerate(chunks):
        logger.info(
            "Generating chunk %d/%d (%d chars)",
            i + 1,
            len(chunks),
            len(chunk_text),
        )
        # Vary the seed per chunk to avoid correlated RNG artefacts,
        # but keep it deterministic so the same (text, seed) pair
        # always produces the same output.
        chunk_seed = (seed + i) if seed is not None else None

        chunk_audio, chunk_sr = await generate_one(
            chunk_text,
            chunk_seed,
        )

        audio_chunks.append(chunk_audio)
        if sample_rate is None:
            sample_rate = chunk_sr
        _report(i + 1)

    audio = concatenate_audio_chunks(audio_chunks, sample_rate, crossfade_ms=crossfade_ms)
    return audio, sample_rate
