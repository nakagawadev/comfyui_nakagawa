import io
import base64
import av
import torch
import numpy as np
import comfy.utils
import time
import os
import uuid
import folder_paths
from fractions import Fraction
from PIL import Image, ImageOps
from comfy_api.util import VideoCodec, VideoContainer


def _temp_video_path(ext):
    """Path in ComfyUI's temp dir (wiped on startup) so the UI can preview it."""
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    filename = f"ws_preview_{uuid.uuid4().hex}.{ext}"
    return filename, os.path.join(temp_dir, filename)


def _ui_preview(filename):
    return {"ui": {"images": [{"filename": filename, "subfolder": "", "type": "temp"}], "animated": (True,)}}


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
        # Get actual format extension
        format_ext = VideoContainer.get_extension(format)

        # Save to ComfyUI's temp dir; the file is kept so the UI can preview it
        filename, temp_path = _temp_video_path(format_ext)
        video.save_to(
            temp_path,
            format=format,
            codec=codec
        )

        with open(temp_path, 'rb') as f:
            video_data = f.read()

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
            PromptServer.instance.send_sync(VIDEO_EVENT, message, sid=None)

        return _ui_preview(filename)

    @classmethod
    def IS_CHANGED(s, video, format, codec):
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
            PromptServer.instance.send_sync(VIDEO_EVENT, message, sid=None)

        # Write a copy to ComfyUI's temp dir so the UI can preview it
        filename, temp_path = _temp_video_path("webm")
        with open(temp_path, "wb") as f:
            f.write(video_data)

        return _ui_preview(filename)

    @classmethod
    def IS_CHANGED(s, images, codec, fps, crf):
        return time.time()


class LoadImageBase64:
    """
    Load an image from a base64 string (raw or data URI).

    The UI preview is rendered client-side from the textarea
    (web/load_image_base64.js), nothing is sent or written to disk.
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
        # Accept data URIs too: strip everything up to the comma
        img = Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[-1])))
        img = ImageOps.exif_transpose(img)

        rgb = img.convert("RGB")
        image = torch.from_numpy(np.array(rgb).astype(np.float32) / 255.0)[None,]

        # Same semantics as LoadImageFromBase64: mask = alpha, ones when there is no alpha
        if "A" in img.getbands():
            mask = torch.from_numpy(np.array(img.getchannel("A")).astype(np.float32) / 255.0)
        else:
            mask = torch.ones((rgb.height, rgb.width), dtype=torch.float32)

        return image, mask.unsqueeze(0)


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "SaveVideoWebsocket": SaveVideoWebsocket,
    "SaveWEBMWebsocket": SaveWEBMWebsocket,
    "LoadImageBase64Nak": LoadImageBase64
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveVideoWebsocket": "Save Video Websocket",
    "SaveWEBMWebsocket": "Save WEBM Websocket",
    "LoadImageBase64Nak": "Load Image (Base64)"
}
