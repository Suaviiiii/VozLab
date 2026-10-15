"""Perfil da placa de vídeo (Missão GPU por famílias, fase GPU 2): só informa, nunca bloqueia."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/gpu/perfil")
async def get_perfil_placa():
    """Família da placa, classe de VRAM (4/6/8 GB), avisos e a situação de cada modelo."""
    from ..services import perfil_placa

    return perfil_placa.detectar()
