"""Route registration for the voicebox API."""

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    """Include all domain routers on the application."""
    from .health import router as health_router
    from .profiles import router as profiles_router
    from .channels import router as channels_router
    from .generations import router as generations_router
    from .history import router as history_router
    from .transcription import router as transcription_router
    from .llm import router as llm_router
    from .captures import router as captures_router
    from .stories import router as stories_router
    from .effects import router as effects_router
    from .audio import router as audio_router
    from .models import router as models_router
    from .settings import router as settings_router
    from .tasks import router as tasks_router
    from .cuda import router as cuda_router
    from .rocm import router as rocm_router
    from .speak import router as speak_router
    from .mcp_bindings import router as mcp_bindings_router
    from .events import router as events_router
    from .cloud import router as cloud_router
    from .voice_packs import router as voice_packs_router
    from .favoritos import router as favoritos_router
    from .perfil_placa import router as perfil_placa_router

    app.include_router(health_router)
    app.include_router(profiles_router)
    app.include_router(voice_packs_router)
    app.include_router(favoritos_router)
    app.include_router(perfil_placa_router)
    app.include_router(channels_router)
    app.include_router(generations_router)
    app.include_router(history_router)
    app.include_router(transcription_router)
    app.include_router(llm_router)
    app.include_router(captures_router)
    app.include_router(stories_router)
    app.include_router(effects_router)
    app.include_router(audio_router)
    app.include_router(models_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)
    app.include_router(cuda_router)
    app.include_router(rocm_router)
    app.include_router(speak_router)
    app.include_router(mcp_bindings_router)
    app.include_router(events_router)
    app.include_router(cloud_router)

    # Roteador OmniVoice (isolado e com opção única de desativação)
    import os
    if os.environ.get("VOICEBOX_DISABLE_OMNIVOICE", "0").lower() not in ("1", "true", "yes"):
        try:
            from .omnivoice import router as omnivoice_router
            app.include_router(omnivoice_router)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Falha ao registrar roteador do OmniVoice: %s", e)

