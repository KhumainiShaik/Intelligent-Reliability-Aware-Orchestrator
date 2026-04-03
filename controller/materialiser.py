"""
Materialiser — creates/updates Argo Rollouts resources.

Builds an unstructured Argo Rollout object matching the chosen strategy
(canary, rolling, delay, pre-scale) and applies it to the cluster via the
Kubernetes dynamic client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from controller.guardrails import get_max_extra_replicas
from controller.policy import (
    ACTION_CANARY,
    ACTION_DELAY,
    ACTION_PRE_SCALE,
    ACTION_ROLLING,
)

if TYPE_CHECKING:
    from controller.config import ControllerConfig

logger = logging.getLogger(__name__)

ROLLOUT_API_VERSION = "argoproj.io/v1alpha1"
ROLLOUT_KIND = "Rollout"
ROLLOUT_PLURAL = "rollouts"
ROLLOUT_GROUP = "argoproj.io"


class Materialiser:
    """Creates / patches Argo Rollouts resources for chosen actions."""

    def __init__(
        self,
        api_client: k8s_client.ApiClient | None = None,
        canary_steps_weights: tuple[int, ...] = (10, 25, 50, 100),
        canary_pause_duration: str = "30s",
        default_delay_seconds: int = 120,
    ) -> None:
        self._dyn = k8s_client.CustomObjectsApi(api_client)
        self._canary_steps = canary_steps_weights
        self._pause_duration = canary_pause_duration
        self._default_delay = default_delay_seconds

    @classmethod
    def from_config(
        cls,
        cfg: ControllerConfig,
        api_client: k8s_client.ApiClient | None = None,
    ) -> Materialiser:
        """Factory: build a Materialiser from a :class:`ControllerConfig`."""
        return cls(
            api_client=api_client,
            canary_steps_weights=cfg.canary_steps_weights,
            canary_pause_duration=cfg.canary_pause_duration,
            default_delay_seconds=cfg.default_delay_seconds,
        )

    def apply(self, oroll: dict, action: str) -> None:
        """
        Create or update the Argo Rollout for *oroll* (the OrchestratedRollout CR dict).
        """
        namespace = oroll["metadata"]["namespace"]
        rollout_body = self._build_rollout(oroll, action)
        rollout_name = rollout_body["metadata"]["name"]

        try:
            # Try to get existing
            self._dyn.get_namespaced_custom_object(
                group=ROLLOUT_GROUP,
                version="v1alpha1",
                namespace=namespace,
                plural=ROLLOUT_PLURAL,
                name=rollout_name,
            )
            # Exists → patch
            logger.info("Updating Argo Rollout %s/%s", namespace, rollout_name)
            self._dyn.patch_namespaced_custom_object(
                group=ROLLOUT_GROUP,
                version="v1alpha1",
                namespace=namespace,
                plural=ROLLOUT_PLURAL,
                name=rollout_name,
                body=rollout_body,
            )
        except ApiException as exc:
            if exc.status == 404:
                logger.info("Creating Argo Rollout %s/%s", namespace, rollout_name)
                self._dyn.create_namespaced_custom_object(
                    group=ROLLOUT_GROUP,
                    version="v1alpha1",
                    namespace=namespace,
                    plural=ROLLOUT_PLURAL,
                    body=rollout_body,
                )
            else:
                raise

    def _build_rollout(self, oroll: dict, action: str) -> dict:
        spec = oroll.get("spec", {})
        target_ref = spec.get("targetRef", {})
        release = spec.get("release", {})
        target_name = target_ref.get("name", "workload")

        image = release.get("image", "")
        tag = release.get("tag", "")
        if tag:
            image = f"{image}:{tag}"

        hints = spec.get("rolloutHints") or {}
        replicas = hints.get("targetReplicas", 2)

        rollout: dict[str, Any] = {
            "apiVersion": ROLLOUT_API_VERSION,
            "kind": ROLLOUT_KIND,
            "metadata": {
                "name": f"{target_name}-rollout",
                "namespace": oroll["metadata"]["namespace"],
                "labels": {
                    "app": target_name,
                    "orchestrated-rollout.io/name": oroll["metadata"]["name"],
                    "orchestrated-rollout.io/strategy": action,
                },
                "ownerReferences": [
                    {
                        "apiVersion": oroll.get("apiVersion", "rollout.orchestrated.io/v1alpha1"),
                        "kind": oroll.get("kind", "OrchestratedRollout"),
                        "name": oroll["metadata"]["name"],
                        "uid": oroll["metadata"]["uid"],
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": target_name}},
                "template": {
                    "metadata": {
                        "labels": {"app": target_name},
                        "annotations": {
                            "prometheus.io/scrape": "true",
                            "prometheus.io/port": "8080",
                            "prometheus.io/path": "/metrics",
                        },
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": target_name,
                                "image": image,
                                "ports": [{"containerPort": 8080, "name": "http"}],
                            }
                        ]
                    },
                },
            },
        }

        # Strategy-specific configuration
        if action == ACTION_CANARY:
            rollout["spec"]["strategy"] = self._canary_strategy(target_name)
        elif action == ACTION_ROLLING:
            rollout["spec"]["strategy"] = self._rolling_strategy()
        elif action == ACTION_PRE_SCALE:
            guardrail_cfg = spec.get("guardrailConfig")
            max_extra = get_max_extra_replicas(guardrail_cfg)
            rollout["spec"]["replicas"] = replicas + min(max_extra, 3)
            rollout["spec"]["strategy"] = self._canary_strategy(target_name)
        elif action == ACTION_DELAY:
            guardrail_cfg = spec.get("guardrailConfig")
            rollout["spec"]["strategy"] = self._delayed_canary_strategy(target_name, guardrail_cfg)

        return rollout

    # strategy builders

    def _canary_strategy(self, target_name: str) -> dict:
        steps: list[dict] = []
        for w in self._canary_steps:
            steps.append({"setWeight": w})
            if w < 100:
                steps.append({"pause": {"duration": self._pause_duration}})
        return {
            "canary": {
                "stableService": f"{target_name}-stable",
                "canaryService": f"{target_name}-canary",
                "trafficRouting": {
                    "nginx": {
                        "stableIngress": f"{target_name}-ingress",
                    }
                },
                "steps": steps,
            }
        }

    @staticmethod
    def _rolling_strategy() -> dict:
        return {
            "canary": {
                "maxSurge": "25%",
                "maxUnavailable": "25%",
                "steps": [{"setWeight": 100}],
            }
        }

    def _delayed_canary_strategy(self, target_name: str, guardrail_cfg: dict | None) -> dict:
        delay_seconds = self._default_delay
        if guardrail_cfg and guardrail_cfg.get("maxDelaySeconds", 0) > 0:
            delay_seconds = guardrail_cfg["maxDelaySeconds"]

        steps: list[dict] = [{"pause": {"duration": f"{delay_seconds}s"}}]
        for w in self._canary_steps:
            steps.append({"setWeight": w})
            if w < 100:
                steps.append({"pause": {"duration": self._pause_duration}})

        return {
            "canary": {
                "stableService": f"{target_name}-stable",
                "canaryService": f"{target_name}-canary",
                "trafficRouting": {
                    "nginx": {
                        "stableIngress": f"{target_name}-ingress",
                    }
                },
                "steps": steps,
            }
        }
