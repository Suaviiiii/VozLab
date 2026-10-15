"""
Valores padrão imutáveis de fábrica para o sistema de Texto Longo V2.
Garante que instalações empacotadas em executáveis (.exe) funcionem sem depender
de arquivos externos graváveis, ao mesmo tempo em que permite sobreposição
pelo usuário via <data_dir>/config/texto_longo.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Set, Tuple

logger = logging.getLogger("voicebox.texto-longo.config")

# Versão oficial do esquema e da lista de abreviações
SCHEMA_VERSION = "2.0.0"
ABBREVIATIONS_VERSION = "pt_v2.0_20261010"

# Valores padrão de fábrica
DEFAULT_CONFIG: Dict[str, Any] = {
    # Chave MESTRA: true de fábrica desde ATIVAR-V2-PADRAO (14/10/2026).
    # Com false no texto_longo.json do usuário, o Voicebox opera 100% como no baseline da Fase 0.
    "texto_longo_v2": True,
    # Sub-chaves secundárias (true de fábrica junto com a chave mestra)
    "persistencia_jobs": True,
    "emendas_v2": True,
    # Parâmetros de particionamento textual
    "empacotar_frases": True,
    "max_chunk_chars": 220,
    "min_chunk_chars": 40,
    "limites_caracteres_modelo": {
        "qwen": 220,
        "chatterbox": 220,
        "kokoro": 220,
        "f5_tts": 220,
        "omnivoice": 220,
    },
    # Pausas sintáticas padrão em milissegundos
    "pausas_ms": {
        # Escolha do usuário na escuta da Fase 4 (meio-termo entre M e C)
        "oracao": 140,
        "frase": 300,
        "paragrafo": 775,
    },
    # Equalização de volume e masterização
    "volume": {
        "target_lufs": -19.0,  # escolha do usuário na Fase 4 (mais limpo que -16)
        "max_gain_db": 8.0,
        "max_dbtp": -1.0,
        "curto_rms_threshold_s": 0.4,
    },
    # Emendas e corte de silêncio
    "emendas": {
        "crossfade_ms": 15,
        "trim_margin_ms": 40,
        "trim_threshold_db": -45.0,
    },
    # Heartbeat e trava de processo
    "heartbeat": {
        "interval_s": 5.0,
        "timeout_s": 120.0,
    },
    # Watchdog de fila e inatividade por bloco
    "watchdog": {
        "idle_timeout_per_chunk_s": 600.0,
        "short_job_timeout_s": 3600.0,
        "thread_join_timeout_s": 60.0,
    },
    # Dicionário canônico de abreviações por idioma (minúsculas)
    "abreviações_padrao": {
        "pt": [
            "dr", "dra", "eng", "prof", "profa", "sr", "sra", "srta",
            "exmo", "exma", "av", "rod", "pág", "págs", "pag", "pags",
            "ex", "cia", "ltda", "sa", "s.a", "etc", "obs", "art",
            "cap", "sec", "ed", "vol", "num", "nº", "apto", "tel",
            "min", "seg", "univ", "dept", "depto", "inst", "fac",
            "assoc", "gov", "pres", "sen", "ilmo", "ilma", "pe",
            "sto", "sta"
        ],
        "en": [
            "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
            "ave", "blvd", "rd", "inc", "ltd", "corp", "co", "dept",
            "approx", "vs", "etc", "e.g", "i.e", "a.m", "p.m",
            "u.s", "u.s.a", "u.k", "fig", "vol", "no", "gen", "rep", "sen"
        ],
        "es": [
            "dr", "dra", "sr", "sra", "srta", "prof", "av", "pág",
            "págs", "pag", "pags", "ej", "cia", "etc", "num", "nro",
            "apdo", "dña", "dño", "ud", "uds", "tte", "cap"
        ],
        "fr": [
            "m", "mme", "mlle", "dr", "prof", "av", "p", "pp",
            "soc", "cie", "etc", "vol", "chap", "ed", "st", "ste"
        ],
        "de": [
            "dr", "prof", "hr", "fr", "str", "abb", "z.b", "d.h",
            "u.a", "bzw", "ca", "etc", "vgl", "inkl", "nr", "abs"
        ],
        "it": [
            "dott", "dott.ssa", "sig", "sig.ra", "prof", "avv",
            "pag", "pagg", "ecc", "es", "art", "v", "cap", "n"
        ],
        "ru": [
            "г", "г-жа", "д-р", "проф", "ул", "стр", "т.д", "т.п",
            "и.т.д", "и.т.п", "см", "пр", "кв", "им", "руб", "коп"
        ],
        "no": [
            "dr", "prof", "herr", "fru", "s", "stk", "etc", "f.eks",
            "dvs", "bl.a", "nr", "kl", "ca"
        ],
        "ja": []
    },
    # Chave para o usuário estender sem editar código
    "abreviacoes_customizadas_usuario": {}
}


KNOWN_LANGS = frozenset({"pt", "en", "es", "fr", "de", "it", "ru", "no", "ja"})
NORWEGIAN_ALIASES = frozenset({"nb", "nn"})


def normalize_lang_code(language: str | None) -> str:
    """Normaliza códigos de idioma para chaves padrão ('pt-BR' -> 'pt').

    Usa correspondência exata de tokens (nunca startswith) para evitar
    falsos positivos como 'desconhecido' virando 'de'.
    Códigos não reconhecidos são tratados como 'auto' com log de aviso.
    """
    if not language:
        return "pt"

    lang_clean = language.strip().lower().replace("_", "-")
    if lang_clean in {"auto", "mixed", "misto"}:
        return "auto"

    primary = lang_clean.split("-")[0]
    if primary in KNOWN_LANGS:
        return primary
    if primary in NORWEGIAN_ALIASES:
        return "no"

    logger.warning(
        f"Código de idioma desconhecido '{language}'; tratando como 'auto'"
    )
    return "auto"


def get_user_config_path(data_dir: Path | str | None = None) -> Path:
    """Retorna o caminho do arquivo de configurações no diretório gravável do usuário."""
    if data_dir is None:
        try:
            from .. import config
            data_dir = config.get_data_dir()
        except Exception:
            data_dir = Path.home() / "AppData" / "Roaming" / "sh.voicebox.app"
    return Path(data_dir) / "config" / "texto_longo.json"


def _validate_and_sanitize_config(config: Dict[str, Any]) -> None:
    """Valida tipos e intervalos da configuração com fallback aos padrões de fábrica."""
    # 1. max_chunk_chars: deve ser inteiro estrito e entre 60 e 600
    max_chars = config.get("max_chunk_chars")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not (60 <= max_chars <= 600):
        logger.warning(
            f"Valor inválido para max_chunk_chars ({max_chars!r}); deve ser inteiro entre 60 e 600. "
            f"Restaurando padrão de fábrica ({DEFAULT_CONFIG['max_chunk_chars']})."
        )
        config["max_chunk_chars"] = DEFAULT_CONFIG["max_chunk_chars"]

    # 2. min_chunk_chars: deve ser inteiro positivo menor que max_chunk_chars
    min_chars = config.get("min_chunk_chars")
    if (
        not isinstance(min_chars, int)
        or isinstance(min_chars, bool)
        or not (1 <= min_chars < config["max_chunk_chars"])
    ):
        logger.warning(
            f"Valor inválido para min_chunk_chars ({min_chars!r}); "
            f"restaurando padrão de fábrica ({DEFAULT_CONFIG['min_chunk_chars']})."
        )
        config["min_chunk_chars"] = DEFAULT_CONFIG["min_chunk_chars"]

    # 3. Flags booleanas
    for bool_key in ["texto_longo_v2", "persistencia_jobs", "emendas_v2", "empacotar_frases"]:
        val = config.get(bool_key)
        if not isinstance(val, bool):
            logger.warning(
                f"Tipo inválido para flag {bool_key} ({val!r}); deve ser booleano. "
                f"Restaurando padrão de fábrica ({DEFAULT_CONFIG.get(bool_key, False)})."
            )
            config[bool_key] = DEFAULT_CONFIG.get(bool_key, False)


def load_texto_longo_config(data_dir: Path | str | None = None) -> Dict[str, Any]:
    """Carrega as configurações mesclando os padrões de código com o arquivo JSON do usuário.

    Se o arquivo não existir ou for inválido, retorna os padrões de fábrica imutáveis.
    Nunca interrompe a execução com erro fatal.
    """
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # cópia profunda
    cfg_file = get_user_config_path(data_dir)

    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                for k, v in user_cfg.items():
                    if isinstance(v, dict) and isinstance(config.get(k), dict):
                        config[k].update(v)
                    else:
                        config[k] = v
            else:
                logger.warning(
                    f"Estrutura inválida em {cfg_file} (esperado objeto JSON); usando padrões de fábrica."
                )
        except Exception as e:
            logger.warning(f"Falha ao ler {cfg_file}, usando padrões de fábrica: {e}")

    _validate_and_sanitize_config(config)
    return config


# =========================================================================
# CLASSES DE ABREVIAÇÕES (A: Sempre Protegidas, B: Com Número, C: Ambíguas)
# =========================================================================

# Conjuntos Universais (válidos em todos os idiomas)
UNIVERSAL_CLASS_A: Set[str] = {
    "mr", "mrs", "ms", "dr", "dra", "prof", "profa", "eng", "enga",
    "sr", "sra", "srta", "jr", "st", "ste", "ave", "av", "rod",
    "blvd", "rd", "str", "cia", "ltda", "inc", "corp", "ltd", "co",
    "soc", "cie", "s.a", "dott", "dott.ssa", "avv", "m", "mme", "mlle",
    "ca", "exmo", "exma", "sig", "sigra", "sig.ra", "hr", "г-н", "г-жа",
    "c.p", "s.f", "n.º", "n.°",
}

UNIVERSAL_CLASS_B: Set[str] = {
    "cap", "pag", "pág", "pags", "págs", "p", "pp", "vol", "v",
    "art", "sec", "ed", "éd", "séc", "fig", "tab", "eq",
    "no", "num", "nro", "núm", "nº", "n°", "n", "km", "s", "str",
    "kl", "ул", "д",
}

UNIVERSAL_CLASS_C: Set[str] = {
    "etc", "ecc", "usw", "a.m", "p.m", "min", "seg", "osv",
}

ABREVIACOES_CLASSES: Dict[str, Dict[str, Set[str]]] = {
    "classe_a": {
        "pt": {"dr", "dra", "eng", "enga", "prof", "profa", "sr", "sra", "srta", "exmo", "exma", "av", "rod", "cia", "ltda", "s.a", "obs", "apto", "tel", "ram", "ca", "c.p", "s.f", "n.º", "n.°", "p.ex", "univ", "dept", "depto", "inst", "fac", "assoc", "gov", "pres", "sen", "ilmo", "ilma", "pe", "sto", "sta"},
        "en": {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "ave", "blvd", "rd", "inc", "ltd", "corp", "co", "dept", "approx", "vs", "u.s", "u.s.a", "u.k", "gen", "rep", "sen", "ca", "c.p", "s.f"},
        "es": {"dr", "dra", "sr", "sra", "srta", "prof", "profa", "av", "c/", "cia", "apdo", "c.p", "dña", "dño", "tte", "ej", "ud", "uds", "ca", "s.f", "n.º", "p.ej"},
        "fr": {"m", "mme", "mlle", "dr", "prof", "av", "bd", "soc", "cie", "st", "ste", "ca", "c.p", "s.f", "n.º"},
        "de": {"dr", "prof", "hr", "fr", "str", "inkl", "bzw", "vgl", "z.b", "d.h", "u.a", "tel", "ca", "c.p", "s.f"},
        "it": {"dott", "dott.ssa", "sig", "sig.ra", "prof", "avv", "es", "ca", "c.p", "s.f", "n.º", "n.°"},
        "ru": {"г-н", "г-жа", "д-р", "проф", "ул", "пр", "пер", "им", "см", "т.е", "ca", "c.p", "s.f"},
        "no": {"dr", "prof", "stk", "nr", "hr", "tlf", "dvs", "bl.a", "f.eks", "ca", "c.p", "s.f"},
        "ja": set()
    },
    "classe_b": {
        "pt": {"cap", "pag", "pág", "pags", "págs", "art", "sec", "ed", "vol", "v", "num", "nº", "n°", "n", "p", "pp", "km", "séc", "fig", "tab", "eq"},
        "en": {"no", "p", "pp", "vol", "v", "ch", "chap", "art", "sec", "ed", "fig", "tab", "eq"},
        "es": {"pág", "págs", "pag", "pags", "art", "vol", "v", "cap", "núm", "num", "nro", "n.º", "fig", "tab", "c.p"},
        "fr": {"p", "pp", "vol", "v", "ed", "éd", "art", "chap", "fig", "tab"},
        "de": {"nr", "abs", "s", "bd", "str", "abb", "tab", "tel", "hauptstr", "bahnhofstr"},
        "it": {"pag", "pagg", "art", "v", "cap", "n", "fig", "tab"},
        "ru": {"г", "стр", "кв", "руб", "коп", "д", "рис", "табл"},
        "no": {"s", "kl", "nr", "kap", "fig", "tab"},
        "ja": set()
    },
    "classe_c": {
        "pt": {"etc", "min", "seg"},
        "en": {"etc", "a.m", "p.m"},
        "es": {"etc"},
        "fr": {"etc"},
        "de": {"etc", "usw"},
        "it": {"ecc"},
        "ru": {"т.д", "т.п", "и.т.д", "и.т.п"},
        "no": {"etc", "osv"},
        "ja": set()
    }
}


def get_abbreviation_classes_for_language(
    language: str | None,
    config: Dict[str, Any] | None = None,
    text: str | None = None,
) -> Tuple[Set[str], Set[str], Set[str]]:
    """Retorna a tripla de conjuntos (classe_a, classe_b, classe_c) para o idioma."""
    lang_key = normalize_lang_code(language)
    ca: Set[str] = set()
    cb: Set[str] = set()
    cc: Set[str] = set()

    if lang_key in {"auto", "mixed", "misto"}:
        for l in ["pt", "en", "es", "fr", "de", "it", "ru", "no"]:
            ca |= ABREVIACOES_CLASSES["classe_a"].get(l, set())
            cb |= ABREVIACOES_CLASSES["classe_b"].get(l, set())
            cc |= ABREVIACOES_CLASSES["classe_c"].get(l, set())
        ca |= UNIVERSAL_CLASS_A
        cb |= UNIVERSAL_CLASS_B
        cc |= UNIVERSAL_CLASS_C
    else:
        ca = set(ABREVIACOES_CLASSES["classe_a"].get(lang_key, set())) | UNIVERSAL_CLASS_A
        cb = set(ABREVIACOES_CLASSES["classe_b"].get(lang_key, set())) | UNIVERSAL_CLASS_B
        cc = set(ABREVIACOES_CLASSES["classe_c"].get(lang_key, set())) | UNIVERSAL_CLASS_C
        if lang_key == "pt":
            ca.discard("no")
            cb.discard("no")
            cc.discard("no")

    if config:
        user_custom = config.get("abreviacoes_customizadas_usuario", {})
        if isinstance(user_custom, dict):
            for w in user_custom.get(lang_key, []):
                ca.add(str(w).lower().strip().rstrip("."))

    return (
        frozenset(str(w).lower().strip().rstrip(".") for w in ca),
        frozenset(str(w).lower().strip().rstrip(".") for w in cb),
        frozenset(str(w).lower().strip().rstrip(".") for w in cc),
    )


def get_abbreviations_for_language(
    language: str | None,
    config: Dict[str, Any] | None = None,
    text: str | None = None,
) -> Set[str]:
    """Retorna o conjunto total de abreviações ativas para o idioma (em minúsculas)."""
    ca, cb, cc = get_abbreviation_classes_for_language(language, config, text=text)
    return ca | cb | cc

