"""Unit tests for controller.snapshot."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from controller.snapshot import Collector, DecisionSnapshot, degraded_snapshot


class TestDecisionSnapshot:
    """Tests for DecisionSnapshot dataclass."""

    def test_defaults(self) -> None:
        """Default snapshot has zeroed values and not degraded."""
        snap = DecisionSnapshot()
        assert snap.rps == 0.0
        assert snap.degraded is False

    def test_to_dict(self) -> None:
        """to_dict() returns serialisable dict with all fields."""
        snap = DecisionSnapshot(rps=100.0, p95_latency_ms=50.0)
        d = snap.to_dict()
        assert d["rps"] == 100.0
        assert "degraded" in d

    def test_degraded_snapshot(self) -> None:
        """degraded_snapshot() returns a snapshot with degraded=True."""
        snap = degraded_snapshot()
        assert snap.degraded is True
        assert snap.rps == 0.0


class TestCollector:
    """Tests for the Prometheus Collector."""

    def test_requires_url(self) -> None:
        """Empty prometheus_url raises ValueError."""
        with pytest.raises(ValueError, match="prometheus_url is required"):
            Collector(prometheus_url="")

    def test_constructor_success(self) -> None:
        """Valid URL allows construction."""
        c = Collector(prometheus_url="http://localhost:9090")
        assert c is not None

    @patch("controller.snapshot.Collector._query_scalar")
    def test_collect_returns_snapshot(self, mock_query: MagicMock) -> None:
        """collect() builds a DecisionSnapshot from Prometheus queries."""
        mock_query.return_value = (42.0, None)
        c = Collector(prometheus_url="http://localhost:9090")
        snap = c.collect(namespace="default", target_name="test-svc")
        assert isinstance(snap, DecisionSnapshot)
        # _query_scalar is called for each metric
        assert mock_query.call_count > 0

    @patch("controller.snapshot.Collector._query_scalar")
    def test_collect_degraded_on_many_failures(self, mock_query: MagicMock) -> None:
        """If most queries fail, collect() returns a degraded snapshot."""
        mock_query.return_value = (0.0, "connection refused")
        c = Collector(prometheus_url="http://localhost:9090")
        snap = c.collect(namespace="default", target_name="test-svc")
        assert snap.degraded is True
