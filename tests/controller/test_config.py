"""Unit tests for controller.config."""

from __future__ import annotations

import pytest

from controller.config import ControllerConfig, load_config


class TestControllerConfig:
    """Tests for ControllerConfig frozen dataclass."""

    def test_defaults(self) -> None:
        """Default construction uses expected values."""
        cfg = ControllerConfig()
        assert cfg.prometheus_url == "http://prometheus.monitoring.svc.cluster.local:9090"
        assert cfg.prometheus_timeout == 10.0
        assert cfg.stress_weight_latency == 0.25
        assert cfg.log_level == "INFO"

    def test_frozen(self) -> None:
        """Config is immutable after construction."""
        cfg = ControllerConfig()
        with pytest.raises(AttributeError):
            cfg.prometheus_url = "http://changed"  # type: ignore[misc]

    def test_custom_values(self) -> None:
        """Custom constructor args are stored correctly."""
        cfg = ControllerConfig(
            prometheus_url="http://custom:9090",
            stress_weight_latency=0.5,
            latency_ceiling_ms=1000.0,
        )
        assert cfg.prometheus_url == "http://custom:9090"
        assert cfg.stress_weight_latency == 0.5
        assert cfg.latency_ceiling_ms == 1000.0


class TestLoadConfig:
    """Tests for load_config() environment-variable parsing."""

    def test_defaults_no_env(self, clean_env: None) -> None:
        """Without env vars, load_config() returns default values."""
        cfg = load_config()
        assert cfg.prometheus_url == ControllerConfig.prometheus_url
        assert cfg.prometheus_timeout == ControllerConfig.prometheus_timeout

    def test_env_override_string(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """String env vars override defaults."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://prom:1234")
        cfg = load_config()
        assert cfg.prometheus_url == "http://prom:1234"

    def test_env_override_float(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Float env vars are parsed correctly."""
        monkeypatch.setenv("STRESS_WEIGHT_LATENCY", "0.40")
        cfg = load_config()
        assert cfg.stress_weight_latency == pytest.approx(0.40)

    def test_env_override_int(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Integer env vars are parsed correctly."""
        monkeypatch.setenv("MAX_DELAY_SECONDS", "300")
        cfg = load_config()
        assert cfg.max_delay_seconds == 300

    def test_invalid_float_uses_default(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid float env var falls back to default."""
        monkeypatch.setenv("STRESS_WEIGHT_LATENCY", "not-a-number")
        cfg = load_config()
        assert cfg.stress_weight_latency == ControllerConfig.stress_weight_latency

    def test_invalid_int_uses_default(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid int env var falls back to default."""
        monkeypatch.setenv("MAX_DELAY_SECONDS", "xyz")
        cfg = load_config()
        assert cfg.max_delay_seconds == ControllerConfig.max_delay_seconds

    def test_log_level_uppercased(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOG_LEVEL is uppercased regardless of input casing."""
        monkeypatch.setenv("LOG_LEVEL", "debug")
        cfg = load_config()
        assert cfg.log_level == "DEBUG"
