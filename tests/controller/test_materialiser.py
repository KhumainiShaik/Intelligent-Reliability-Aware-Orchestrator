"""Unit tests for controller.materialiser."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from controller.materialiser import Materialiser
from controller.policy import ACTION_PRE_SCALE

if TYPE_CHECKING:
    from controller.config import ControllerConfig


class TestMaterialiser:
    """Tests for the Materialiser class."""

    def test_from_config(self, default_config: ControllerConfig) -> None:
        """from_config() maps config fields correctly."""
        with patch("controller.materialiser.k8s_client"):
            mat = Materialiser.from_config(default_config)
            assert mat._canary_steps == default_config.canary_steps_weights
            assert mat._pause_duration == default_config.canary_pause_duration
            assert mat._default_delay == default_config.default_delay_seconds

    def test_custom_canary_steps(self) -> None:
        """Custom canary steps are stored on the instance."""
        with patch("controller.materialiser.k8s_client"):
            mat = Materialiser(
                canary_steps_weights=(20, 50, 100),
                canary_pause_duration="60s",
                default_delay_seconds=180,
            )
            assert mat._canary_steps == (20, 50, 100)
            assert mat._pause_duration == "60s"
            assert mat._default_delay == 180

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_pre_scale_default_keeps_fixed_baseline_extra(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        """Fixed Pre-Scale baseline keeps the historical +3 replica headroom."""
        dyn_api.return_value = MagicMock()
        apps_api.return_value = MagicMock()
        mat = Materialiser(api_client=None)
        mat._resolve_selector_labels = MagicMock(return_value={"app": "workload"})
        mat._resolve_container_spec = MagicMock(
            return_value={"name": "workload", "image": "repo:v2"}
        )

        rollout = mat._build_rollout(
            {
                "apiVersion": "rollout.orchestrated.io/v1alpha1",
                "kind": "OrchestratedRollout",
                "metadata": {"name": "oroll", "namespace": "ns", "uid": "uid"},
                "spec": {
                    "targetRef": {"name": "workload"},
                    "release": {"image": "repo", "tag": "v2"},
                    "rolloutHints": {"targetReplicas": 4},
                    "guardrailConfig": {"maxExtraReplicas": 5},
                },
            },
            ACTION_PRE_SCALE,
        )

        assert rollout["spec"]["replicas"] == 7
        assert (
            rollout["metadata"]["annotations"]["orchestrated-rollout.io/pre-scale-extra-replicas"]
            == "3"
        )

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_pre_scale_default_ignores_rollout_hint_extra(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        """Fixed Pre-Scale is not changed by rolloutHints.preScaleExtraReplicas."""
        dyn_api.return_value = MagicMock()
        apps_api.return_value = MagicMock()
        mat = Materialiser(api_client=None)
        mat._resolve_selector_labels = MagicMock(return_value={"app": "workload"})
        mat._resolve_container_spec = MagicMock(
            return_value={"name": "workload", "image": "repo:v2"}
        )

        rollout = mat._build_rollout(
            {
                "apiVersion": "rollout.orchestrated.io/v1alpha1",
                "kind": "OrchestratedRollout",
                "metadata": {"name": "oroll", "namespace": "ns", "uid": "uid"},
                "spec": {
                    "targetRef": {"name": "workload"},
                    "release": {"image": "repo", "tag": "v2"},
                    "rolloutHints": {
                        "targetReplicas": 4,
                        "preScaleExtraReplicas": 5,
                    },
                    "guardrailConfig": {"maxExtraReplicas": 5},
                },
            },
            ACTION_PRE_SCALE,
        )

        assert rollout["spec"]["replicas"] == 7
        assert (
            rollout["metadata"]["annotations"]["orchestrated-rollout.io/pre-scale-extra-replicas"]
            == "3"
        )

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_pre_scale_adaptive_extra_uses_requested_headroom(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        """Adaptive callers can request more headroom up to the guardrail cap."""
        dyn_api.return_value = MagicMock()
        apps_api.return_value = MagicMock()
        mat = Materialiser(api_client=None)
        mat._resolve_selector_labels = MagicMock(return_value={"app": "workload"})
        mat._resolve_container_spec = MagicMock(
            return_value={"name": "workload", "image": "repo:v2"}
        )

        rollout = mat._build_rollout(
            {
                "apiVersion": "rollout.orchestrated.io/v1alpha1",
                "kind": "OrchestratedRollout",
                "metadata": {"name": "oroll", "namespace": "ns", "uid": "uid"},
                "spec": {
                    "targetRef": {"name": "workload"},
                    "release": {"image": "repo", "tag": "v2"},
                    "rolloutHints": {"targetReplicas": 4},
                    "guardrailConfig": {"maxExtraReplicas": 5},
                },
            },
            ACTION_PRE_SCALE,
            pre_scale_extra_replicas=5,
        )

        assert rollout["spec"]["replicas"] == 9
        assert (
            rollout["metadata"]["annotations"]["orchestrated-rollout.io/pre-scale-extra-replicas"]
            == "5"
        )

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_pre_scale_adaptive_extra_is_guardrail_capped(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        """Requested adaptive headroom is capped by maxExtraReplicas."""
        dyn_api.return_value = MagicMock()
        apps_api.return_value = MagicMock()
        mat = Materialiser(api_client=None)
        mat._resolve_selector_labels = MagicMock(return_value={"app": "workload"})
        mat._resolve_container_spec = MagicMock(
            return_value={"name": "workload", "image": "repo:v2"}
        )

        rollout = mat._build_rollout(
            {
                "apiVersion": "rollout.orchestrated.io/v1alpha1",
                "kind": "OrchestratedRollout",
                "metadata": {"name": "oroll", "namespace": "ns", "uid": "uid"},
                "spec": {
                    "targetRef": {"name": "workload"},
                    "release": {"image": "repo", "tag": "v2"},
                    "rolloutHints": {"targetReplicas": 4},
                    "guardrailConfig": {"maxExtraReplicas": 5},
                },
            },
            ACTION_PRE_SCALE,
            pre_scale_extra_replicas=8,
        )

        assert rollout["spec"]["replicas"] == 9

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_apply_creates_rollout_when_absent(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        dyn = MagicMock()
        dyn_api.return_value = dyn
        apps_api.return_value = MagicMock()

        mat = Materialiser(api_client=None)
        mat._build_rollout = MagicMock(
            return_value={
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Rollout",
                "metadata": {"name": "workload-rollout", "namespace": "ns"},
                "spec": {},
            }
        )

        mat.apply({"metadata": {"namespace": "ns"}}, action="canary")

        dyn.create_namespaced_custom_object.assert_called_once()
        dyn.patch_namespaced_custom_object.assert_not_called()

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_apply_patches_when_rollout_exists(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        dyn = MagicMock()
        dyn_api.return_value = dyn
        apps_api.return_value = MagicMock()

        create_exc = ApiException(status=409, reason="AlreadyExists")
        dyn.create_namespaced_custom_object.side_effect = create_exc

        mat = Materialiser(api_client=None)
        mat._build_rollout = MagicMock(
            return_value={
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Rollout",
                "metadata": {"name": "workload-rollout", "namespace": "ns"},
                "spec": {"replicas": 1},
            }
        )

        mat.apply({"metadata": {"namespace": "ns"}}, action="canary")

        dyn.patch_namespaced_custom_object.assert_called_once()

    @patch("controller.materialiser.time.sleep", return_value=None)
    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_apply_deletes_and_recreates_on_immutable_selector(
        self,
        dyn_api: MagicMock,
        apps_api: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        dyn = MagicMock()
        dyn_api.return_value = dyn
        apps_api.return_value = MagicMock()

        # Existing rollout -> create returns 409, patch fails with immutable selector.
        dyn.create_namespaced_custom_object.side_effect = [
            ApiException(status=409, reason="AlreadyExists"),
            {},
        ]

        patch_exc = ApiException(status=422, reason="Invalid")
        patch_exc.body = "Rollout.argoproj.io is invalid: spec.selector is immutable"
        dyn.patch_namespaced_custom_object.side_effect = patch_exc

        # After delete, first get should return 404 (gone) so recreate can proceed.
        dyn.get_namespaced_custom_object.side_effect = ApiException(status=404, reason="NotFound")

        mat = Materialiser(api_client=None)
        mat._build_rollout = MagicMock(
            return_value={
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Rollout",
                "metadata": {"name": "workload-rollout", "namespace": "ns"},
                "spec": {"selector": {"matchLabels": {"a": "b"}}},
            }
        )

        mat.apply({"metadata": {"namespace": "ns"}}, action="canary")

        dyn.delete_namespaced_custom_object.assert_called_once()
        # create called twice: initial (409) + recreate (success)
        assert dyn.create_namespaced_custom_object.call_count == 2

    @patch("controller.materialiser.k8s_client.AppsV1Api")
    @patch("controller.materialiser.k8s_client.CustomObjectsApi")
    def test_resolve_container_spec_copies_env_for_ml_workload(
        self, dyn_api: MagicMock, apps_api: MagicMock
    ) -> None:
        dyn_api.return_value = MagicMock()
        apps = MagicMock()
        apps_api.return_value = apps

        container = k8s_client.V1Container(
            name="ml-workload",
            image="old-image:v1",
            env=[
                k8s_client.V1EnvVar(name="MODEL_PATH", value="/app/model/mobilenetv2.onnx"),
                k8s_client.V1EnvVar(name="VERSION", value="v1.0.0"),
            ],
            args=["--version=v1.0.0", "--other=value"],
        )
        deployment = k8s_client.V1Deployment(
            spec=k8s_client.V1DeploymentSpec(
                selector=k8s_client.V1LabelSelector(match_labels={"app": "ml-workload"}),
                template=k8s_client.V1PodTemplateSpec(
                    spec=k8s_client.V1PodSpec(containers=[container])
                ),
            )
        )
        apps.read_namespaced_deployment.return_value = deployment

        mat = Materialiser(api_client=None)
        resolved = mat._resolve_container_spec(
            namespace="orchestrated-rollout",
            target_name="ml-workload-ml-workload",
            new_image="repo/ml:v2.0.0",
            release_tag="v2.0.0",
        )

        assert resolved["image"] == "repo/ml:v2.0.0"
        assert {"name": "MODEL_PATH", "value": "/app/model/mobilenetv2.onnx"} in resolved["env"]
        assert {"name": "VERSION", "value": "v2.0.0"} in resolved["env"]
        assert "--version=v2.0.0" in resolved["args"]
        assert "--other=value" in resolved["args"]
