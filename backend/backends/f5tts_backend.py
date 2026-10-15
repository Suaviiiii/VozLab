"""
F5-TTS backend implementation.

Wraps SWivid/F5-TTS (Flow-Matching Diffusion Transformer) for zero-shot
voice cloning without hallucinations, skipped words, or looping audio.
Optimized for AMD Radeon GPUs (ROCm / HIP) and NVIDIA GPUs on Windows/Linux.
"""

import asyncio
import logging
import threading
from typing import ClassVar, List, Optional, Tuple
from pathlib import Path

import numpy as np

from . import TTSBackend
from .base import (
    is_model_cached,
    get_torch_device,
    empty_device_cache,
    manual_seed,
    combine_voice_prompts as _combine_voice_prompts,
    model_load_progress,
)

logger = logging.getLogger(__name__)

F5_HF_REPO = "SWivid/F5-TTS"

_F5_WEIGHT_FILES = [
    "F5TTS_v1_Base/model_1250000.safetensors",
]


def _ensure_audio_loader_patch():
    """Ensure torchaudio.load falls back to soundfile to avoid torchcodec DLL issues on Windows."""
    import torchaudio
    import soundfile as sf
    import torch

    if getattr(torchaudio, "_f5_soundfile_patched", False):
        return

    orig_load = torchaudio.load

    def _sf_load(filepath, *args, **kwargs):
        try:
            return orig_load(filepath, *args, **kwargs)
        except Exception:
            data, samplerate = sf.read(str(filepath), dtype="float32")
            tensor = torch.from_numpy(data)
            if tensor.ndim == 1:
                tensor = tensor.unsqueeze(0)
            else:
                tensor = tensor.t()
            return tensor, samplerate

    torchaudio.load = _sf_load
    torchaudio._f5_soundfile_patched = True


class F5TTSBackend:
    """F5-TTS Flow-Matching Diffusion Transformer backend for voice cloning."""

    _load_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self):
        self.model = None
        self.model_size = "default"
        self._device = None
        self._model_load_lock = asyncio.Lock()

    def _get_device(self) -> str:
        """Get best torch device (cuda / rocm / xpu / cpu)."""
        return get_torch_device(allow_xpu=True, allow_directml=False)

    def is_loaded(self) -> bool:
        return self.model is not None

    def _get_model_path(self, model_size: str = "default") -> str:
        return F5_HF_REPO

    def _is_model_cached(self, model_size: str = "default") -> bool:
        return is_model_cached(F5_HF_REPO, required_files=_F5_WEIGHT_FILES)

    async def load_model(self, model_size: str = "default") -> None:
        """Load the F5-TTS model."""
        if self.model is not None:
            return
        async with self._model_load_lock:
            if self.model is not None:
                return
            await asyncio.to_thread(self._load_model_sync)

    def _load_model_sync(self):
        """Synchronous model loading with progress tracking."""
        model_name = "f5-tts"
        is_cached = self._is_model_cached()

        with model_load_progress(model_name, is_cached):
            _ensure_audio_loader_patch()

            import torch
            from f5_tts.api import F5TTS

            device = self._get_device()
            self._device = device
            logger.info(f"Loading F5-TTS on {device}...")

            # AMD ROCm Optimization: configure SDP to Math mode on RDNA3
            # to avoid experimental AOTriton warnings or JIT hangs
            if device == "cuda" and hasattr(torch.backends, "cuda"):
                try:
                    torch.backends.cuda.enable_flash_sdp(False)
                    torch.backends.cuda.enable_mem_efficient_sdp(False)
                    torch.backends.cuda.enable_math_sdp(True)
                except Exception as e:
                    logger.debug(f"Could not tune SDP for ROCm: {e}")

            self.model = F5TTS(device=device)

        logger.info(f"F5-TTS loaded successfully on {self._device}")

    def unload_model(self) -> None:
        """Unload model to free VRAM."""
        if self.model is not None:
            del self.model
            self.model = None

        device = self._device
        self._device = None

        if device:
            empty_device_cache(device)

        logger.info("F5-TTS unloaded")

    async def create_voice_prompt(
        self,
        audio_path: str,
        reference_text: str,
        use_cache: bool = True,
    ) -> Tuple[dict, bool]:
        """
        Create voice prompt for F5-TTS.
        F5-TTS uses reference audio and text directly during flow-matching sampling.
        """
        voice_prompt = {
            "ref_audio": str(audio_path),
            "ref_text": reference_text or "",
        }
        return voice_prompt, False

    async def combine_voice_prompts(
        self,
        audio_paths: List[str],
        reference_texts: List[str],
    ) -> Tuple[np.ndarray, str]:
        return await _combine_voice_prompts(audio_paths, reference_texts, sample_rate=24000)

    async def generate(
        self,
        text: str,
        voice_prompt: dict,
        language: str = "en",
        seed: Optional[int] = None,
        instruct: Optional[str] = None,
    ) -> Tuple[np.ndarray, int]:
        """
        Generate audio using F5-TTS Flow-Matching.

        Returns:
            Tuple of (audio_numpy_array, sample_rate=24000)
        """
        await self.load_model()

        def _generate_sync():
            _ensure_audio_loader_patch()
            import torch

            if seed is not None:
                manual_seed(seed, self._device)

            ref_audio = voice_prompt.get("ref_audio")
            ref_text = voice_prompt.get("ref_text", "")

            logger.info(f"[F5-TTS] Generating audio for text ({len(text)} chars)...")

            # 16 NFE steps provide an optimal balance of speed and clarity on GPU
            wav, sr, _ = self.model.infer(
                ref_file=ref_audio,
                ref_text=ref_text,
                gen_text=text,
                nfe_step=16,
                speed=1.0,
                seed=seed,
            )

            if isinstance(wav, torch.Tensor):
                wav = wav.detach().cpu().numpy()

            wav = np.squeeze(wav).astype(np.float32)
            return wav, int(sr)

        return await asyncio.to_thread(_generate_sync)
