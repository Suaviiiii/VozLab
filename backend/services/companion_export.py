"""
Voicebox Companion MP3 Auto-Exporter.

Monitors user export locations (Desktop, Downloads, Documents, Custom folders, etc.)
and automatically generates a matching high-quality .mp3 companion alongside any .wav file
exported from Voicebox or saved by the user.
"""

import ctypes
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Registry of recent Voicebox export requests: expected_filename -> (audio_path, timestamp)
_recent_exports: Dict[str, Tuple[Path, float]] = {}
_lock = threading.Lock()
_watcher_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# Cache of already processed files: path -> (mtime, size)
_processed_files: Dict[Path, Tuple[float, int]] = {}


def register_export(generation_id: str, audio_path: Path, expected_filename: str) -> None:
    """Register that an export was triggered so we can match the saved destination."""
    with _lock:
        now = time.time()
        # Keep up to 100 recent exports
        if len(_recent_exports) > 100:
            oldest = min(_recent_exports.keys(), key=lambda k: _recent_exports[k][1])
            _recent_exports.pop(oldest, None)

        clean_name = Path(expected_filename).name
        _recent_exports[clean_name] = (audio_path, now)
        logger.info("Companion MP3: registered pending export %s -> %s", clean_name, audio_path)


def wav_to_mp3(wav_path: Path | str, mp3_path: Optional[Path | str] = None) -> Optional[Path]:
    """Convert a WAV file to MP3 format using soundfile with FFmpeg fallback."""
    import soundfile as sf

    wav_p = Path(wav_path)
    if not wav_p.is_file():
        return None

    if mp3_path is None:
        mp3_p = wav_p.with_suffix(".mp3")
    else:
        mp3_p = Path(mp3_path)

    # If MP3 already exists and is not empty, nothing to do
    try:
        if mp3_p.is_file() and mp3_p.stat().st_size > 0:
            return mp3_p
    except OSError:
        pass

    temp_mp3 = mp3_p.parent / f"{mp3_p.name}.tmp_{int(time.time()*1000)}"

    # Method 1: Soundfile (fast, in-process, no subprocess overhead)
    try:
        mp3_p.parent.mkdir(parents=True, exist_ok=True)
        data, sr = sf.read(str(wav_p))
        sf.write(str(temp_mp3), data, sr, format="MP3")
        os.replace(str(temp_mp3), str(mp3_p))
        logger.info("Companion MP3: successfully created %s (via libsndfile)", mp3_p.name)
        return mp3_p
    except Exception as e:
        logger.debug("libsndfile failed for %s: %s, trying FFmpeg", wav_p.name, e)
        if temp_mp3.exists():
            try:
                temp_mp3.unlink()
            except Exception:
                pass

    # Method 2: FFmpeg fallback
    import subprocess
    import sys
    ffmpeg_bin = Path(sys.executable).parent / "ffmpeg.exe"  # o do próprio ambiente Python (antes: caminho fixo)
    if not ffmpeg_bin.is_file():
        found = shutil.which("ffmpeg")
        if found:
            ffmpeg_bin = Path(found)

    if ffmpeg_bin.is_file():
        try:
            res = subprocess.run(
                [
                    str(ffmpeg_bin),
                    "-y",
                    "-i",
                    str(wav_p),
                    "-codec:a",
                    "libmp3lame",
                    "-qscale:a",
                    "2",
                    str(temp_mp3),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )
            if res.returncode == 0 and temp_mp3.is_file() and temp_mp3.stat().st_size > 0:
                os.replace(str(temp_mp3), str(mp3_p))
                logger.info("Companion MP3: successfully created %s (via FFmpeg)", mp3_p.name)
                return mp3_p
        except Exception as e:
            logger.warning("FFmpeg conversion failed for %s: %s", wav_p.name, e)
            if temp_mp3.exists():
                try:
                    temp_mp3.unlink()
                except Exception:
                    pass

    return None


def get_monitored_directories() -> Set[Path]:
    """Collect standard user directories where audio exports are frequently saved."""
    dirs: Set[Path] = set()

    user_home = Path.home()
    candidates = [
        user_home / "OneDrive" / "Desktop",
        user_home / "Desktop",
        user_home / "Downloads",
        user_home / "OneDrive" / "Documentos",
        user_home / "OneDrive" / "Documents",
        user_home / "Documents",
        user_home / "Music",
        user_home / "OneDrive" / "Music",
        *[Path(x) for x in os.environ.get("VOZLAB_PASTAS_EXTRAS", "").split(";") if x.strip()],
        Path(os.environ.get("APPDATA", "")) / "sh.voicebox.app" / "generations",
    ]

    for c in candidates:
        try:
            if c.is_dir():
                dirs.add(c)
        except Exception:
            pass

    # Inspect Windows Common Dialog MRU registry keys for recent Save/Open folders
    if os.name == "nt":
        try:
            import winreg
            shell32 = ctypes.windll.shell32
            for ext in ["wav", "*", "mp3"]:
                key_path = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32\OpenSavePidlMRU\{ext}"
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                        i = 0
                        while True:
                            try:
                                name, val, typ = winreg.EnumValue(key, i)
                                i += 1
                                if isinstance(val, bytes) and len(val) > 20:
                                    buf = ctypes.create_unicode_buffer(1024)
                                    if shell32.SHGetPathFromIDListW(val, buf):
                                        p = Path(buf.value)
                                        if p.is_dir():
                                            dirs.add(p)
                                        elif p.parent.is_dir():
                                            dirs.add(p.parent)
                            except OSError:
                                break
                except Exception:
                    pass
        except Exception:
            pass

    return dirs


def process_wav_file(wav_path: Path) -> None:
    """Examine a WAV file and create its matching MP3 companion if missing."""
    try:
        if not wav_path.is_file() or wav_path.suffix.lower() != ".wav":
            return

        stat = wav_path.stat()
        if stat.st_size == 0:
            return  # File still being written / empty

        # Check if already processed with this mtime and size
        last = _processed_files.get(wav_path)
        if last is not None and last == (stat.st_mtime, stat.st_size):
            return

        mp3_path = wav_path.with_suffix(".mp3")
        if mp3_path.is_file() and mp3_path.stat().st_size > 0:
            _processed_files[wav_path] = (stat.st_mtime, stat.st_size)
            return

        # Check if this file is currently locked/being written by another process
        # Wait until it can be opened for reading
        try:
            with open(wav_path, "rb") as f:
                header = f.read(12)
                if len(header) < 12 or not header.startswith(b"RIFF"):
                    # Not a completed RIFF/WAV file yet
                    return
        except OSError:
            # File locked by writing process, will retry on next loop
            return

        # Try fast copy from pre-generated internal MP3 if matched with recent export
        copied = False
        with _lock:
            for exp_name, (src_audio, ts) in list(_recent_exports.items()):
                # Match filename or prefix
                if exp_name == wav_path.name or wav_path.stem.startswith(Path(exp_name).stem[:20]):
                    src_mp3 = src_audio.with_suffix(".mp3")
                    if src_mp3.is_file() and src_mp3.stat().st_size > 0:
                        try:
                            shutil.copyfile(src_mp3, mp3_path)
                            copied = True
                            logger.info("Companion MP3: fast-copied from cache %s -> %s", src_mp3.name, mp3_path)
                            break
                        except Exception:
                            pass

        if not copied:
            wav_to_mp3(wav_path, mp3_path)

        _processed_files[wav_path] = (stat.st_mtime, stat.st_size)

    except Exception as e:
        logger.debug("Error processing wav file %s: %s", wav_path, e)


def _watcher_loop() -> None:
    """Background loop to check monitored directories for newly exported WAV files."""
    logger.info("Companion MP3: watcher thread started")
    while not _stop_event.is_set():
        try:
            dirs = get_monitored_directories()
            for d in dirs:
                if not d.is_dir():
                    continue
                try:
                    # Scan top-level files in directory
                    for entry in os.scandir(d):
                        if entry.is_file() and entry.name.lower().endswith(".wav"):
                            process_wav_file(Path(entry.path))
                except (PermissionError, OSError):
                    continue

        except Exception as e:
            logger.debug("Companion MP3 loop exception: %s", e)

        # Sleep briefly between sweeps
        _stop_event.wait(1.0)


def start_companion_watcher() -> None:
    """Start the background watcher thread if not already running."""
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return

    _stop_event.clear()
    _watcher_thread = threading.Thread(target=_watcher_loop, daemon=True, name="CompanionMP3Watcher")
    _watcher_thread.start()
    logger.info("Companion MP3: background watcher initialized")


def stop_companion_watcher() -> None:
    """Stop the background watcher thread."""
    _stop_event.set()
