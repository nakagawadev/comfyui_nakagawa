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
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", "webm"),
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
    b"\x1a\x45\xdf\xa3\xa3\x42\x82\x88matroska",  # .mkv
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


# --- fetch ----------------------------------------------------------------------

import http.client
import http.server
import threading

OPEN = {**media.URL_DEFAULTS, "allowed_hosts": ["127.0.0.1"]}


def test_url_policy_defaults_and_override():
    assert media.url_policy({"enabled": False}) == media.URL_DEFAULTS
    policy = media.url_policy({"url_loader": {"max_mb": {"video": 50}, "timeout_s": 60}})
    assert policy["max_mb"] == {"image": 10, "audio": 15, "video": 50}
    assert policy["timeout_s"] == 60


def test_host_allowed():
    policy = {**media.URL_DEFAULTS, "allowed_hosts": ["cdn.example.com", "*.r2.dev"]}
    assert media.host_allowed("CDN.example.com.", policy)
    assert media.host_allowed("bucket.r2.dev", policy)
    assert not media.host_allowed("r2.dev", policy)
    assert not media.host_allowed("evilr2.dev", policy)
    assert not media.host_allowed("cdn.example.com.evil.io", policy)
    assert media.host_allowed("anything.com", media.URL_DEFAULTS)  # any public host by default
    assert not media.host_allowed("anything.com", {**media.URL_DEFAULTS, "allow_any_public": False})


@pytest.mark.parametrize("url,msg", [
    ("file:///etc/passwd", "Only https"),
    ("ftp://cdn.example.com/a.png", "Only https"),
    ("http://cdn.example.com/a.png", "Only https"),
])
def test_fetch_refuses_before_connecting(url, msg):
    with pytest.raises(ValueError, match=msg):
        media.fetch(url, media.URL_DEFAULTS, media.AUDIO)


def test_fetch_off_when_any_public_disabled():
    with pytest.raises(ValueError, match="URL loading is off"):
        media.fetch("https://cdn.example.com/a.png", {**media.URL_DEFAULTS, "allow_any_public": False}, media.AUDIO)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "100.64.0.1", "[::1]", "[::ffff:127.0.0.1]"])
def test_fetch_refuses_non_public_ips(ip):
    with pytest.raises(ValueError, match="non-public"):
        media.fetch(f"https://{ip}/x", {**media.URL_DEFAULTS, "allow_any_public": True}, media.AUDIO)


@pytest.fixture
def server(monkeypatch):
    """Local plain-HTTP server behind https:// URLs (the pinned connection is swapped for a
    plain one, TLS itself is covered by http.client); 127.0.0.1 counts as public here."""
    real = media._is_public
    monkeypatch.setattr(media, "_is_public", lambda ip: ip == "127.0.0.1" or real(ip))

    class Plain(http.client.HTTPConnection):
        def __init__(self, host, ip, port, timeout):
            super().__init__(ip, port, timeout=timeout)

    monkeypatch.setattr(media, "_PinnedHTTPSConnection", Plain)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect-local":
                self.send_response(302)
                self.send_header("Location", "/file?x=1")
                self.end_headers()
            elif self.path == "/redirect-metadata":
                self.send_response(302)
                self.send_header("Location", "https://169.254.169.254/latest/meta-data/")
                self.end_headers()
            elif self.path == "/redirect-http":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/file?x=1")
                self.end_headers()
            elif self.path in ("/hop2", "/hop3"):
                self.send_response(302)
                self.send_header("Location", "/redirect-local" if self.path == "/hop2" else "/hop2")
                self.end_headers()
            elif self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
            elif self.path == "/file?x=1":
                body = b"fLaC" + b"\0" * 2048
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/chunked-big":
                # no Content-Length: the cap has to trip while reading
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"\0" * (2 * 1024 * 1024))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_fetch_follows_safe_redirect(server):
    assert media.fetch(f"{server}/redirect-local", OPEN, media.AUDIO)[:4] == b"fLaC"
    assert media.fetch(f"{server}/hop2", OPEN, media.AUDIO)[:4] == b"fLaC"  # 2 redirects: the max


def test_fetch_rechecks_redirect_target(server):
    with pytest.raises(ValueError, match="not in url_loader.allowed_hosts"):
        media.fetch(f"{server}/redirect-metadata", OPEN, media.AUDIO)
    with pytest.raises(ValueError, match="non-public"):
        media.fetch(f"{server}/redirect-metadata", {**OPEN, "allowed_hosts": [], "allow_any_public": True}, media.AUDIO)


def test_fetch_refuses_redirect_to_http(server):
    with pytest.raises(ValueError, match="Only https"):
        media.fetch(f"{server}/redirect-http", OPEN, media.AUDIO)


def test_fetch_limits(server):
    with pytest.raises(ValueError, match="More than 2 redirects"):
        media.fetch(f"{server}/hop3", OPEN, media.AUDIO)
    with pytest.raises(ValueError, match="redirects"):
        media.fetch(f"{server}/loop", OPEN, media.AUDIO)
    with pytest.raises(ValueError, match="max_mb.audio"):
        media.fetch(f"{server}/file?x=1", {**OPEN, "max_mb": {"audio": 0.001}}, media.AUDIO)
    # each kind has its own cap
    assert media.fetch(f"{server}/file?x=1", {**OPEN, "max_mb": {"audio": 0.001, "video": 1}}, media.VIDEO)
    with pytest.raises(ValueError, match="max_mb.audio"):
        media.fetch(f"{server}/chunked-big", {**OPEN, "max_mb": {"audio": 1}}, media.AUDIO)
    with pytest.raises(ValueError, match="HTTP 404"):
        media.fetch(f"{server}/missing", OPEN, media.AUDIO)


# the values a server would set in its comfyui_nakagawa.yaml
SERVER = {"audio": {"min_s": 2, "max_s": 15},
          "video": {"min_s": 2, "max_s": 15, "max_long_side": 1920, "max_short_side": 1080, "max_fps": 30}}


def test_limits_override_one_key():
    lim = media.limits({"audio": {"max_s": 30}}, media.AUDIO)
    assert lim["max_s"] == 30 and lim["min_s"] is None
    assert media.limits({"enabled": False}, media.VIDEO) == media.LIMITS[media.VIDEO]


def test_no_product_limits_by_default():
    media.check_audio(600, media.limits({}, media.AUDIO))
    media.check_video(3840, 2160, 60, 600, media.limits({}, media.VIDEO))


def test_check_audio():
    lim = media.limits(SERVER, media.AUDIO)
    for ok in (2, 9.5, 15):
        media.check_audio(ok, lim)
    with pytest.raises(ValueError, match="minimum is 2s"):
        media.check_audio(1.9, lim)
    with pytest.raises(ValueError, match="maximum is 15s"):
        media.check_audio(15.1, lim)


def test_check_video():
    lim = media.limits(SERVER, media.VIDEO)
    media.check_video(1920, 1080, 30, 15, lim)
    media.check_video(1080, 1920, 29.97, 2, lim)  # vertical
    with pytest.raises(ValueError, match="1920x1080"):
        media.check_video(2560, 1440, 30, 5, lim)
    with pytest.raises(ValueError, match="1920x1080"):
        media.check_video(1200, 1200, 30, 5, lim)  # short side over 1080
    with pytest.raises(ValueError, match="fps"):
        media.check_video(1280, 720, 60, 5, lim)
    with pytest.raises(ValueError, match="maximum is 15s"):
        media.check_video(1280, 720, 30, 16, lim)


def test_sniff_formats_only_narrow():
    webm = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm"
    with pytest.raises(ValueError, match="allowed: mp4"):
        media.sniff(webm, media.VIDEO, ["mp4"])
    # a name the module can't detect (mkv) doesn't widen anything
    with pytest.raises(ValueError):
        media.sniff(b"\x1a\x45\xdf\xa3\xa3\x42\x82\x88matroska", media.VIDEO, ["mp4", "webm", "mkv", "matroska"])


def test_pil_formats_exist():
    """Every name passed to Image.open(formats=...) must be a registered plugin (MPO isn't)."""
    Image = pytest.importorskip("PIL.Image")
    Image.init()
    for names in media.PIL_FORMATS.values():
        for name in names:
            assert name in Image.OPEN, name
