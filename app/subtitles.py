"""Lecture, extraction et nettoyage des sous-titres.

Rien n'est écrit de façon durable : les vidéos sont passées à ffmpeg en flux
(stdin) et les pistes extraites sont relues en mémoire puis supprimées.
"""

import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import pysubs2

TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
VIDEO_EXTS = {".mkv", ".mk3d", ".webm", ".mp4", ".m4v", ".mov", ".avi", ".ts"}
# Formats lisibles en flux (l'en-tête suffit à connaître les pistes)
STREAMABLE_EXTS = {".mkv", ".mk3d", ".webm", ".ts"}

PROBE_BYTES = 16 * 1024 * 1024

# Codes ISO 639-2 (tags des pistes) -> codes Google Translate
ISO639_2 = {
    "fre": "fr", "fra": "fr", "eng": "en", "spa": "es", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "dut": "nl", "nld": "nl", "rus": "ru", "jpn": "ja",
    "chi": "zh-CN", "zho": "zh-CN", "kor": "ko", "ara": "ar", "pol": "pl",
    "tur": "tr", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "gre": "el",
    "ell": "el", "heb": "iw", "hin": "hi", "cze": "cs", "ces": "cs", "hun": "hu",
    "rum": "ro", "ron": "ro", "ukr": "uk", "vie": "vi", "tha": "th", "ind": "id",
}


@dataclass
class Track:
    index: int
    codec: str
    language: str | None
    title: str | None
    content: str = ""
    fmt: str = "srt"
    lines: int = 0
    sample: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "index": self.index,
            "codec": self.codec,
            "language": self.language,
            "title": self.title,
            "format": self.fmt,
            "lines": self.lines,
            "sample": self.sample,
        }


class ExtractionError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    # ffmpeg et les fichiers Windows écrivent des \r\n
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load(text: str, fmt: str | None = None) -> pysubs2.SSAFile:
    try:
        return pysubs2.SSAFile.from_string(text, format_=fmt)
    except Exception as exc:  # pysubs2 lève des erreurs variées
        raise ExtractionError(f"Fichier de sous-titres illisible ({exc})") from exc


def describe(track: Track) -> Track:
    subs = load(track.content, track.fmt)
    dialogue = [e for e in subs if not e.is_comment and not e.is_drawing and e.plaintext.strip()]
    track.lines = len(dialogue)
    # Quelques vraies répliques (pas des descriptions SDH) pour l'aperçu
    texts = (e.plaintext.replace("\n", " ").strip() for e in dialogue[len(dialogue) // 10:])
    track.sample = [t for t in texts if len(t) > 12 and not is_sdh(strip_sdh(t))][:3]
    return track


def track_from_file(filename: str, raw: bytes) -> Track:
    ext = Path(filename).suffix.lower()
    fmt = {".srt": "srt", ".ass": "ass", ".ssa": "ssa", ".vtt": "vtt", ".sub": "microdvd"}.get(ext)
    text = decode(raw)
    subs = load(text, fmt)
    fmt = subs.format or fmt or "srt"
    return describe(Track(index=0, codec=fmt, language=None, title=None, content=text, fmt=fmt))


# ------------------------------------------------------------ extraction

def _probe(source: str | bytes) -> list[dict]:
    """Liste les pistes de sous-titres via ffprobe (chemin ou début du fichier)."""
    is_bytes = isinstance(source, bytes)
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         "-select_streams", "s", "pipe:0" if is_bytes else source],
        input=source if is_bytes else None,
        capture_output=True,
    )
    try:
        return json.loads(proc.stdout or b"{}").get("streams", [])
    except json.JSONDecodeError:
        return []


def _tracks_from_probe(streams: list[dict]) -> tuple[list[Track], int]:
    tracks, skipped = [], 0
    for pos, s in enumerate(streams):
        codec = s.get("codec_name", "?")
        if codec not in TEXT_CODECS:
            skipped += 1  # PGS / VobSub : images, pas de texte à traduire
            continue
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        lang = tags.get("language")
        fmt = "ass" if codec in ("ass", "ssa") else "srt"
        tracks.append(Track(index=pos, codec=codec, language=ISO639_2.get(lang, lang),
                            title=tags.get("title"), fmt=fmt))
    return tracks, skipped


def _ffmpeg_cmd(input_arg: str, tracks: list[Track], outdir: str) -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if input_arg != "pipe:0":
        cmd.append("-nostdin")
    cmd += ["-i", input_arg]
    for t in tracks:
        cmd += ["-map", f"0:s:{t.index}", "-c:s", t.fmt if t.fmt == "ass" else "srt",
                "-f", t.fmt, f"{outdir}/{t.index}.{t.fmt}"]
    return cmd


def _collect(tracks: list[Track], outdir: str) -> list[Track]:
    result = []
    for t in tracks:
        path = Path(outdir) / f"{t.index}.{t.fmt}"
        if path.exists() and path.stat().st_size:
            t.content = decode(path.read_bytes())
            result.append(describe(t))
    return result


async def extract_from_stream(filename: str, chunks: AsyncIterator[bytes]) -> tuple[list[Track], int]:
    """Extrait toutes les pistes texte d'une vidéo reçue en flux.

    MKV/WebM/TS : le flux est envoyé directement dans ffmpeg, la vidéo n'est
    jamais écrite. Autres conteneurs (MP4…) : l'index peut être en fin de
    fichier, on passe alors par un fichier temporaire supprimé aussitôt.
    """
    ext = Path(filename).suffix.lower()

    with tempfile.TemporaryDirectory(prefix="subs-") as outdir:
        if ext not in STREAMABLE_EXTS:
            video = Path(outdir) / f"input{ext}"
            with video.open("wb") as f:
                async for chunk in chunks:
                    f.write(chunk)
            tracks, skipped = _tracks_from_probe(_probe(str(video)))
            if tracks:
                await asyncio.to_thread(
                    subprocess.run, _ffmpeg_cmd(str(video), tracks, outdir), capture_output=True)
            video.unlink(missing_ok=True)
            return _collect(tracks, outdir), skipped

        # Lecture du début du fichier pour connaître les pistes
        head = bytearray()
        it = chunks.__aiter__()
        done = False
        while len(head) < PROBE_BYTES:
            try:
                head += await it.__anext__()
            except StopAsyncIteration:
                done = True
                break
        tracks, skipped = _tracks_from_probe(_probe(bytes(head)))
        if not tracks:
            async for _ in it:  # on vide le flux sans le garder
                pass
            return [], skipped

        proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_cmd("pipe:0", tracks, outdir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            try:
                proc.stdin.write(bytes(head))
                await proc.stdin.drain()
                if not done:
                    async for chunk in it:
                        proc.stdin.write(chunk)
                        await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # ffmpeg s'est arrêté avant la fin : on garde ce qui a été extrait
            await proc.wait()
            return _collect(tracks, outdir), skipped
        finally:
            # Envoi interrompu par le navigateur : on ne laisse pas ffmpeg tourner
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# --------------------------------------------------- nettoyage SDH (malentendants)

# Description musicale courte : "Intriguing music", "Musique intense"
_MUSIC = r"(?:[A-Z][a-z]+(?: [a-z]+){0,2} music|Musique(?: [a-zéèêà]+){0,3})"
_SOUNDS = (r"(?:laughter|applause|cheers|screams? of joy|clapping|silence|crowd noise|"
           r"whistling|bell ringing|rires?|applaudissements|soupirs?|cris)")

SDH_LINE = [
    re.compile(r"^\s*\.{2,}\s*$"),
    re.compile(rf"^\s*{_MUSIC}\s*$"),
    re.compile(rf"^\s*{_SOUNDS}\s*$", re.IGNORECASE),
    re.compile(r"^\s*[\[(][^\])]*[\])]\s*$"),  # [Rires], (musique)…
    re.compile(r"^\s*[♪♫#\s]+$"),
]
SDH_INLINE = [
    (re.compile(rf"\s+{_MUSIC}\s*$"), ""),
    (re.compile(r"\s*\b(?:laughter|screams? of joy|applause)\b\s*", re.IGNORECASE), " "),
    (re.compile(r"\s*[\[(][A-ZÀ-Ý\s,.'-]+[\])]\s*"), " "),  # [PORTE QUI CLAQUE]
    (re.compile(r"^[A-ZÀ-Ý][A-ZÀ-Ý\s'-]{1,20}:\s+"), ""),  # JOHN: réplique
]


def is_sdh(text: str) -> bool:
    return any(p.match(text) for p in SDH_LINE)


def strip_sdh(text: str) -> str:
    for pattern, repl in SDH_INLINE:
        text = pattern.sub(repl, text)
    return re.sub(r"\s{2,}", " ", text).strip()
