#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
translate_serowsour_fixed.py
Versione debug e migliorata:
- barra di progresso per le fasi (mostra 👍 alla fine)
- compatibility_check() per verificare l'ambiente
- retry e fallback migliorati per i motori di traduzione
- logging più dettagliato
"""

import argparse
import concurrent.futures
import logging
import re
import shutil
import sys
import threading
import time
import socket
from pathlib import Path
from typing import Any, Dict

import yaml

# Prova a importare googletrans, fallback a deep-translator se disponibile
try:
    from googletrans import Translator as GTTranslator
except Exception:
    GTTranslator = None

try:
    from deep_translator import GoogleTranslator as DTTranslator
except Exception:
    DTTranslator = None

# -----------------------
# Configurazione
# -----------------------
DEFAULT_LANG = "it"
PROGRESS_WIDTH = 36
MAX_RETRIES = 3
TRANSLATE_TIMEOUT = 10  # secondi per chiamata di traduzione
RETRY_BACKOFF = 1.5  # moltiplicatore backoff

MINECRAFT_TERMS = {
    "Land", "land", "Chunk", "Chunks", "chunk", "chunks", "Biome", "biomes",
    "Nether", "End", "Overworld", "PvP", "PVP", "Cooldown", "Cooldowns",
    "Claim", "Claims", "claim", "claims", "Unclaim", "unclaim", "Spawn",
    "Mob", "Mobs", "XP", "Health", "Mana", "Region", "Block", "Blocks",
    "Item", "Items", "Inventory", "Server", "Player", "Players", "World",
    "Worlds"
}
LOWER_TERMS = {t.lower() for t in MINECRAFT_TERMS}

PLACEHOLDER_RE = re.compile(r"(\{[^}]+\}|%[^%\s]+%|\$[A-Za-z0-9_]+)")
TERMS_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(MINECRAFT_TERMS, key=len, reverse=True)) + r")\b",
    flags=re.IGNORECASE
)

# -----------------------
# Progress bar per fasi
# -----------------------
def progress_bar_phase(message: str, duration: float = 0.6):
    """
    Mostra una barra di progresso animata per la durata stimata.
    Usala per dare feedback durante le fasi. Alla fine stampa 👍.
    """
    steps = PROGRESS_WIDTH
    sys.stdout.write(f"{message} ")
    sys.stdout.flush()
    for i in range(steps + 1):
        filled = i
        bar = "█" * filled + "-" * (PROGRESS_WIDTH - filled)
        percent = int((filled / PROGRESS_WIDTH) * 100)
        sys.stdout.write(f"\r{message} [{bar}] {percent:3d}%")
        sys.stdout.flush()
        time.sleep(duration / max(1, steps))
    sys.stdout.write("  👍\n")
    sys.stdout.flush()

# -----------------------
# Logging
# -----------------------
logger = logging.getLogger("translate_serowsour")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
logger.addHandler(handler)

# -----------------------
# File helpers
# -----------------------
def find_file(path_str: str) -> Path:
    p = Path(path_str)
    if p.exists():
        return p
    candidates = [
        Path("/storage/emulated/0") / path_str,
        Path("/sdcard") / path_str,
        Path("/storage/emulated/0/Download") / path_str,
        Path("/sdcard/Download") / path_str,
        Path.cwd() / path_str,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def backup_file(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


# -----------------------
# YAML fixer (intelligente, non distruttivo)
# -----------------------
def fix_yaml_content(content: str) -> str:
    fixed_lines = []
    for line in content.splitlines():
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("#") or ":" not in stripped:
            fixed_lines.append(line)
            continue

        key_part, val_part = stripped.split(":", 1)
        key = key_part.rstrip()
        val = val_part.lstrip()

        if val == "" or val.startswith(("'", '"')) or val.startswith("|") or val.startswith(">"):
            fixed_lines.append(line)
            continue

        if any(c in val for c in ["&", ":", "'"]):
            if "'" in val and '"' not in val:
                safe_val = "'" + val.replace("'", "''") + "'"
            else:
                safe_val = '"' + val.replace('\\', '\\\\').replace('"', '\\"') + '"'
            new_line = f"{indent}{key}: {safe_val}"
            fixed_lines.append(new_line)
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines)


def load_yaml_with_fix(path: Path, make_backup: bool = True) -> Any:
    raw = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("YAML non valido: %s", e)
        if make_backup:
            bak = backup_file(path)
            logger.info("Backup creato: %s", bak.name)
        logger.info("Provo a correggere automaticamente il file YAML (pre-translation fix)...")
        fixed = fix_yaml_content(raw)
        try:
            return yaml.safe_load(fixed)
        except Exception as e2:
            logger.error("Correzione automatica fallita: %s", e2)
            raise

# -----------------------
# Masking / Unmasking
# -----------------------
def mask_text(text: str, mapping: Dict[str, str]) -> str:
    token_index = len(mapping)

    def repl_ph(m):
        nonlocal token_index
        token = f"__PH{token_index}__"
        mapping[token] = m.group(0)
        token_index += 1
        return token

    text = PLACEHOLDER_RE.sub(repl_ph, text)

    def repl_term(m):
        nonlocal token_index
        original = m.group(0)
        if original.lower() in LOWER_TERMS:
            token = f"__MT{token_index}__"
            mapping[token] = original
            token_index += 1
            return token
        return original

    return TERMS_PATTERN.sub(repl_term, text)


def unmask_text(text: str, mapping: Dict[str, str]) -> str:
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text

# -----------------------
# Traduttore con fallback e timeout migliorato
# -----------------------
def translate_via_googletrans(text: str, dest: str) -> str:
    if GTTranslator is None:
        raise RuntimeError("googletrans non disponibile")
    t = GTTranslator()
    res = t.translate(text, dest=dest)
    # googletrans può restituire oggetti diversi a seconda della versione
    if hasattr(res, "text"):
        return res.text
    if isinstance(res, dict):
        # alcune versioni possono restituire dict
        return res.get("translatedText") or res.get("text") or str(res)
    return str(res)


def translate_via_deep_translator(text: str, dest: str) -> str:
    if DTTranslator is None:
        raise RuntimeError("deep-translator non disponibile")
    return DTTranslator(source="auto", target=dest).translate(text)


def translate_one(masked_text: str, dest: str, timeout: int = TRANSLATE_TIMEOUT) -> str:
    """
    Tenta la traduzione con più motori, retry e backoff.
    Restituisce la stringa tradotta o solleva eccezione.
    """
    engines = []
    if GTTranslator is not None:
        engines.append(("googletrans", translate_via_googletrans))
    if DTTranslator is not None:
        engines.append(("deep-translator", translate_via_deep_translator))

    if not engines:
        raise RuntimeError("Nessun motore di traduzione disponibile (installa googletrans o deep-translator)")

    last_exc = None
    for name, fn in engines:
        attempt = 0
        backoff = 1.0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(fn, masked_text, dest)
                    return fut.result(timeout=timeout)
            except Exception as e:
                last_exc = e
                logger.debug("Motore %s attempt %d fallito: %s", name, attempt, e)
                time.sleep(backoff)
                backoff *= RETRY_BACKOFF
        logger.info("Motore %s esaurito, passo al successivo", name)
    raise last_exc

# -----------------------
# Funzioni di traduzione con masking, retry e logging
# -----------------------
PROGRESS = {"translated": 0, "skipped": 0}


def translate_string(s: str, dest: str, max_retries: int = MAX_RETRIES) -> str:
    if not isinstance(s, str) or not s.strip():
        PROGRESS["skipped"] += 1
        return s

    mapping: Dict[str, str] = {}
    masked = mask_text(s, mapping)

    # se il testo è solo placeholder/token, non tradurre
    if re.fullmatch(r"(?:(?:__PH\d+__)|(?:__MT\d+__))+", masked or ""):
        PROGRESS["skipped"] += 1
        return unmask_text(masked, mapping)

    last_result = unmask_text(masked, mapping)
    for attempt in range(1, max_retries + 1):
        try:
            translated_masked = translate_one(masked, dest)
            final = unmask_text(translated_masked, mapping)
            PROGRESS["translated"] += 1
            return final
        except Exception as e:
            logger.warning("Tentativo %d fallito per stringa: %s", attempt, e)
            last_result = unmask_text(masked, mapping)
            time.sleep(1)
    PROGRESS["skipped"] += 1
    logger.info("Stringa saltata dopo %d tentativi", max_retries)
    return last_result


def translate_value(val: Any, dest: str) -> Any:
    if isinstance(val, str):
        return translate_string(val, dest)
    if isinstance(val, dict):
        return {k: translate_value(v, dest) for k, v in val.items()}
    if isinstance(val, list):
        return [translate_value(x, dest) for x in val]
    return val

# -----------------------
# Post-fix: correzioni dopo traduzione
# -----------------------
def post_fix_translated_content(obj: Any) -> Any:
    if isinstance(obj, str):
        s = obj
        s = s.replace("''", "’") if "''" in s else s
        words = s.split()
        for i, w in enumerate(words):
            lw = re.sub(r"[^\w]", "", w).lower()
            if lw in LOWER_TERMS:
                for orig in MINECRAFT_TERMS:
                    if orig.lower() == lw:
                        words[i] = re.sub(r"\b" + re.escape(re.sub(r"[^\w]", "", w)) + r"\b", orig, words[i])
                        break
        return " ".join(words)
    if isinstance(obj, dict):
        return {k: post_fix_translated_content(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [post_fix_translated_content(x) for x in obj]
    return obj

# -----------------------
# Ultimo controllo (sanity)
# -----------------------
def final_sanity_check(obj: Any) -> bool:
    problems = []

    def walk(x, path="root"):
        if x is None:
            problems.append(f"{path} is None")
            return
        if isinstance(x, str):
            if "__PH" in x or "__MT" in x:
                problems.append(f"{path} contiene token non sostituiti")
            return
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{path}.{k}")
            return
        if isinstance(x, list):
            for idx, v in enumerate(x):
                walk(v, f"{path}[{idx}]")
            return

    walk(obj)
    if problems:
        logger.warning("Final sanity check: problemi trovati:")
        for p in problems:
            logger.warning(" - %s", p)
        return False
    return True

# -----------------------
# Compatibility check
# -----------------------
def compatibility_check() -> Dict[str, Any]:
    """
    Controlla le dipendenze e l'ambiente e restituisce un dizionario con lo stato.
    """
    info = {}
    info["python_version"] = sys.version.splitlines()[0]
    info["platform"] = sys.platform
    info["yaml_installed"] = True
    info["googletrans_installed"] = GTTranslator is not None
    info["deep_translator_installed"] = DTTranslator is not None

    # semplice test di rete (DNS lookup)
    try:
        socket.gethostbyname("google.com")
        info["network_ok"] = True
    except Exception:
        info["network_ok"] = False

    # Termux detection
    info["is_termux"] = any(p in sys.executable.lower() for p in ("com.termux", "/data/data/com.termux"))
    # WSL detection
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            ver = f.read().lower()
            info["is_wsl"] = "microsoft" in ver
    except Exception:
        info["is_wsl"] = False

    return info

def print_compatibility(info: Dict[str, Any]):
    print("=== Compatibility check ===")
    print(f"Python: {info['python_version']}")
    print(f"Platform: {info['platform']}")
    print(f"yaml installed: {'yes' if info.get('yaml_installed') else 'no'}")
    print(f"googletrans installed: {'yes' if info.get('googletrans_installed') else 'no'}")
    print(f"deep-translator installed: {'yes' if info.get('deep_translator_installed') else 'no'}")
    print(f"Network DNS ok: {'yes' if info.get('network_ok') else 'no'}")
    if info.get("is_termux"):
        print("Environment: Termux detected. Note: Termux cannot build APKs.")
    if info.get("is_wsl"):
        print("Environment: WSL detected. You can install build tools inside WSL.")
    print("===========================")

# -----------------------
# Main flow (fasi a→f)
# -----------------------
def main():
    parser = argparse.ArgumentParser(description="translate_serowsour.py - traduttore YAML con correzione intelligente")
    parser.add_argument("-i", "--input", required=True, help="File YAML di input")
    parser.add_argument("-o", "--output", default=None, help="File YAML di output (default: input_LANG.yml)")
    parser.add_argument("-l", "--lang", default=DEFAULT_LANG, help="Lingua di destinazione (es: it)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose console")
    parser.add_argument("-nobackup", action="store_true", help="Non creare backup automatico")
    parser.add_argument("--check", action="store_true", help="Esegui solo compatibility check")
    args = parser.parse_args()

    print("script made by SerowSour (fixed)")
    if args.verbose:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)

    # compatibility check
    comp = compatibility_check()
    print_compatibility(comp)
    if args.check:
        print("Eseguito compatibility check. Esco.")
        return

    input_path = find_file(args.input)
    if not input_path:
        print("❌ File di input non trovato:", args.input)
        sys.exit(2)

    if args.output:
        output_path = Path(args.output)
    else:
        stem = input_path.stem
        suffix = input_path.suffix or ".yml"
        output_name = f"{stem}_{args.lang}{suffix}"
        output_path = input_path.with_name(output_name)

    # FASE a
    phase_msg = "FASE a) pre-fix (controllo e correzione file non tradotto)"
    progress_bar_phase(phase_msg, duration=0.6)
    try:
        data = load_yaml_with_fix(input_path, make_backup=not args.nobackup)
    except Exception as e:
        print(f"{phase_msg} -> ERRORE: impossibile caricare YAML: {e}")
        sys.exit(3)

    # FASE b
    phase_msg = "FASE b) traduzione in corso"
    progress_bar_phase(phase_msg, duration=0.8)
    try:
        translated = translate_value(data, args.lang)
    except Exception as e:
        print(f"{phase_msg} -> ERRORE: {e}")
        sys.exit(4)

    # FASE c
    phase_msg = "FASE c) post-fix parziale (correzioni su traduzioni)"
    progress_bar_phase(phase_msg, duration=0.5)
    try:
        translated = post_fix_translated_content(translated)
    except Exception as e:
        print(f"{phase_msg} -> ERRORE: {e}")
        sys.exit(5)

    # FASE d
    phase_msg = "FASE d) post-fix completo (sanity & cleanup)"
    progress_bar_phase(phase_msg, duration=0.4)
    try:
        # placeholder per eventuali correzioni estese
        pass
    except Exception as e:
        print(f"{phase_msg} -> ERRORE: {e}")
        sys.exit(6)

    # FASE e
    phase_msg = "FASE e) ultimo controllo (sanity)"
    progress_bar_phase(phase_msg, duration=0.4)
    ok = final_sanity_check(translated)
    if not ok:
        logger.warning("Sono stati rilevati problemi nel controllo finale. Controlla il log.")

    # Salva output
    try:
        output_path.write_text(yaml.dump(translated, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"File di output creato: {output_path.name}")
    except Exception as e:
        logger.error("Errore scrittura output: %s", e)
        print("❌ Errore scrivendo il file di output:", e)
        sys.exit(7)

    print("Finish👍")

if __name__ == "__main__":
    main()