import io
import functools
import av
import torch
import numpy as np
import comfy.utils
import time
import os
import uuid
import folder_paths
from fractions import Fraction
from PIL import Image, ImageOps, UnidentifiedImageError
from comfy_api.util import VideoCodec, VideoContainer
from comfy_api.latest import InputImpl
from . import media, stats


def _temp_video_path(ext):
    """Scratch file in ComfyUI's temp dir: save_to needs a real path for MP4 faststart."""
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    return os.path.join(temp_dir, f"ws_video_{uuid.uuid4().hex}.{ext}")


class SaveVideoWebsocket:
    """
    Send videos through websocket instead of saving to disk.
    Based on SaveImageWebsocket and SaveVideo nodes.

    The video will be sent as binary data through the websocket with
    an 8 byte header indicating the message type and format.
    """

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video": ("VIDEO", {"tooltip": "The video to send through websocket"}),
                "format": (VideoContainer.as_input(), {"default": "auto", "tooltip": "The format to save the video as"}),
                "codec": (VideoCodec.as_input(), {"default": "auto", "tooltip": "The codec to use for the video"})
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "save_video"
    OUTPUT_NODE = True
    CATEGORY = "api/video"

    def save_video(self, video, format, codec):
        # Resolve "auto" like the native SaveVideo: with an explicit container,
        # save_to re-encodes streams it can't copy (e.g. VP8 from a webm into mp4)
        if format == "auto":
            format = "webm" if codec == "av1" else "mp4"
        format_ext = VideoContainer.get_extension(format)

        # Encode through a scratch file, removed right away; the UI preview is
        # built client-side from the websocket bytes (web/video_websocket_preview.js)
        temp_path = _temp_video_path(format_ext)
        try:
            video.save_to(temp_path, format=format, codec=codec)
            with open(temp_path, 'rb') as f:
                video_data = f.read()
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # Send video as binary data through custom event
        from server import PromptServer
        import struct

        # Create header with format info (8 bytes: 4 for magic, 4 for format length)
        magic = b'VIDF'  # Video Data Format
        format_bytes = format_ext.encode('utf-8')
        header = magic + struct.pack('<I', len(format_bytes)) + format_bytes

        # Combine header and video data
        message = header + video_data

        # Send binary data directly through websocket
        if PromptServer.instance:
            # Custom event type for video data (using high number to avoid conflicts)
            VIDEO_EVENT = 100
            PromptServer.instance.send_sync(VIDEO_EVENT, message, sid=PromptServer.instance.client_id)

        return {}

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return time.time()


class SaveWEBMWebsocket:
    """
    Send WEBM videos through websocket instead of saving to disk.
    Based on SaveWEBM node - converts image sequences to WEBM format.
    """

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "Sequence of images to convert to WEBM"}),
                "codec": (["vp9", "av1"], {"default": "vp9", "tooltip": "WEBM codec to use"}),
                "fps": ("FLOAT", {
                    "default": 24.0,
                    "min": 0.01,
                    "max": 1000.0,
                    "step": 0.01,
                    "tooltip": "Frames per second"
                }),
                "crf": ("FLOAT", {
                    "default": 32.0,
                    "min": 0,
                    "max": 63.0,
                    "step": 1,
                    "tooltip": "Higher crf means lower quality with smaller file size"
                })
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "save_webm"
    OUTPUT_NODE = True
    CATEGORY = "api/video"

    def save_webm(self, images, codec, fps, crf):
        if images.shape[0] == 0:
            return {}

        # Create in-memory buffer for video
        buffer = io.BytesIO()
        container = av.open(buffer, mode="w", format="webm")

        # Setup codec mapping
        codec_map = {"vp9": "libvpx-vp9", "av1": "libsvtav1"}

        # Create stream
        stream = container.add_stream(codec_map[codec], rate=Fraction(round(fps * 1000), 1000))
        stream.width = images.shape[-2]
        stream.height = images.shape[-3]
        stream.pix_fmt = "yuv420p10le" if codec == "av1" else "yuv420p"
        stream.bit_rate = 0
        stream.options = {'crf': str(int(crf))}

        # AV1 specific settings
        if codec == "av1":
            stream.options["preset"] = "6"

        # Progress bar
        pbar = comfy.utils.ProgressBar(images.shape[0])

        # Encode frames
        for i, frame in enumerate(images):
            frame_data = torch.clamp(frame[..., :3] * 255, min=0, max=255).to(device=torch.device("cpu"), dtype=torch.uint8).numpy()
            av_frame = av.VideoFrame.from_ndarray(frame_data, format="rgb24")

            for packet in stream.encode(av_frame):
                container.mux(packet)

            # Update progress (skip sending partial data to avoid errors)
            pbar.update_absolute(i + 1, images.shape[0])

        # Flush encoder
        for packet in stream.encode():
            container.mux(packet)

        container.close()

        # Send video as binary data through custom event
        video_data = buffer.getvalue()

        from server import PromptServer
        import struct

        # Create header with format info
        magic = b'VIDF'  # Video Data Format
        format_bytes = b'webm'
        header = magic + struct.pack('<I', len(format_bytes)) + format_bytes

        # Combine header and video data
        message = header + video_data

        # Send binary data directly through websocket
        if PromptServer.instance:
            VIDEO_EVENT = 100
            PromptServer.instance.send_sync(VIDEO_EVENT, message, sid=PromptServer.instance.client_id)

        return {}

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return time.time()


def _limits(kind):
    """Per-file limits for `kind` from comfyui_nakagawa.yaml (media.LIMITS by default)."""
    return media.limits(_config().get(), kind)


def _decode_image(raw):
    """IMAGE and MASK like ComfyUI's LoadImage (first frame, EXIF rotation, alpha as mask).

    Pillow only gets the configured formats: the rarer decoders are the riskier ones.
    """
    formats = _limits(media.IMAGE)["formats"]
    Image.init()  # registers every plugin; open(formats=...) only knows PNG/JPEG/GIF/BMP/PPM before it
    try:
        img = Image.open(io.BytesIO(raw), formats=[n for f in formats for n in media.PIL_FORMATS.get(f, [])])
    except UnidentifiedImageError:
        raise ValueError(f"Unsupported image format (allowed: {', '.join(formats)})") from None
    img = ImageOps.exif_transpose(img)

    rgb = img.convert("RGB")
    image = torch.from_numpy(np.array(rgb).astype(np.float32) / 255.0)[None,]

    # mask = alpha, ones when there is no alpha
    if "A" in img.getbands():
        mask = torch.from_numpy(np.array(img.getchannel("A")).astype(np.float32) / 255.0)
    else:
        mask = torch.ones((rgb.height, rgb.width), dtype=torch.float32)

    return image, mask.unsqueeze(0)


class LoadImageBase64:
    """
    Load an image from a base64 string (raw or data URI).

    The UI preview is rendered client-side from the textarea
    (web/load_base64.js), nothing is sent or written to disk.
    """

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "data": ("STRING", {"default": "", "multiline": True, "tooltip": "Base64 image, raw or as a data:image/...;base64, URI"}),
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load_image"
    CATEGORY = "api/image"

    def load_image(self, data):
        return _decode_image(media.b64decode(data))


def _decode_audio(raw):
    """Like ComfyUI's LoadAudio, but from bytes and with the demuxer forced (see media.py)."""
    with av.open(io.BytesIO(raw), format=media.sniff(raw, media.AUDIO, _limits(media.AUDIO)["formats"])) as container:
        if not container.streams.audio:
            raise ValueError("No audio stream found")
        stream = container.streams.audio[0]
        # float32 planar keeps the native [channels, samples] shape at any input sample format
        resampler = av.AudioResampler(format="fltp")
        chunks = [f.to_ndarray() for frame in container.decode(stream) for f in resampler.resample(frame)]
        if not chunks:
            raise ValueError("No audio frames decoded")
        return torch.from_numpy(np.concatenate(chunks, axis=1)), stream.codec_context.sample_rate


def _audio_output(waveform, sample_rate):
    """AUDIO dict, after checking the per-file duration limit (media.py)."""
    media.check_audio(waveform.shape[-1] / sample_rate, _limits(media.AUDIO))
    return ({"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate},)


class LoadAudioBase64:
    """
    Load 2-15s of audio from a base64 string (raw or data URI): wav, flac, mp3, aac, ogg, m4a/mp4, webm.

    The UI preview is rendered client-side from the textarea (web/load_base64.js).
    """

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "data": ("STRING", {"default": "", "multiline": True, "tooltip": "Base64 audio, raw or as a data:audio/...;base64, URI"}),
            }
        }

    RETURN_TYPES = ("AUDIO",)
    FUNCTION = "load_audio"
    CATEGORY = "api/audio"

    def load_audio(self, data):
        return _audio_output(*_decode_audio(media.b64decode(data)))


@functools.cache
def _config():
    from comfy.cli_args import args
    return stats.Config(stats.find_config(args.extra_model_paths_config, folder_paths.base_path), args.port)


def _fetch(url, kind):
    """Download url under the url_loader policy in comfyui_nakagawa.yaml (re-read when it changes)."""
    return media.fetch(url, media.url_policy(_config().get()), kind)


_URL_INPUT = {"required": {"url": ("STRING", {"default": "", "tooltip": "https URL; the host must be in url_loader.allowed_hosts (comfyui_nakagawa.yaml)"})}}


class LoadImageURL:
    """Load an image from a URL. Same outputs as Load Image."""

    @classmethod
    def INPUT_TYPES(s):
        return _URL_INPUT

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load"
    CATEGORY = "api/image"

    def load(self, url):
        return _decode_image(_fetch(url, media.IMAGE))


class LoadVideoURL:
    """Load a video (mp4/mov/m4v, webm) from a URL. Same output as Load Video."""

    @classmethod
    def INPUT_TYPES(s):
        return _URL_INPUT

    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "load"
    CATEGORY = "api/video"

    def load(self, url):
        raw = _fetch(url, media.VIDEO)
        lim = _limits(media.VIDEO)
        # Checking the magic bytes is enough here: VideoFromFile lets ffmpeg probe,
        # and a file starting with an mp4/webm header can't be probed as a playlist
        media.sniff(raw, media.VIDEO, lim["formats"])
        video = InputImpl.VideoFromFile(io.BytesIO(raw))
        # From container metadata, before any frame is decoded into RAM
        media.check_video(*video.get_dimensions(), float(video.get_frame_rate()), video.get_duration(), lim)
        return (video,)


class LoadAudioURL:
    """Load audio from a URL. Same output as Load Audio."""

    @classmethod
    def INPUT_TYPES(s):
        return _URL_INPUT

    RETURN_TYPES = ("AUDIO",)
    FUNCTION = "load"
    CATEGORY = "api/audio"

    def load(self, url):
        return _audio_output(*_decode_audio(_fetch(url, media.AUDIO)))


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "SaveVideoWebsocket": SaveVideoWebsocket,
    "SaveWEBMWebsocket": SaveWEBMWebsocket,
    "LoadImageBase64Nak": LoadImageBase64,
    "LoadAudioBase64Nak": LoadAudioBase64,
    "LoadImageURLNak": LoadImageURL,
    "LoadVideoURLNak": LoadVideoURL,
    "LoadAudioURLNak": LoadAudioURL,
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveVideoWebsocket": "Save Video Websocket",
    "SaveWEBMWebsocket": "Save WEBM Websocket",
    "LoadImageBase64Nak": "Load Image (Base64)",
    "LoadAudioBase64Nak": "Load Audio (Base64)",
    "LoadImageURLNak": "Load Image (URL)",
    "LoadVideoURLNak": "Load Video (URL)",
    "LoadAudioURLNak": "Load Audio (URL)",
}
