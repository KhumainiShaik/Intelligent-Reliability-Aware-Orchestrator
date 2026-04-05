"""
Export a pre-trained MobileNetV2 to ONNX format.

Run this ONCE on your development machine before building the Docker image:

    pip install torch torchvision          # CPU-only is fine
    python ml-workload/export_model.py     # from the repo root

The exported model (~14 MB) will be placed in ml-workload/model/mobilenetv2.onnx.
"""

from __future__ import annotations

import os
import sys

MODEL_DIR = os.path.join(os.path.dirname(__file__), "model")
OUTPUT_PATH = os.path.join(MODEL_DIR, "mobilenetv2.onnx")


def export() -> None:
    try:
        import torch
        import torchvision.models as models
    except ImportError:
        print(
            "ERROR: torch and torchvision are required.\n"
            "  pip install torch torchvision\n",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Downloading MobileNetV2 pre-trained weights …")
    model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
    model.eval()

    dummy_input = torch.randn(1, 3, 224, 224)

    os.makedirs(MODEL_DIR, exist_ok=True)

    print(f"Exporting to {OUTPUT_PATH} …")
    torch.onnx.export(
        model,
        dummy_input,
        OUTPUT_PATH,
        export_params=True,
        opset_version=13,
        do_constant_folding=True,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={
            "input": {0: "batch_size"},
            "output": {0: "batch_size"},
        },
    )

    size_mb = os.path.getsize(OUTPUT_PATH) / (1024 * 1024)
    print(f"✓ Model exported: {OUTPUT_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    export()
