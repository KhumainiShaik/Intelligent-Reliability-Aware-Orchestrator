# Orchestrated Rollout — Makefile
PROJECT := orchestrated-rollout
CONTROLLER_IMG ?= $(PROJECT)-controller:latest
WORKLOAD_IMG ?= $(PROJECT)-workload:latest
KIND_CLUSTER ?= orchestrated-rollout

# Go (workload only)
GOOS ?= linux
GOARCH ?= amd64

# Python
PYTHON ?= python3

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------- Build ----------
.PHONY: build-workload
build-workload: ## Build the workload Go binary
	cd workload && CGO_ENABLED=0 GOOS=$(GOOS) GOARCH=$(GOARCH) go build -o ../bin/workload .

.PHONY: build
build: build-workload ## Build all binaries

# ---------- Controller (Python) ----------
.PHONY: controller-deps
controller-deps: ## Install Python controller dependencies
	$(PYTHON) -m pip install -r controller/requirements.txt

.PHONY: controller-run
controller-run: ## Run controller locally (for development)
	$(PYTHON) -m controller.main

.PHONY: controller-lint
controller-lint: ## Lint Python controller
	$(PYTHON) -m ruff check controller/
	$(PYTHON) -m mypy controller/ --ignore-missing-imports

# ---------- Docker ----------
.PHONY: docker-controller
docker-controller: ## Build controller Docker image (Python)
	docker build -t $(CONTROLLER_IMG) -f controller/Dockerfile .

.PHONY: docker-workload
docker-workload: ## Build workload Docker image (Go)
	docker build -t $(WORKLOAD_IMG) -f workload/Dockerfile .

.PHONY: docker
docker: docker-controller docker-workload ## Build all Docker images

.PHONY: kind-load
kind-load: ## Load controller+workload images into kind cluster
	kind load docker-image $(CONTROLLER_IMG) --name $(KIND_CLUSTER)
	kind load docker-image $(WORKLOAD_IMG) --name $(KIND_CLUSTER)

.PHONY: setup-kind
setup-kind: ## Create kind cluster + monitoring + GitOps (Argo CD/Rollouts)
	./scripts/01_setup_cluster.sh
	./scripts/02_setup_monitoring.sh
	./scripts/03_setup_gitops.sh
	./scripts/04_setup_litmus.sh

.PHONY: demo-compare
demo-compare: ## Run the spike comparison (auto ingress port-forward)
	AUTO_PORT_FORWARD=1 bash ./scripts/run_comparison.sh

.PHONY: demo-e2e
demo-e2e: ## Run a single e2e scenario (default: steady)
	AUTO_PORT_FORWARD=1 bash ./scripts/run_e2e_experiment.sh steady

# ---------- Test ----------
.PHONY: test-workload
test-workload: ## Run Go workload tests
	cd workload && go test ./... -v -count=1

.PHONY: test-controller
test-controller: ## Run Python controller tests
	$(PYTHON) -m pytest tests/ -v

.PHONY: test
test: test-workload test-controller ## Run all tests

# ---------- Deploy (Helm) ----------
.PHONY: install-crd
install-crd: ## Install CRDs into cluster
	kubectl apply -f charts/controller/crds/

.PHONY: deploy-controller
deploy-controller: ## Deploy controller to cluster (Helm)
	helm upgrade --install controller charts/controller \
		--namespace orchestrated-rollout --create-namespace

.PHONY: deploy-workload
deploy-workload: ## Deploy workload to cluster (Helm)
	helm upgrade --install workload charts/workload \
		--namespace orchestrated-rollout --create-namespace

.PHONY: deploy-monitoring
deploy-monitoring: ## Deploy Prometheus stack (Helm)
	helm upgrade --install monitoring charts/monitoring \
		--namespace monitoring --create-namespace

.PHONY: deploy-all
deploy-all: install-crd deploy-monitoring deploy-controller deploy-workload ## Deploy everything

# ---------- k6 ----------
.PHONY: k6-steady
k6-steady: ## Run k6 steady load test
	k6 run k6/scenarios/steady.js

.PHONY: k6-ramp
k6-ramp: ## Run k6 ramp load test
	k6 run k6/scenarios/ramp.js

.PHONY: k6-spike
k6-spike: ## Run k6 spike load test
	k6 run k6/scenarios/spike.js

.PHONY: k6-worldcup98
k6-worldcup98: ## Run k6 WorldCup98 flash-crowd scenario (trace-calibrated)
	k6 run k6/scenarios/worldcup98_burst.js

.PHONY: k6-azure-functions
k6-azure-functions: ## Run k6 Azure Functions burst scenario (trace-calibrated)
	k6 run k6/scenarios/azure_functions_burst.js

.PHONY: k6-alibaba
k6-alibaba: ## Run k6 Alibaba cluster pressure scenario (trace-calibrated)
	k6 run k6/scenarios/alibaba_pressure.js

# ---------- KISim ----------
.PHONY: kisim-train
kisim-train: ## Train RL policy offline
	cd kisim && $(PYTHON) -m training.train --episodes 50000 --seed 42

.PHONY: kisim-eval
kisim-eval: ## Evaluate trained policy
	cd kisim && $(PYTHON) -m training.evaluate

# ---------- Clean ----------
.PHONY: clean
clean: ## Remove build artifacts
	rm -rf bin/ coverage.out coverage.html __pycache__ controller/__pycache__
