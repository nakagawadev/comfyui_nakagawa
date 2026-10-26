# ComfyUI Nakagawa

A collection of custom nodes for ComfyUI aimed at API usage: send video data through websockets instead of saving to disk, and load images from base64.

## Installation

1. Clone this repository into your `ComfyUI/custom_nodes` directory
2. Restart ComfyUI
3. The nodes will appear in the `api/video` and `api/image` categories

## Nodes

### Save Video Websocket

Sends video data through websocket instead of saving to disk. Compatible with ComfyUI's native `SaveVideo` node.

**Inputs:**
- `video` (VIDEO) - The video to send through websocket
- `format` (COMBO) - Output video format (auto, mp4, webm, avi, mov, etc.)
- `codec` (COMBO) - Video codec to use (auto, libx264, libx265, libvpx-vp9, etc.)

**Features:**
- Uses ComfyUI's native video handling system
- Supports all formats and codecs available in your ComfyUI installation
- Streams video data through websocket with proper headers
- Progress tracking during processing
- Video preview in the node when the workflow runs from the ComfyUI UI (see [UI preview](#ui-preview))

### Save WEBM Websocket

Converts image sequences to WEBM format and sends through websocket. Compatible with ComfyUI's native `SaveWEBM` node.

**Inputs:**
- `images` (IMAGE) - Sequence of images to convert to WEBM
- `codec` (COMBO) - WEBM codec (vp9, av1)
- `fps` (FLOAT) - Frames per second (0.01-1000.0, default: 24.0)
- `crf` (FLOAT) - Quality setting (0-63, default: 32.0, lower = better quality)

**Features:**
- Specialized WEBM encoding with VP9 and AV1 support
- Optimized for web streaming
- Real-time progress updates
- AV1 preset optimization for faster encoding
- Video preview in the node when the workflow runs from the ComfyUI UI (see [UI preview](#ui-preview))

### Load Image (Base64)

Loads an image from a base64 string, so API clients can send images inline instead of uploading files. Node ID: `LoadImageBase64Nak`.

**Inputs:**
- `data` (STRING) - The image as base64, raw (`iVBORw0KGgo...`) or as a data URI (`data:image/png;base64,...`)

**Outputs:**
- `IMAGE` - The decoded image (RGB)
- `MASK` - The alpha channel, or all ones when the image has no alpha

**Features:**
- Live preview of the image below the textarea, client-side only (nothing is uploaded or saved)
- `load image as base64` button to pick an image file and fill the textarea with it
- Preview and button are not serialized, so an API export only contains `data`

## Generation stats

Optional Prometheus metrics and a per-generation JSONL log, meant for debugging (memory, model usage, failures). Off unless a config file exists.

### Config

ComfyUI looks for `comfyui_nakagawa.yaml` next to each `--extra-model-paths-config` file, then in the ComfyUI root. Instances that share an `extra_model_paths.yaml` share this file, so one file per server controls every instance. Changes apply without a restart.

```yaml
stats:                      # applies to every instance
  enabled: true
  dir: ~/comfy_stats        # {dir}/{port}/{YYYY-MM-DD}/events.jsonl and graphs/{prompt_id}.json
  retention_days: 14        # day folders older than this are deleted
  max_mb: 500               # cap per instance; oldest days go first, today is kept
  save_graph: errors        # off | errors | all
instances:                  # per-port overrides of any `stats` key
  "8189": { save_graph: all }
  "8190": { enabled: false }
```

Saved graphs are in API format, so they can be posted back to `/prompt` to reproduce a failure. Strings over 1 KB (e.g. base64 images) are replaced by their size and hash. Prompt text is only stored inside saved graphs.

### Endpoints

| Endpoint | Returns |
|---|---|
| `GET /nkg/metrics` | Prometheus metrics |
| `GET /nkg/stats/events?status=error&since=2026-01-01T10:00&until=...&limit=50` | Events, newest first |
| `GET /nkg/stats/events/{prompt_id}` | One event |
| `GET /nkg/stats/events/{prompt_id}/graph` | Its saved graph |

All return 404 while stats are disabled. Each event has the `prompt_id` that `/prompt` returns; send `"extra_data": {"trace_id": "..."}` with `/prompt` to also record your own ID.

### Metrics

`comfyui_prompts_total{status}` (success, error, interrupted, rejected), `comfyui_prompt_errors_total{exception}`, `comfyui_images_total` (from `batch_size`), `comfyui_prompt_duration_seconds`, `comfyui_queue_wait_seconds`, `comfyui_queue_pending`, `comfyui_model_uses_total{type,name}` (checkpoint, unet, vae, clip), `comfyui_model_loads_total{class}`, `comfyui_loaded_model_bytes{class}`, `comfyui_torch_vram_allocated_bytes`, `comfyui_torch_vram_reserved_bytes`, `comfyui_build_info{version}`, `process_resident_memory_bytes`, `process_start_time_seconds`.

Per-generation details (LoRAs and strengths, every model file in the graph, sampling params, slowest nodes, memory before and after, models loaded or evicted, error and traceback) go to the JSONL only, to keep metric cardinality low.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: comfyui
    static_configs:
      - targets: ["comfy-host:8188", "comfy-host:8189"]
    metrics_path: /nkg/metrics
```

Host RAM and full-GPU usage are out of scope here: run [node_exporter](https://github.com/prometheus/node_exporter) and an NVIDIA exporter such as [nvidia_gpu_exporter](https://github.com/utkuozdemir/nvidia_gpu_exporter) next to ComfyUI for those.

## Usage

The video nodes work exactly like their native ComfyUI counterparts (`SaveVideo` and `SaveWEBM`) but instead of saving files to disk, they send the video data through ComfyUI's websocket connection. This is useful for:

- Real-time video streaming applications
- Web-based ComfyUI integrations
- Custom clients that need direct video data access
- Reducing disk I/O in automated workflows

### UI preview

When a workflow runs from the ComfyUI UI, both video nodes also write the video to ComfyUI's `temp` directory (wiped on startup) and return it as a UI output, so the video plays inside the node. The websocket message is sent either way.

UI runs are detected through the `extra_pnginfo` hidden input: the ComfyUI frontend sends the workflow there, API clients usually don't. When it is missing (API runs), nothing is kept on disk and the node returns no UI output. An API client that does send `extra_pnginfo` with a `workflow` is treated as a UI run.

## Technical Details

### Video Transmission Protocol

Videos are sent as **binary data** through a custom WebSocket event (type 100) for maximum efficiency:

#### Binary Message Structure

**Note:** ComfyUI's WebSocket protocol adds a 4-byte message length header before the actual data.

```
[Message Length (4)] [Magic Bytes (4)] [Format Length (4)] [Format String (N)] [Video Data]
```

- **Message Length**: 32-bit big-endian integer - Total message size (added by ComfyUI)
- **Magic Bytes**: `VIDF` (0x56494446) - Identifies video data format
- **Format Length**: 32-bit little-endian integer - Length of format string
- **Format String**: UTF-8 encoded format (e.g., "mp4", "webm")
- **Video Data**: Raw video binary data

#### Client Implementation

To receive video data on the client side:

```javascript
// Listen for video data (event type 100)
ws.onmessage = (e) => {
  if (e.data instanceof ArrayBuffer) {
    const view = new DataView(e.data);
    
    // Skip ComfyUI's 4-byte message length header and check for "VIDF" magic bytes
    if (view.getUint32(4) === 0x46444956) {
      const fmtLen = view.getUint32(8, true);
      const format = new TextDecoder().decode(new Uint8Array(e.data, 12, fmtLen));
      const video = e.data.slice(12 + fmtLen);
      
      // Create video URL
      const url = URL.createObjectURL(new Blob([video], {type: `video/${format}`}));
      document.querySelector('video').src = url;
    }
  }
};
```