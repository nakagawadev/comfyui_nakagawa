import base64

import pytest

from src.comfyui_nakagawa import media


@pytest.mark.parametrize("head,fmt", [
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
    (b"fLaC\x00\x00\x00\x22", "flac"),
    (b"OggS\x00\x02", "ogg"),
    (b"ID3\x04\x00", "mp3"),
    (b"\xff\xfb\x90\x00", "mp3"),
    (b"\xff\xf1\x50\x80", "aac"),
    (b"\x00\x00\x00\x20ftypM4A ", "mp4"),
    (b"\x1a\x45\xdf\xa3\x9f", "matroska"),
])
def test_sniff_audio(head, fmt):
    assert media.sniff(head, media.AUDIO) == fmt


def test_sniff_video_rejects_audio_only_containers():
    assert media.sniff(b"\x00\x00\x00\x20ftypisom", media.VIDEO) == "mp4"
    with pytest.raises(ValueError):
        media.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ", media.VIDEO)


@pytest.mark.parametrize("head", [
    b"#EXTM3U\n#EXT-X-VERSION:3\n",  # HLS playlist: would make ffmpeg fetch URLs / local files
    b"ffconcat version 1.0\n",
    b"\x89PNG\r\n\x1a\n",
    b"",
])
def test_sniff_rejects_unknown(head):
    with pytest.raises(ValueError):
        media.sniff(head, media.AUDIO)


def test_b64decode_raw_and_data_uri():
    raw = b"fLaC\x00\x01"
    enc = base64.b64encode(raw).decode()
    assert media.b64decode(enc) == raw
    assert media.b64decode(f"data:audio/flac;base64,{enc}\n") == raw
