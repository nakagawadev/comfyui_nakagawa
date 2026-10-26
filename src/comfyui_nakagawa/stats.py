"""Generation stats: Prometheus /metrics plus a JSONL event log per instance.

Configured by comfyui_nakagawa.yaml, looked up next to --extra-model-paths-config,
then in the ComfyUI root. No file means stats are off.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

log = logging.getLogger("comfyui_nakagawa.stats")

CONFIG_NAME = "comfyui_nakagawa.yaml"
DEFAULTS = {
    "enabled": True,
    "dir": "~/comfy_stats",
    "retention_days": 14,
    "max_mb": 500,
    "save_graph": "errors",  # off | errors | all
}
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft")
METRIC_KINDS = ("checkpoint", "unet", "vae", "clip")
PARAM_KEYS = ("batch_size", "width", "height", "steps", "sampler_name", "scheduler", "cfg", "denoise")
STATUSES = ("success", "error", "interrupted", "rejected")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TRUNCATE_AT = 1024
ROTATE_EVERY = 100  # events between max_mb checks
DURATION_BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120, 300, 600)
WAIT_BUCKETS = (0.1, 0.5, 1, 5, 10, 30, 60, 300, 900)


# --- config -----------------------------------------------------------------

def find_config(extra_model_paths_config, base_path):
    """First existing config next to an --extra-model-paths-config file, else in the ComfyUI root.

    extra_model_paths_config is ComfyUI's list of lists (nargs='+', action='append').
    When none exists, returns the preferred location so creating it later enables stats.
    """
    candidates = [os.path.join(os.path.dirname(os.path.abspath(path)), CONFIG_NAME)
                  for group in extra_model_paths_config or [] for path in group]
    candidates.append(os.path.join(base_path, CONFIG_NAME))
    return next((c for c in candidates if os.path.isfile(c)), candidates[0])


def resolve_config(raw, port):
    """Global `stats` (and top-level `drain`) merged with `instances[port]`; the instance wins."""
    raw = raw or {}
    cfg = {**DEFAULTS, **(raw.get("stats") or {}), "drain": bool(raw.get("drain", False))}
    cfg.update((raw.get("instances") or {}).get(str(port)) or {})
    if cfg["save_graph"] is False:  # YAML 1.1 reads a bare `off` as false
        cfg["save_graph"] = "off"
    return cfg


class Config:
    """Re-reads the YAML when its mtime changes, so toggling needs no restart."""

    def __init__(self, path, port):
        self.path, self.port = path, port
        self._mtime = None
        self._cfg = {"enabled": False}

    def get(self):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            self._mtime, self._cfg = None, {"enabled": False}
            return self._cfg
        if mtime != self._mtime:
            import yaml
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._cfg = resolve_config(yaml.safe_load(f), self.port)
            except Exception:
                log.warning("stats: could not read %s, stats disabled", self.path, exc_info=True)
                self._cfg = {"enabled": False}
            self._mtime = mtime
        return self._cfg


# --- graph inspection --------------------------------------------------------

def _kind(input_name, class_type):
    name = f"{input_name} {class_type}".lower()
    for kind in ("lora", "ckpt", "unet", "vae", "clip"):
        if kind in name:
            return "checkpoint" if kind == "ckpt" else kind
    return "other"


def _scalar(value):
    return isinstance(value, (str, int, float, bool))


def extract(prompt):
    """Models (with LoRA strengths) and sampling params from an API-format prompt."""
    models, params = [], {}
    for node_id, node in prompt.items():
        class_type = node.get("class_type", "")
        inputs = node.get("inputs") or {}
        for key, value in inputs.items():
            if isinstance(value, str) and value.lower().endswith(MODEL_EXT):
                entry = {"node_id": node_id, "class_type": class_type, "input": key,
                         "kind": _kind(key, class_type), "value": value}
                if entry["kind"] == "lora":
                    # lora_name -> strength_model; lora_name_1 -> model_weight_1, switch_1 (CR LoRA Stack)
                    suffix = key[len(key.rstrip("0123456789")):]
                    suffix = "_" + suffix if suffix and key.endswith("_" + suffix) else ""
                    for extra in ("strength_model", "strength_clip", "model_weight", "clip_weight", "switch"):
                        if _scalar(inputs.get(extra + suffix)):
                            entry[extra] = inputs[extra + suffix]
                models.append(entry)
        for key in PARAM_KEYS:
            value = inputs.get(key)
            if _scalar(value) and value not in params.setdefault(key, []):
                params[key].append(value)
    params = {k: v[0] if len(v) == 1 else v for k, v in params.items() if v}
    return models, params


def images_requested(params):
    # ponytail: largest batch_size in the graph; per-output counting needs output tensors
    batch = params.get("batch_size", 1)
    return max(batch) if isinstance(batch, list) else batch


def truncate(obj, limit=TRUNCATE_AT):
    """Replace long strings (base64 images) with their size and hash."""
    if isinstance(obj, str) and len(obj) > limit:
        digest = hashlib.sha256(obj.encode("utf-8", "replace")).hexdigest()[:16]
        return f"<truncated {len(obj)} bytes sha256:{digest}>"
    if isinstance(obj, dict):
        return {k: truncate(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [truncate(v, limit) for v in obj]
    return obj


# --- storage -----------------------------------------------------------------

def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def rotate(inst_dir, retention_days, max_bytes, today):
    """Drop day folders past retention, then oldest days while over max_bytes. Never today."""
    if not os.path.isdir(inst_dir):
        return
    days = sorted(d for d in os.listdir(inst_dir) if DAY_RE.fullmatch(d) and d != today)
    cutoff = (date.fromisoformat(today) - timedelta(days=retention_days)).isoformat()
    for day in [d for d in days if d <= cutoff]:
        shutil.rmtree(os.path.join(inst_dir, day), ignore_errors=True)
        days.remove(day)
    sizes = {d: _dir_size(os.path.join(inst_dir, d)) for d in days}
    total = sum(sizes.values()) + _dir_size(os.path.join(inst_dir, today))
    for day in days:
        if total <= max_bytes:
            break
        shutil.rmtree(os.path.join(inst_dir, day), ignore_errors=True)
        total -= sizes[day]


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self._last_day = None
        self._writes = 0

    @staticmethod
    def inst_dir(cfg, port):
        return os.path.join(os.path.expanduser(cfg["dir"]), str(port))

    def write(self, cfg, port, event, graph=None):
        inst_dir = self.inst_dir(cfg, port)
        today = date.today().isoformat()
        day_dir = os.path.join(inst_dir, today)
        with self.lock:
            if graph is not None:
                os.makedirs(os.path.join(day_dir, "graphs"), exist_ok=True)
                with open(os.path.join(day_dir, "graphs", event["prompt_id"] + ".json"), "w", encoding="utf-8") as f:
                    json.dump(truncate(graph), f)
                event["graph"] = "graphs/" + event["prompt_id"] + ".json"
            os.makedirs(day_dir, exist_ok=True)
            with open(os.path.join(day_dir, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
            self._writes += 1
            if today != self._last_day or self._writes % ROTATE_EVERY == 0:
                self._last_day = today
                rotate(inst_dir, int(cfg["retention_days"]), float(cfg["max_mb"]) * 1024 * 1024, today)

    def days(self, cfg, port):
        inst_dir = self.inst_dir(cfg, port)
        if not os.path.isdir(inst_dir):
            return inst_dir, []
        return inst_dir, sorted((d for d in os.listdir(inst_dir) if DAY_RE.fullmatch(d)), reverse=True)

    def query(self, cfg, port, status=None, since=None, until=None, limit=50):
        """Newest first. since/until are epoch seconds."""
        inst_dir, days = self.days(cfg, port)
        out = []
        for day in days:
            if since is not None and day < date.fromtimestamp(since).isoformat():
                break
            if until is not None and day > date.fromtimestamp(until).isoformat():
                continue
            try:
                with open(os.path.join(inst_dir, day, "events.jsonl"), encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                ts = event.get("ts", 0)
                if (status and event.get("status") != status) or (since and ts < since) or (until and ts > until):
                    continue
                out.append(event)
                if len(out) >= limit:
                    return out
        return out

    def find(self, cfg, port, prompt_id):
        inst_dir, days = self.days(cfg, port)
        for day in days:
            try:
                with open(os.path.join(inst_dir, day, "events.jsonl"), encoding="utf-8") as f:
                    for line in f:
                        if prompt_id in line:
                            return day, json.loads(line)
            except (OSError, ValueError):
                continue
        return None, None


# --- metrics -----------------------------------------------------------------

def _num(value):
    return str(value) if isinstance(value, int) else repr(float(value))


def _labels(labels):
    if not labels:
        return ""
    esc = (str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") for _, v in labels)
    return "{" + ",".join(f'{k}="{v}"' for (k, _), v in zip(labels, esc)) + "}"


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counters = {}  # name -> {labels tuple: value}
        self.hists = {}  # name -> [buckets, counts, sum, count]
        for status in STATUSES:
            self.inc("comfyui_prompts_total", (("status", status),), 0)
        self.inc("comfyui_images_total", (), 0)
        self.hists["comfyui_prompt_duration_seconds"] = [DURATION_BUCKETS, [0] * len(DURATION_BUCKETS), 0.0, 0]
        self.hists["comfyui_queue_wait_seconds"] = [WAIT_BUCKETS, [0] * len(WAIT_BUCKETS), 0.0, 0]

    def inc(self, name, labels=(), value=1):
        with self.lock:
            series = self.counters.setdefault(name, {})
            series[labels] = series.get(labels, 0) + value

    def observe(self, name, value):
        with self.lock:
            hist = self.hists[name]
            for i, bound in enumerate(hist[0]):
                if value <= bound:
                    hist[1][i] += 1
            hist[2] += value
            hist[3] += 1

    def render(self, gauges=()):
        """gauges: iterable of (name, labels tuple, value)."""
        lines = []
        with self.lock:
            for name, series in sorted(self.counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines += [f"{name}{_labels(labels)} {_num(value)}" for labels, value in series.items()]
            for name, (buckets, counts, total, count) in sorted(self.hists.items()):
                lines.append(f"# TYPE {name} histogram")
                lines += [f'{name}_bucket{{le="{b:g}"}} {c}' for b, c in zip(buckets, counts)]
                lines += [f'{name}_bucket{{le="+Inf"}} {count}', f"{name}_sum {_num(total)}", f"{name}_count {count}"]
        typed = set()
        for name, labels, value in gauges:
            if name not in typed:
                typed.add(name)
                lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{_labels(labels)} {_num(value)}")
        return "\n".join(lines) + "\n"


# --- runtime (needs ComfyUI) -------------------------------------------------

def _loaded_models():
    """Counter of loaded model classes and bytes per class."""
    import comfy.model_management as mm
    classes, sizes = Counter(), Counter()
    for lm in list(mm.current_loaded_models):
        try:
            patcher = lm.model
            if patcher is None:
                continue
            cls = type(patcher.model).__name__
            classes[cls] += 1
            sizes[cls] += lm.model_memory()
        except Exception:
            continue
    return classes, sizes


def _memory():
    import psutil
    snap = {"rss": psutil.Process().memory_info().rss}
    try:
        import torch
        if torch.cuda.is_available():
            snap["vram_allocated"] = torch.cuda.memory_allocated()
            snap["vram_reserved"] = torch.cuda.memory_reserved()
    except Exception:
        pass
    snap["models"] = dict(_loaded_models()[0])
    return snap


class Collector:
    def __init__(self, config, port, version):
        self.config, self.port, self.version = config, port, version
        self.metrics, self.store = Metrics(), Store()
        self.running = {}  # prompt_id -> {"before": snapshot, "nodes": {node_id: seconds}}

    def node_start(self, prompt_id):
        if prompt_id not in self.running:
            self.running.clear()  # one prompt at a time per instance
            self.running[prompt_id] = {"before": _memory(), "nodes": {}}

    def node_done(self, prompt_id, node_id, seconds):
        state = self.running.get(prompt_id)
        if state is not None:
            state["nodes"][node_id] = state["nodes"].get(node_id, 0) + seconds

    def on_done(self, cfg, item, status):
        _, prompt_id, prompt, extra_data = item[:4]
        events = dict(status.messages or [])
        state = self.running.pop(prompt_id, {"before": None, "nodes": {}})
        after = _memory()

        if "execution_interrupted" in events:
            result = "interrupted"
        elif "execution_error" in events or status.status_str != "success":
            result = "error"
        else:
            result = "success"
        now_ms = int(time.time() * 1000)
        start = events.get("execution_start", {}).get("timestamp")
        end = next((events[e]["timestamp"] for e in ("execution_success", "execution_error", "execution_interrupted")
                    if e in events), now_ms)
        created = extra_data.get("create_time")
        models, params = extract(prompt)
        slowest = sorted(state["nodes"].items(), key=lambda kv: kv[1], reverse=True)[:5]
        before = state["before"]

        event = {
            "ts": end / 1000,
            "prompt_id": prompt_id,
            "trace_id": extra_data.get("trace_id"),
            "client_id": extra_data.get("client_id"),
            "port": self.port,
            "comfyui_version": self.version,
            "status": result,
            "queued_at": created / 1000 if created else None,
            "started_at": start / 1000 if start else None,
            "duration_s": round((end - start) / 1000, 3) if start else None,
            "queue_wait_s": round((start - created) / 1000, 3) if start and created else None,
            "slowest_nodes": [{"node_id": n, "class_type": prompt.get(n, {}).get("class_type"), "seconds": round(s, 3)}
                              for n, s in slowest],
            "cached_nodes": events.get("execution_cached", {}).get("nodes", []),
            "models": models,
            "params": params,
            "images": images_requested(params),
            "memory_before": before,
            "memory_after": after,
            "models_loaded": sorted((Counter(after["models"]) - Counter((before or {}).get("models", {}))).elements()),
            "models_evicted": sorted((Counter((before or {}).get("models", {})) - Counter(after["models"])).elements()),
            "graph": None,
        }
        error = events.get("execution_error") or events.get("execution_interrupted")
        if error:
            event["error"] = {k: error.get(k) for k in ("node_id", "node_type", "exception_type", "exception_message", "traceback")}

        m = self.metrics
        m.inc("comfyui_prompts_total", (("status", result),))
        if result == "error":
            m.inc("comfyui_prompt_errors_total", (("exception", event["error"].get("exception_type") or "unknown"),))
        if result == "success":
            m.inc("comfyui_images_total", (), event["images"])
        if event["duration_s"] is not None:
            m.observe("comfyui_prompt_duration_seconds", event["duration_s"])
        if event["queue_wait_s"] is not None:
            m.observe("comfyui_queue_wait_seconds", max(event["queue_wait_s"], 0))
        for model in models:
            if model["kind"] in METRIC_KINDS:
                m.inc("comfyui_model_uses_total", (("type", model["kind"]), ("name", model["value"])))
        for cls in event["models_loaded"]:
            m.inc("comfyui_model_loads_total", (("class", cls),))

        save = cfg["save_graph"] == "all" or (cfg["save_graph"] == "errors" and result != "success")
        self.store.write(cfg, self.port, event, prompt if save else None)

    def on_rejected(self, cfg, prompt_id, prompt, result, trace_id=None):
        models, params = extract(prompt)
        event = {
            "ts": time.time(),
            "prompt_id": prompt_id,
            "trace_id": trace_id,
            "port": self.port,
            "comfyui_version": self.version,
            "status": "rejected",
            "models": models,
            "params": params,
            "error": result[1],
            "node_errors": truncate(result[3]),
            "graph": None,
        }
        self.metrics.inc("comfyui_prompts_total", (("status", "rejected"),))
        save = cfg["save_graph"] in ("errors", "all")
        self.store.write(cfg, self.port, event, prompt if save else None)

    def gauges(self, queue):
        import psutil
        proc = psutil.Process()
        out = [
            ("comfyui_build_info", (("version", self.version),), 1),
            ("comfyui_queue_pending", (), queue.get_tasks_remaining()),
            ("process_resident_memory_bytes", (), proc.memory_info().rss),
            ("process_start_time_seconds", (), proc.create_time()),
        ]
        _, sizes = _loaded_models()
        out += [("comfyui_loaded_model_bytes", (("class", cls),), size) for cls, size in sorted(sizes.items())]
        try:
            import torch
            if torch.cuda.is_available():
                out.append(("comfyui_torch_vram_allocated_bytes", (), torch.cuda.memory_allocated()))
                out.append(("comfyui_torch_vram_reserved_bytes", (), torch.cuda.memory_reserved()))
        except Exception:
            pass
        return out


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception:
        log.warning("stats: %s failed", getattr(fn, "__name__", fn), exc_info=True)


def install():
    import execution
    import folder_paths
    from aiohttp import web
    from comfy.cli_args import args
    from server import PromptServer

    try:
        from comfyui_version import __version__ as version
    except ImportError:
        version = "unknown"

    path = find_config(args.extra_model_paths_config, folder_paths.base_path)
    config = Config(path, args.port)
    collector = Collector(config, args.port, version)
    server = PromptServer.instance
    queue = server.prompt_queue
    log.info("stats: config %s (%s)", path, "found" if os.path.isfile(path) else "missing, stats disabled")

    from . import drain as drain_mode
    drain = drain_mode.install(server, config)

    def enabled():
        cfg = config.get()
        return cfg if cfg.get("enabled") else None

    orig_task_done = queue.task_done

    def task_done(item_id, history_result, status, process_item=None):
        cfg = enabled()
        if cfg and status is not None:
            item = queue.currently_running.get(item_id)  # read before the original pops it
            if item is not None:
                _safe(collector.on_done, cfg, item, status)
        return orig_task_done(item_id, history_result, status, process_item=process_item)

    queue.task_done = task_done

    orig_execute = execution.execute

    async def execute(*a, **kw):
        # execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id, ...)
        if not enabled():
            return await orig_execute(*a, **kw)
        node_id, prompt_id = a[3], a[6]
        _safe(collector.node_start, prompt_id)
        t = time.perf_counter()
        try:
            return await orig_execute(*a, **kw)
        finally:
            collector.node_done(prompt_id, node_id, time.perf_counter() - t)

    execution.execute = execute

    # validate_prompt only gets the prompt; remember each request's trace_id by prompt identity
    trace_ids = {}

    def on_prompt(json_data):
        if isinstance(json_data.get("prompt"), dict):
            trace_ids[id(json_data["prompt"])] = (json_data.get("extra_data") or {}).get("trace_id")
        return json_data

    server.add_on_prompt_handler(on_prompt)

    orig_validate = execution.validate_prompt

    async def validate_prompt(prompt_id, prompt, *a, **kw):
        trace_id = trace_ids.pop(id(prompt), None)
        result = await orig_validate(prompt_id, prompt, *a, **kw)
        cfg = enabled()
        if cfg and not result[0]:
            _safe(collector.on_rejected, cfg, prompt_id, prompt, result, trace_id)
        return result

    execution.validate_prompt = validate_prompt

    def ts_param(request, name):
        value = request.query.get(name)
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    @server.routes.get("/nkg/metrics")
    async def metrics(request):
        if not enabled():
            raise web.HTTPNotFound()
        gauges = collector.gauges(queue) + [("comfyui_draining", (), int(drain.active()))]
        return web.Response(text=collector.metrics.render(gauges),
                            content_type="text/plain", charset="utf-8")

    @server.routes.get("/nkg/stats/events")
    async def events(request):
        cfg = enabled()
        if not cfg:
            raise web.HTTPNotFound()
        try:
            since, until = ts_param(request, "since"), ts_param(request, "until")
            limit = min(int(request.query.get("limit", 50)), 1000)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response(collector.store.query(cfg, args.port, request.query.get("status"), since, until, limit))

    @server.routes.get("/nkg/stats/events/{prompt_id}")
    async def event(request):
        cfg, prompt_id = enabled(), request.match_info["prompt_id"]
        if not cfg or not UUID_RE.fullmatch(prompt_id):
            raise web.HTTPNotFound()
        _, found = collector.store.find(cfg, args.port, prompt_id)
        if found is None:
            raise web.HTTPNotFound()
        return web.json_response(found)

    @server.routes.get("/nkg/stats/events/{prompt_id}/graph")
    async def graph(request):
        cfg, prompt_id = enabled(), request.match_info["prompt_id"]
        if not cfg or not UUID_RE.fullmatch(prompt_id):
            raise web.HTTPNotFound()
        inst_dir, days = collector.store.days(cfg, args.port)
        for day in days:
            path = os.path.join(inst_dir, day, "graphs", prompt_id + ".json")
            if os.path.isfile(path):
                return web.FileResponse(path, headers={"Content-Type": "application/json"})
        raise web.HTTPNotFound()
