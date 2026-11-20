"""Helpers for media coming from clients (base64 now, URLs later). No ComfyUI/torch imports, so CI can test it.

Untrusted bytes must never reach ffmpeg's format probing: a probed HLS playlist or
concat list makes ffmpeg open other URLs and local files. sniff() picks the demuxer
from the magic bytes and the caller passes it explicitly to av.open(format=...).
"""
import base64
import binascii

AUDIO = "audio"
VIDEO = "video"

# ffmpeg demuxer name -> kinds of media it can hold
_KINDS = {
    "wav": {AUDIO},
    "flac": {AUDIO},
    "mp3": {AUDIO},
    "aac": {AUDIO},
    "ogg": {AUDIO},
    "mp4": {AUDIO, VIDEO},       # mov/mp4/m4a demuxer
    "matroska": {AUDIO, VIDEO},  # mkv/webm demuxer
}


def b64decode(data):
    """Raw base64 or a data URI (everything up to the comma is dropped)."""
    try:
        return base64.b64decode(data.split(",", 1)[-1].strip(), validate=False)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Invalid base64: {e}") from None


def sniff(raw, kind):
    """ffmpeg demuxer for these bytes, or ValueError if it isn't an allowed `kind` container."""
    fmt = _detect(raw[:16])
    if fmt is None or kind not in _KINDS[fmt]:
        raise ValueError(f"Unsupported {kind} format (allowed: {', '.join(f for f, k in _KINDS.items() if kind in k)})")
    return fmt


def _detect(head):
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:  # ADTS sync, layer 00
            return "aac"
        if head[1] & 0xE0 == 0xE0 and head[1] & 0x06:  # MPEG audio sync, layer != 00
            return "mp3"
    return None
