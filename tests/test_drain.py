from src.comfyui_nakagawa import drain, stats


class FakeConfig:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self):
        return self.cfg


class FakeQueue:
    def get_current_queue_volatile(self):
        return (["running"], ["a", "b"])


def test_resolve_config_drain():
    raw = {"drain": True, "instances": {"8189": {"drain": False}}}
    assert stats.resolve_config(raw, 8188)["drain"] is True
    assert stats.resolve_config(raw, 8189)["drain"] is False
    assert stats.resolve_config({}, 8188)["drain"] is False


def test_drain_access():
    d = drain.Drain(FakeConfig({"drain": True}), FakeQueue())
    assert d.allowed("127.0.0.1", "127.0.0.1:8188")
    assert d.allowed("::1", "[::1]:8188") and d.allowed("127.0.0.1", "localhost")
    assert not d.allowed("100.64.0.7", "127.0.0.1:8188")  # remote caller
    assert not d.allowed("127.0.0.1", "evil.example:8188")  # DNS rebinding / proxy with a public Host
    assert not d.allowed("127.0.0.1", "127.0.0.1.evil.example")
    assert not d.allowed("127.0.0.1", "")
    d.config.cfg["drain_remote"] = True
    assert d.allowed("100.64.0.7", "comfy-host:8188")
    d.config.cfg["drain"] = False
    assert not d.allowed("127.0.0.1", "127.0.0.1:8188")  # off is off


def test_drain_state_follows_config():
    cfg = FakeConfig({"drain": True})
    d = drain.Drain(cfg, FakeQueue())
    assert d.status() == {"draining": False, "since": None, "running": 1, "pending": 2}
    d.since = 123.0
    assert d.active() and d.status()["since"] == 123.0
    cfg.cfg = {"drain": False}  # turning it off in the file stops draining at once
    assert not d.active() and d.status()["draining"] is False
