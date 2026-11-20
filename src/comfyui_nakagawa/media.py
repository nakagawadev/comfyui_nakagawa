"""Helpers for media coming from clients (base64 or URLs). No ComfyUI/torch imports, so CI can test it.

Untrusted bytes must never reach ffmpeg's format probing: a probed HLS playlist or
concat list makes ffmpeg open other URLs and local files. sniff() picks the demuxer
from the magic bytes; the caller passes it to av.open(format=...), or relies on the
magic bytes already ruling out the text-based playlist demuxers.

fetch() downloads a URL for the "Load ... (URL)" nodes without opening an SSRF hole:
https only (redirects too), hosts from url_loader.allowed_hosts, never a non-public IP, the
connection pinned to the IP that was checked (no DNS rebinding), every redirect
re-checked, and a size cap and deadline on the download.
"""
import base64
import binascii
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

URL_DEFAULTS = {
    "allowed_hosts": [],        # exact host or "*.example.com"; empty = see allow_any_public
    "allow_any_public": True,   # with no allowed_hosts: True = any public host, False = URL loading off
    "max_mb": {"image": 10, "audio": 15, "video": 50},  # per kind; set only the ones to change
    "timeout_s": 30,
}
MAX_REDIRECTS = 2
CHUNK = 1 << 16

IMAGE = "image"
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
    "webm": {AUDIO, VIDEO},      # matroska demuxer, but only files whose DocType is webm
}


def b64decode(data):
    """Raw base64 or a data URI (everything up to the comma is dropped)."""
    try:
        return base64.b64decode(data.split(",", 1)[-1].strip(), validate=False)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Invalid base64: {e}") from None


def sniff(raw, kind, formats=None):
    """ffmpeg demuxer for these bytes, or ValueError if it isn't an allowed `kind` container.

    formats: the configured list (limits()["formats"]); it can only narrow the known set,
    a name this module can't detect is ignored.
    """
    allowed = [f for f, k in _KINDS.items() if kind in k and (formats is None or f in formats)]
    fmt = _detect(raw[:64])
    if fmt not in allowed:
        raise ValueError(f"Unsupported {kind} format (allowed: {', '.join(allowed) or 'none'})")
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
        # EBML header: DocType element (0x4282), 4 bytes, "webm"; a .mkv says "matroska"
        return "webm" if b"\x42\x82\x84webm" in head else None
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:  # ADTS sync, layer 00
            return "aac"
        if head[1] & 0xE0 == 0xE0 and head[1] & 0x06:  # MPEG audio sync, layer != 00
            return "mp3"
    return None


# --- URL fetching -------------------------------------------------------------

def url_policy(raw):
    """url_loader settings from the resolved config dict (missing keys take URL_DEFAULTS)."""
    user = (raw or {}).get("url_loader") or {}
    return {**URL_DEFAULTS, **user, "max_mb": {**URL_DEFAULTS["max_mb"], **(user.get("max_mb") or {})}}


def host_allowed(host, policy):
    hosts = policy["allowed_hosts"] or []
    if not hosts:
        return bool(policy["allow_any_public"])
    host = host.lower().rstrip(".")
    for pattern in hosts:
        pattern = str(pattern).lower().rstrip(".")
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):  # "*.example.com" matches a.example.com, not example.com
                return True
        elif host == pattern:
            return True
    return False


def _is_public(ip):
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def _resolve(host, port):
    """One public IP for host, or ValueError if any address it resolves to isn't public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve {host}: {e}") from None
    ips = [info[4][0] for info in infos]
    if not ips or not all(_is_public(ip) for ip in ips):
        raise ValueError(f"{host} resolves to a non-public address")
    return ips[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        # SNI and certificate checked against the hostname, not the pinned IP
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url, policy, kind):
    """Bytes of the `kind` (IMAGE/AUDIO/VIDEO) file at url, or ValueError naming why it was refused."""
    max_mb = policy["max_mb"][kind]
    max_bytes = int(float(max_mb) * 1024 * 1024)
    too_big = f"File is larger than url_loader.max_mb.{kind} ({max_mb:g} MB)"
    timeout = float(policy["timeout_s"])
    deadline = time.monotonic() + timeout
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("Only https:// URLs are allowed")
        host = parts.hostname
        if not host_allowed(host, policy):
            if not policy["allowed_hosts"] and not policy["allow_any_public"]:
                raise ValueError("URL loading is off: set url_loader.allowed_hosts in comfyui_nakagawa.yaml")
            raise ValueError(f"Host {host} is not in url_loader.allowed_hosts")
        port = parts.port or 443
        ip = _resolve(host, port)
        conn = _PinnedHTTPSConnection(host, ip, port, timeout)
        try:
            path = parts.path or "/"
            conn.request("GET", f"{path}?{parts.query}" if parts.query else path,
                         headers={"User-Agent": "comfyui_nakagawa", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise ValueError(f"Redirect {resp.status} without Location")
                url = urljoin(url, location)
                continue
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status} from {host}")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ValueError(too_big)
            chunks, size = [], 0
            while chunk := resp.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(too_big)
                if time.monotonic() > deadline:
                    raise ValueError(f"Download took longer than url_loader.timeout_s ({timeout:g}s)")
                chunks.append(chunk)
            return b"".join(chunks)
        except (OSError, http.client.HTTPException) as e:
            raise ValueError(f"Download from {host} failed: {e}") from None
        finally:
            conn.close()
    raise ValueError(f"More than {MAX_REDIRECTS} redirects")


# --- per-kind limits ------------------------------------------------------------

# `image` / `audio` / `video` sections of comfyui_nakagawa.yaml; set only the keys to change.
# They apply per file, to the base64 and URL loaders alike. Formats are always restricted;
# duration, resolution and fps are product rules, so they are off (None) unless configured.
# Limits across all the files of a job belong in the API backend, which sees the whole job.
LIMITS = {
    IMAGE: {"formats": ["png", "jpeg", "webp", "gif", "bmp", "tiff"]},
    AUDIO: {"formats": ["wav", "flac", "mp3", "aac", "ogg", "mp4", "webm"], "min_s": None, "max_s": None},
    VIDEO: {"formats": ["mp4", "webm"], "min_s": None, "max_s": None,
            "max_long_side": None, "max_short_side": None,  # any orientation: 1080x1920 fits 1920/1080
            "max_fps": None},
}
# Pillow plugin names per configured image format (the JPEG plugin also opens MPO phone photos)
PIL_FORMATS = {"png": ["PNG"], "jpeg": ["JPEG"], "webp": ["WEBP"], "gif": ["GIF"], "bmp": ["BMP"], "tiff": ["TIFF"]}


def limits(raw, kind):
    """`kind` limits from the resolved config dict, over LIMITS."""
    return {**LIMITS[kind], **((raw or {}).get(kind) or {})}


def _check_seconds(kind, seconds, lim):
    lo, hi = lim["min_s"], lim["max_s"]
    if lo is not None and seconds < lo:
        raise ValueError(f"{kind.capitalize()} is {seconds:.1f}s; the minimum is {lo:g}s")
    if hi is not None and seconds > hi:
        raise ValueError(f"{kind.capitalize()} is {seconds:.1f}s; the maximum is {hi:g}s")


def check_audio(seconds, lim):
    _check_seconds(AUDIO, seconds, lim)


def check_video(width, height, fps, seconds, lim):
    long_max, short_max = lim["max_long_side"], lim["max_short_side"]
    if (long_max is not None and max(width, height) > long_max) or (short_max is not None and min(width, height) > short_max):
        raise ValueError(f"Video is {width}x{height}; the limit is {long_max or 'any'}x{short_max or 'any'} (either orientation)")
    if lim["max_fps"] is not None and fps > lim["max_fps"] + 0.01:  # 29.97 and friends
        raise ValueError(f"Video is {fps:.2f} fps; the limit is {lim['max_fps']:g}")
    _check_seconds(VIDEO, seconds, lim)
