[comment]: # (You may find the following markdown cheat sheet useful: https://www.markdownguide.org/cheat-sheet/. You may also consider using an online Markdown editor such as StackEdit or makeareadme.) 

## Project title: *Intelligent Reliability-Aware Orchestration for Cloud Systems*

### Student name: *Abdul Khumaini Shaik*

### Student email: *aks85@student.le.ac.uk*

### Project description: 
*Modern cloud systems, particularly those hosting machine learning workloads on Kubernetes, suffer frequent service disruptions not because of application bugs, but due to poor coordination between deployment operations and dynamic workload conditions. In real production environments, deployments often coincide with sudden workload surges triggered by user demand, scheduled jobs, or external system behaviour. During such periods, Kubernetes must simultaneously terminate existing pods, schedule new ones, pull container images, and rebalance cluster resources. This concurrency can result in resource starvation, delayed pod startups, cascading restarts, and in extreme cases, partial or full service outages.*

*Although Kubernetes provides mechanisms such as rolling updates and horizontal autoscaling, these mechanisms operate in isolation. Autoscalers react only after resource pressure is already visible, while deployment controllers follow predefined rollout strategies without any awareness of the operational stress already present in the cluster. Consequently, Kubernetes lacks an intelligent decision layer capable of reasoning about when and how to deploy based on the current cluster state and real-time autoscaling conditions.*

*This project addresses that gap by designing an intelligent orchestration agent that selects an optimal deployment strategy at deployment time. Given a cluster state vector 𝑆 at deployment time, the agent selects an action 𝐴 ∈ {𝑐𝑙𝑜𝑛𝑒, 𝑟𝑒𝑑𝑒𝑝𝑙𝑜𝑦, 𝑐𝑎𝑛𝑎𝑟𝑦} that minimises an expected failure cost 𝐶 under currently observed autoscaling demand 𝐷. The system evaluates real-time operational metrics such as CPU utilisation, pending pods, desired replicas, model size, scheduling latency, and historical failure behaviour to make a strategic decision before a deployment proceeds.*

*The proposed solution does not replace Kubernetes scheduling or autoscaling mechanisms. Instead, it augments them with a higher-level decision-making component that reasons about deployment timing and strategy. The effectiveness of this approach will be evaluated in a controlled Kubernetes testbed using realistic workloads and failure scenarios. Success will be measured in terms of reduced deployment failures, lower service latency during rollouts, and faster recovery times compared to standard Kubernetes behaviour.*

*The overall goal is to demonstrate that deployment reliability in cloud systems is not only an infrastructure challenge, but also a decision-making problem that can be improved through intelligent orchestration.*

### List of requirements (objectives): 

[comment]: # (You can add as many additional bullet points as necessary by adding an additional hyphon symbol '-' at the end of each list) 

**Essential:**
- Formally define the orchestration problem as a decision-making task that selects an optimal deployment strategy based on real-time cluster conditions.
- Design and implement a Kubernetes-based experimental environment that includes real deployment controllers, autoscalers, and workload generation tools.
- Develop an intelligent decision agent capable of choosing between multiple deployment strategies (clone, redeploy, canary) at deployment time.
- Integrate the agent with Kubernetes workflows so that it can intercept deployment events and influence rollout behaviour.
- Implement a measurable cost function that captures failure probability and recovery time as optimization targets.
- Conduct controlled experiments comparing the agent-driven approach with standard Kubernetes deployment mechanisms.
- Produce quantitative evaluation results demonstrating whether intelligent strategy selection improves deployment reliability.

**Desirable:**
- Incorporate additional system signals such as memory pressure, scheduling latency, and node availability into the decision process.
- Implement multiple baseline strategies (standard rolling update, delayed deployment, canary) to allow more rigorous comparison.
- Incorporate additional real-time contextual signals, such as recent autoscaling activity and scheduling backlog, to improve decision quality.
- Evaluate the system under diverse scenarios, including resource contention and simulated failure conditions.
- Provide detailed analysis of trade-offs between resource overhead and reliability improvements.
- Develop visualizations and dashboards to analyse system behaviour during experiments.

**Optional:**
- Explore reinforcement learning techniques for automated policy learning based solely on real-time observed system state instead of rule-based decision logic.
- Package the agent as a reusable Kubernetes operator or controller.
- Extend the system to support multi-cluster or multi-application environments.
- Validate the approach using external workload traces or additional real-world datasets.
- Investigate economic cost models to quantify financial savings from reduced failures.


## Information about this repository
This is the repository that you are going to use **individually** for developing your project. Please use the resources provided in the module to learn about **plagiarism** and how plagiarism awareness can foster your learning.

Regarding the use of this repository, once a feature (or part of it) is developed and **working** or parts of your system are integrated and **working**, define a commit and push it to the remote repository. You may find yourself making a commit after a productive hour of work (or even after 20 minutes!), for example. Choose commit message wisely and be concise.

Please choose the structure of the contents of this repository that suits the needs of your project but do indicate in this file where the main software artefacts are located.

## Main software artefacts (where to look)

- `controller/` — Python kopf operator (deploy-time orchestration logic)
- `charts/` — Helm charts (controller, workload, ML workload, monitoring)
- `k8s/` — Supplementary Kubernetes manifests (namespaces, Argo CD, chaos, CRD)
- `workload/` — Go workload service (target app)
- `ml-workload/` — ML inference service
- `kisim/` — offline simulator + RL training code
- `evaluation/` — analysis scripts
- `k6/` — load testing scenarios
- `experiments/` — experiment configuration (results are not committed)

## E2E runs (after cloning)

This repository intentionally does **not** commit `scripts/`, `artifacts/`, or `results/`.

- Copy your automation scripts into `./scripts/` (from your cloud storage).
- (Optional RL) Mount a `policy_artifact.json` via the `rl-policy-artifact` ConfigMap (Helm chart supports this as an optional volume).
- Deploy using Helm (`charts/`) or your scripts, then run the experiment scenarios.
