"""
Materialiser — creates/updates Argo Rollouts resources.

Builds an unstructured Argo Rollout object matching the chosen strategy
(canary, rolling, delay, pre-scale) and applies it to the cluster via the
Kubernetes dynamic client.
"""

from __future__ import annotations

import logging
import time
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
        self._apps = k8s_client.AppsV1Api(api_client)
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

    def _resolve_container_spec(self, namespace: str, target_name: str, new_image: str, release_tag: str) -> dict:
        """
        Build the container spec for the Argo Rollout pods.

        Copies args from the live target Deployment so the rollout pods get
        the same configuration (cpu-work-ms, warmup-delay, etc.), then
        overrides --version and --image with the release values.
        """
        container: dict[str, Any] = {
            "name": target_name,
            "image": new_image,
            "ports": [{"containerPort": 8080, "name": "http"}],
        }

        try:
            deployment = self._apps.read_namespaced_deployment(
                name=target_name, namespace=namespace
            )
            api_client = k8s_client.ApiClient()
            if deployment.spec and deployment.spec.template.spec:
                containers = deployment.spec.template.spec.containers
                # Match by name; fall back to the first container (Helm typically
                # names the container after the chart name which differs from the
                # Deployment name, e.g. "workload" vs "workload-workload").
                c = next(
                    (c for c in containers if c.name == target_name),
                    containers[0] if containers else None,
                )
                if c is not None:
                    if c.resources:
                        container["resources"] = {
                            "requests": {
                                k: v for k, v in (c.resources.requests or {}).items()
                            },
                            "limits": {
                                k: v for k, v in (c.resources.limits or {}).items()
                            },
                        }
                    if c.liveness_probe:
                        container["livenessProbe"] = api_client.sanitize_for_serialization(c.liveness_probe)
                    if c.readiness_probe:
                        container["readinessProbe"] = api_client.sanitize_for_serialization(c.readiness_probe)
                    if c.security_context:
                        container["securityContext"] = api_client.sanitize_for_serialization(c.security_context)
                    if c.env:
                        env = api_client.sanitize_for_serialization(c.env)
                        for env_var in env:
                            if env_var.get("name") == "VERSION":
                                env_var["value"] = release_tag
                        container["env"] = env
                    if c.env_from:
                        container["envFrom"] = api_client.sanitize_for_serialization(c.env_from)
                    if c.command:
                        container["command"] = list(c.command)
                    if c.volume_mounts:
                        container["volumeMounts"] = api_client.sanitize_for_serialization(c.volume_mounts)
                    # Copy args, overriding --version with the release tag
                    if c.args:
                        new_args = []
                        for arg in c.args:
                            if arg.startswith("--version="):
                                new_args.append(f"--version={release_tag}")
                            else:
                                new_args.append(arg)
                        container["args"] = new_args
        except Exception as exc:
            logger.warning(
                "Could not fetch container spec from Deployment %s/%s; using minimal spec: %s",
                namespace, target_name, exc,
            )

        return container

    def _resolve_selector_labels(self, namespace: str, target_name: str) -> dict[str, str]:
        """Resolve selector labels from the target Deployment.

        Argo Rollouts validates that the stable/canary Services' selector labels are
        present in the Rollout's selector. In this codebase, the workload Services
        and Deployment selectors come from Helm's `workload.selectorLabels` helper.
        Using the live Deployment selector ensures consistency and avoids InvalidSpec.
        """
        try:
            deployment = self._apps.read_namespaced_deployment(
                name=target_name,
                namespace=namespace,
            )
            match_labels = (deployment.spec.selector.match_labels or {}) if deployment.spec and deployment.spec.selector else {}
            if isinstance(match_labels, dict) and match_labels:
                return dict(match_labels)
        except Exception as exc:
            logger.warning(
                "Failed to resolve selector labels from Deployment %s/%s; falling back: %s",
                namespace,
                target_name,
                exc,
            )

        # Backwards-compatible fallback.
        return {"app": target_name}

    def apply(
        self,
        oroll: dict,
        action: str,
        pre_scale_extra_replicas: int | None = None,
    ) -> None:
        """
        Create or update the Argo Rollout for *oroll* (the OrchestratedRollout CR dict).
        """
        namespace = oroll["metadata"]["namespace"]
        rollout_body = self._build_rollout(oroll, action, pre_scale_extra_replicas)
        rollout_name = rollout_body["metadata"]["name"]

        # Avoid a get→create race window: create first; if it already exists, patch.
        # In concurrent reconciles, two handlers can try to create the same Rollout.
        # The loser sees HTTP 409 AlreadyExists; treat it as an update.
        for attempt in (1, 2):
            try:
                logger.info("Creating Argo Rollout %s/%s", namespace, rollout_name)
                self._dyn.create_namespaced_custom_object(
                    group=ROLLOUT_GROUP,
                    version="v1alpha1",
                    namespace=namespace,
                    plural=ROLLOUT_PLURAL,
                    body=rollout_body,
                )
                return
            except ApiException as exc:
                if exc.status != 409:
                    raise

            try:
                logger.info("Updating Argo Rollout %s/%s", namespace, rollout_name)
                self._dyn.patch_namespaced_custom_object(
                    group=ROLLOUT_GROUP,
                    version="v1alpha1",
                    namespace=namespace,
                    plural=ROLLOUT_PLURAL,
                    name=rollout_name,
                    body=rollout_body,
                )
                return
            except ApiException as exc:
                # Extremely rare: object deleted between create(409) and patch(404).
                if exc.status == 404 and attempt == 1:
                    continue

                # Argo Rollouts forbids changing spec.selector after creation.
                # If an older Rollout exists with a different selector, patching will
                # fail with 422. In that case, delete and recreate the Rollout.
                if exc.status == 422 and (exc.body and "spec.selector" in exc.body and "immutable" in exc.body):
                    logger.warning(
                        "Rollout %s/%s selector is immutable; deleting and recreating",
                        namespace,
                        rollout_name,
                    )
                    try:
                        self._dyn.delete_namespaced_custom_object(
                            group=ROLLOUT_GROUP,
                            version="v1alpha1",
                            namespace=namespace,
                            plural=ROLLOUT_PLURAL,
                            name=rollout_name,
                            body={"gracePeriodSeconds": 0, "propagationPolicy": "Background"},
                        )
                    except ApiException as del_exc:
                        # If it vanished already, proceed to recreate.
                        if del_exc.status != 404:
                            raise

                    # Wait briefly for deletion to take effect before recreating.
                    for _ in range(20):
                        try:
                            self._dyn.get_namespaced_custom_object(
                                group=ROLLOUT_GROUP,
                                version="v1alpha1",
                                namespace=namespace,
                                plural=ROLLOUT_PLURAL,
                                name=rollout_name,
                            )
                            time.sleep(0.25)
                        except ApiException as get_exc:
                            if get_exc.status == 404:
                                break
                            raise

                    # Recreate (may still race with finalizers; retry briefly).
                    for _ in range(20):
                        try:
                            self._dyn.create_namespaced_custom_object(
                                group=ROLLOUT_GROUP,
                                version="v1alpha1",
                                namespace=namespace,
                                plural=ROLLOUT_PLURAL,
                                body=rollout_body,
                            )
                            return
                        except ApiException as create_exc:
                            if create_exc.status == 409:
                                time.sleep(0.25)
                                continue
                            raise

                    # If we get here, the Rollout is stuck deleting/creating.
                    raise
                raise

    def _build_rollout(
        self,
        oroll: dict,
        action: str,
        pre_scale_extra_replicas: int | None = None,
    ) -> dict:
        spec = oroll.get("spec", {})
        target_ref = spec.get("targetRef", {})
        release = spec.get("release", {})
        target_name = target_ref.get("name", "workload")
        namespace = oroll["metadata"]["namespace"]
        selector_labels = self._resolve_selector_labels(namespace, target_name)

        image = release.get("image", "")
        tag = release.get("tag", "")
        if tag:
            image = f"{image}:{tag}"

        hints = spec.get("rolloutHints") or {}
        replicas = hints.get("targetReplicas", 2)

        container_spec = self._resolve_container_spec(namespace, target_name, image, tag or image)

        rollout: dict[str, Any] = {
            "apiVersion": ROLLOUT_API_VERSION,
            "kind": ROLLOUT_KIND,
            "metadata": {
                "name": f"{target_name}-rollout",
                "namespace": namespace,
                "labels": {
                    "app": target_name,
                    "orchestrated-rollout.io/name": oroll["metadata"]["name"],
                    "orchestrated-rollout.io/strategy": action,
                },
                "annotations": {},
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
                "selector": {"matchLabels": selector_labels},
                "template": {
                    "metadata": {
                        "labels": selector_labels,
                        "annotations": {
                            "prometheus.io/scrape": "true",
                            "prometheus.io/port": "8080",
                            "prometheus.io/path": "/metrics",
                        },
                    },
                    "spec": {
                        "containers": [container_spec],
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
            extra_replicas = self._resolve_pre_scale_extra_replicas(
                hints,
                max_extra=max_extra,
                requested_extra=pre_scale_extra_replicas,
            )
            rollout["spec"]["replicas"] = replicas + extra_replicas
            rollout["metadata"]["annotations"][
                "orchestrated-rollout.io/pre-scale-extra-replicas"
            ] = str(extra_replicas)
            rollout["spec"]["strategy"] = self._canary_strategy(target_name)
        elif action == ACTION_DELAY:
            guardrail_cfg = spec.get("guardrailConfig")
            rollout["spec"]["strategy"] = self._delayed_canary_strategy(target_name, guardrail_cfg)

        return rollout

    @staticmethod
    def _resolve_pre_scale_extra_replicas(
        hints: dict[str, Any],
        max_extra: int,
        requested_extra: int | None = None,
    ) -> int:
        """Resolve extra replicas for pre-scale while preserving fixed baseline default.

        The historical fixed Pre-Scale baseline uses +3 replicas. Adaptive
        callers may request a larger value through the reconciler. Rollout hints
        are intentionally not used here so fixed baselines remain fixed.
        """
        extra = 3
        if requested_extra is not None:
            extra = requested_extra

        return max(0, min(max_extra, extra))

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
