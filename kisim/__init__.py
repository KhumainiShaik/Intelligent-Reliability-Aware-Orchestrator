"""
KISim -- Kubernetes-Inspired Simulator for Rollout Episode Generation.

Inspired by the KIS-S framework (Li et al., 2025; https://github.com/GuilinDev/KISim),
this simulator was adapted and re-engineered for deploy-time rollout strategy
selection under Kubernetes autoscaling dynamics.  While KIS-S focuses on GPU
inference auto-scaling, KISim models progressive-delivery strategies (canary,
rolling, delay, pre-scale), HPA interactions, fault injection, and SLO-aware
cost functions specific to this dissertation's research question.

Traffic patterns are calibrated against public traces (WorldCup98, Azure
Functions) and cluster pressure regimes from Alibaba Cluster Trace v2018.
"""

__version__ = "0.1.0"
__all__ = ["sim", "training"]
