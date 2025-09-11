import io
import av
import torch
import numpy as np
import comfy.utils
import time
from fractions import Fraction
from comfy_api.util import VideoCodec, VideoContainer


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
        import tempfile

        # Get actual format extension
        format_ext = VideoContainer.get_extension(format)

        # Create a temporary file with the correct extension
        with tempfile.NamedTemporaryFile(suffix=f".{format_ext}", delete=False) as temp_file:
            temp_path = temp_file.name

        try:
            # Save video to temporary file
            video.save_to(
                temp_path,
                format=format,
                codec=codec
            )

            # Read the file content
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

        finally:
            # Clean up temporary file
            import os
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return {}

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

        return {}

    @classmethod
    def IS_CHANGED(s, images, codec, fps, crf):
        return time.time()


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "SaveVideoWebsocket": SaveVideoWebsocket,
    "SaveWEBMWebsocket": SaveWEBMWebsocket
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveVideoWebsocket": "Save Video Websocket",
    "SaveWEBMWebsocket": "Save WEBM Websocket"
}
