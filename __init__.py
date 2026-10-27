"""Top-level package for comfyui_nakagawa."""

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]

__author__ = """nakagawadev"""
__email__ = "hi@nakagawa.dev"
__version__ = "0.0.1"

from .src.comfyui_nakagawa.nodes import NODE_CLASS_MAPPINGS
from .src.comfyui_nakagawa.nodes import NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

try:
    from .src.comfyui_nakagawa import stats

    stats.install()
except Exception:
    import logging

    logging.getLogger("comfyui_nakagawa.stats").warning("stats: install failed, ComfyUI continues without stats", exc_info=True)
