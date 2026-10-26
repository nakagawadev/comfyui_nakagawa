import os
import time

from src.comfyui_nakagawa import stats

GRAPH = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
    "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t5xxl.safetensors", "type": "flux"}},
    "4": {"class_type": "CR LoRA Stack", "inputs": {
        "switch_1": "On", "lora_name_1": "skin.safetensors", "model_weight_1": 0.8, "clip_weight_1": 1.0,
        "switch_2": "Off", "lora_name_2": "None", "model_weight_2": 1, "clip_weight_2": 1}},
    "5": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "eyes.safetensors", "strength_model": 0.5, "model": ["1", 0]}},
    "6": {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": "bbox/face_yolov8m.pt"}},
    "7": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 4}},
    "8": {"class_type": "KSampler", "inputs": {"steps": 20, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple",
                                               "model": ["5", 0], "positive": ["9", 0]}},
    "9": {"class_type": "FaceDetailer", "inputs": {"steps": 10, "image": ["8", 0]}},
}


def test_extract():
    models, params = stats.extract(GRAPH)
    by_value = {m["value"]: m for m in models}
    assert set(by_value) == {"krea2.safetensors", "ae.safetensors", "t5xxl.safetensors", "skin.safetensors",
                             "eyes.safetensors", "bbox/face_yolov8m.pt"}
    assert by_value["krea2.safetensors"]["kind"] == "unet"
    assert by_value["ae.safetensors"]["kind"] == "vae"
    assert by_value["t5xxl.safetensors"]["kind"] == "clip"
    assert by_value["bbox/face_yolov8m.pt"]["kind"] == "other"
    skin = by_value["skin.safetensors"]
    assert (skin["kind"], skin["model_weight"], skin["clip_weight"], skin["switch"]) == ("lora", 0.8, 1.0, "On")
    assert by_value["eyes.safetensors"]["strength_model"] == 0.5
    assert params["batch_size"] == 4 and params["width"] == 1024 and params["steps"] == [20, 10]
    assert "model" not in params and stats.images_requested(params) == 4


def test_truncate():
    graph = {"1": {"inputs": {"data": "x" * 5000, "text": "short", "n": 3, "l": ["y" * 2000]}}}
    out = stats.truncate(graph)
    assert out["1"]["inputs"]["data"].startswith("<truncated 5000 bytes sha256:")
    assert out["1"]["inputs"]["text"] == "short" and out["1"]["inputs"]["n"] == 3
    assert out["1"]["inputs"]["l"][0].startswith("<truncated 2000")


def test_config_lookup_and_merge(tmp_path):
    shared, root = tmp_path / "shared", tmp_path / "root"
    shared.mkdir()
    root.mkdir()
    extra = [[str(shared / "extra_model_paths.yaml")]]
    # missing everywhere: preferred location is next to the extra paths file, and it reads as disabled
    path = stats.find_config(extra, str(root))
    assert path == str(shared / stats.CONFIG_NAME)
    assert stats.Config(path, 7855).get()["enabled"] is False
    # fallback to the ComfyUI root without --extra-model-paths-config
    (root / stats.CONFIG_NAME).write_text("stats: {enabled: true}")
    assert stats.find_config(None, str(root)) == str(root / stats.CONFIG_NAME)

    (shared / stats.CONFIG_NAME).write_text(
        'stats: {save_graph: all, retention_days: 7}\ninstances: {"7860": {save_graph: off, enabled: false}}\n')
    path = stats.find_config(extra, str(root))
    assert path == str(shared / stats.CONFIG_NAME)
    global_cfg = stats.Config(path, 7855).get()
    assert (global_cfg["enabled"], global_cfg["save_graph"], global_cfg["retention_days"], global_cfg["max_mb"]) == (True, "all", 7, 500)
    inst = stats.Config(path, 7860).get()
    assert inst["enabled"] is False and inst["save_graph"] == "off" and inst["retention_days"] == 7


def test_rotate(tmp_path):
    for day, size in (("2026-10-01", 10), ("2026-10-20", 600), ("2026-10-25", 600), ("2026-10-26", 2000)):
        (tmp_path / day).mkdir()
        (tmp_path / day / "events.jsonl").write_bytes(b"x" * size)
    (tmp_path / "notaday").mkdir()
    stats.rotate(str(tmp_path), retention_days=14, max_bytes=2700, today="2026-10-26")
    # 10-01 is past retention; 10-20 goes for size; today stays even though it alone is close to the cap
    assert sorted(os.listdir(tmp_path)) == ["2026-10-25", "2026-10-26", "notaday"]
    stats.rotate(str(tmp_path), retention_days=14, max_bytes=100, today="2026-10-26")
    assert sorted(os.listdir(tmp_path)) == ["2026-10-26", "notaday"]


def test_store_query_and_find(tmp_path):
    cfg = {**stats.DEFAULTS, "dir": str(tmp_path)}
    store = stats.Store()
    ids = [f"00000000-0000-4000-8000-00000000000{i}" for i in range(3)]
    now = int(time.time())
    for i, pid in enumerate(ids):
        store.write(cfg, 7855, {"prompt_id": pid, "ts": now + i, "status": "error" if i == 1 else "success"},
                    graph={"1": {"inputs": {"data": "z" * 3000}}} if i == 1 else None)
    assert [e["prompt_id"] for e in store.query(cfg, 7855)] == ids[::-1]
    errors = store.query(cfg, 7855, status="error")
    assert [e["prompt_id"] for e in errors] == [ids[1]] and errors[0]["graph"] == f"graphs/{ids[1]}.json"
    assert [e["prompt_id"] for e in store.query(cfg, 7855, since=now + 1, until=now + 1)] == [ids[1]]
    assert store.query(cfg, 7855, limit=1)[0]["prompt_id"] == ids[2]
    day, found = store.find(cfg, 7855, ids[0])
    assert found["ts"] == now
    graph_file = tmp_path / "7855" / day / "graphs" / f"{ids[1]}.json"
    assert "<truncated 3000" in graph_file.read_text()


def test_metrics_render():
    m = stats.Metrics()
    m.inc("comfyui_prompts_total", (("status", "success"),))
    m.inc("comfyui_model_uses_total", (("type", "unet"), ("name", 'we"ird\\name.safetensors')))
    m.observe("comfyui_prompt_duration_seconds", 7.5)
    out = m.render([("process_resident_memory_bytes", (), 12345678901), ("comfyui_loaded_model_bytes", (("class", "Flux"),), 5)])
    assert "# TYPE comfyui_prompts_total counter" in out
    assert 'comfyui_prompts_total{status="success"} 1' in out
    assert 'comfyui_prompts_total{status="rejected"} 0' in out
    assert 'comfyui_model_uses_total{type="unet",name="we\\"ird\\\\name.safetensors"} 1' in out
    assert 'comfyui_prompt_duration_seconds_bucket{le="5"} 0' in out
    assert 'comfyui_prompt_duration_seconds_bucket{le="10"} 1' in out
    assert 'comfyui_prompt_duration_seconds_bucket{le="+Inf"} 1' in out
    assert "comfyui_prompt_duration_seconds_sum 7.5" in out
    assert "process_resident_memory_bytes 12345678901" in out
    assert out.count("# TYPE comfyui_loaded_model_bytes gauge") == 1
