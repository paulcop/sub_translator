"""Traduction par lots d'un fichier de sous-titres."""

import json
import os
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import pysubs2
from deep_translator import GoogleTranslator
from deep_translator.exceptions import TooManyRequests

from .subtitles import is_sdh, strip_sdh

# Si une clé DeepL est fournie, elle remplace Google Traduction
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()

CHUNK_CHARS = 1800  # lots plus petits = barre de progression plus fluide
WORKERS = 4
RETRIES = 3

ProgressFn = Callable[[int, int, str, str], None]


def _with_retry(fn, *args):
    for attempt in range(RETRIES):
        try:
            return fn(*args)
        except Exception as exc:
            client_error = isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429
            if client_error or attempt == RETRIES - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


class TranslationError(Exception):
    pass


def _google(lines: list[str], source: str, target: str) -> list[str]:
    translator = GoogleTranslator(source=source, target=target)
    try:
        out = _with_retry(translator.translate, "\n".join(lines)) or ""
        parts = out.split("\n")
        if len(parts) == len(lines):
            return [p.strip() for p in parts]
        # Google a fusionné ou découpé des lignes : on retombe sur du ligne à ligne
        return [(_with_retry(translator.translate, line) or line).strip() for line in lines]
    except TooManyRequests as exc:
        raise TranslationError(
            "Google Traduction bloque temporairement ce serveur (trop de requêtes). "
            "Réessayez plus tard ou configurez DEEPL_API_KEY.") from exc


DEEPL_TARGETS = {"en": "EN-US", "pt": "PT-PT", "zh-CN": "ZH-HANS", "iw": "HE", "no": "NB"}


def _deepl(lines: list[str], source: str, target: str) -> list[str]:
    host = "api-free.deepl.com" if DEEPL_API_KEY.endswith(":fx") else "api.deepl.com"
    params = [("text", line) for line in lines]
    params.append(("target_lang", DEEPL_TARGETS.get(target, target.upper())))
    if source != "auto":
        params.append(("source_lang", source.split("-")[0].replace("iw", "he").upper()))
    req = urllib.request.Request(
        f"https://{host}/v2/translate",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
    )

    def call():
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["translations"]

    try:
        return [t["text"].strip() for t in _with_retry(call)]
    except urllib.error.HTTPError as exc:
        msg = {400: "langue non prise en charge par DeepL", 403: "clé DeepL invalide",
               456: "quota DeepL épuisé"}.get(exc.code, f"erreur DeepL {exc.code}")
        raise TranslationError(msg) from exc


def _translate_chunk(lines: list[str], source: str, target: str) -> list[str]:
    return (_deepl if DEEPL_API_KEY else _google)(lines, source, target)


def _chunks(items: list[str]) -> list[tuple[int, list[str]]]:
    chunks, current, size, start = [], [], 0, 0
    for i, text in enumerate(items):
        if current and size + len(text) > CHUNK_CHARS:
            chunks.append((start, current))
            current, size, start = [], 0, i
        current.append(text)
        size += len(text) + 1
    if current:
        chunks.append((start, current))
    return chunks


def _rewrap(text: str, had_break: bool) -> str:
    """Remet le texte sur deux lignes si l'original l'était et que c'est long."""
    if not had_break or len(text) <= 42:
        return text
    lines = textwrap.wrap(text, width=max(len(text) // 2 + 4, 20))
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    return "\n".join(lines)


def translate_subs(
    subs: pysubs2.SSAFile,
    source: str,
    target: str,
    remove_sdh: bool,
    progress: ProgressFn,
    cancelled: Callable[[], bool] = lambda: False,
) -> int:
    """Traduit `subs` en place. Retourne le nombre de lignes SDH retirées."""
    removed = 0
    if remove_sdh:
        keep = []
        for event in subs:
            if event.is_comment or event.is_drawing:
                keep.append(event)
                continue
            flat = event.plaintext.replace("\n", " ").strip()
            if is_sdh(flat):
                removed += 1
                continue
            cleaned = strip_sdh(flat)
            if not cleaned:
                removed += 1
                continue
            if cleaned != flat:
                event.plaintext = cleaned
            keep.append(event)
        subs.events = keep

    events = [e for e in subs if not e.is_comment and not e.is_drawing and e.plaintext.strip()]
    texts = [e.plaintext.replace("\n", " ").strip() for e in events]
    # Réplique entièrement en italique (voix off, chanson…) : on garde l'italique
    italic = [e.text.startswith("{\\i1}") and "{\\i0}" not in e.text.removesuffix("{\\i0}")
              for e in events]
    had_break = ["\n" in e.plaintext for e in events]

    # Dédoublonnage : chaque phrase n'est traduite qu'une fois
    counts = Counter(texts)
    unique = list(counts)
    translated: dict[str, str] = {}
    total, done = len(texts), 0
    progress(0, total, "", "")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_translate_chunk, chunk, source, target): chunk
                   for _, chunk in _chunks(unique)}
        for fut in as_completed(futures):
            if cancelled():
                pool.shutdown(cancel_futures=True)
                raise RuntimeError("Traduction annulée")
            chunk = futures[fut]
            result = fut.result()
            translated.update(zip(chunk, result))
            done += sum(counts[t] for t in chunk)
            progress(done, total, chunk[-1], result[-1])

    for event, text, it, br in zip(events, texts, italic, had_break):
        new = _rewrap(translated.get(text) or text, br)
        event.plaintext = new
        if it:
            event.text = "{\\i1}" + event.text + "{\\i0}"

    return removed
