"""
Modelos Pydantic para o subsistema de Texto Longo V2 (Fase 3b).
Isolados em arquivo dedicado para manter backend/models.py intocado (Condição C7).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BlockAttempt(BaseModel):
    attempt_idx: int
    seed: Optional[int] = None
    timestamp: float
    status: str
    error: Optional[str] = None


class BlockMetadataResponse(BaseModel):
    idx: int
    paragraph_idx: int
    boundary_type: str
    text: str
    chars: int
    status: str
    wav_rel_path: str
    sha256: Optional[str] = None
    file_bytes: Optional[int] = None
    sample_rate: int = 24000
    audio_dur_s: Optional[float] = None
    gen_time_s: Optional[float] = None
    rtf: Optional[float] = None
    lufs: Optional[float] = None
    attempts: List[BlockAttempt] = Field(default_factory=list)
    error_message: Optional[str] = None


class LongJobProgressResponse(BaseModel):
    generation_id: str
    job_status: str
    total_chunks: int
    completed_chunks: int
    percent: float
    active_chunk_idx: int
    is_locked: bool
    can_resume: bool
    last_beat_s_ago: Optional[float] = None
    error: Optional[str] = None


class ReassembleRequest(BaseModel):
    pause_oracao_ms: Optional[int] = None
    pause_frase_ms: Optional[int] = None
    pause_paragrafo_ms: Optional[int] = None
    target_lufs: Optional[float] = None
    crossfade_ms: Optional[int] = None
    make_default: bool = True


class ResumeRequest(BaseModel):
    pass


class StorageSummaryResponse(BaseModel):
    total_jobs: int
    completed_jobs: int
    interrupted_jobs: int
    total_bytes: int
    total_megabytes: float
