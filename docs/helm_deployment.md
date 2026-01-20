# Helm-Based vLLM Deployment Guide

This guide explains how to use Helm-based vLLM deployments as an execution backend for auto-tune-vllm.

## Overview

The Helm backend deploys vLLM servers using Helm charts and executes benchmarks as Kubernetes Jobs. This provides a fully Kubernetes-native execution model for optimization studies.

## Prerequisites

### Required Tools

- **Helm CLI**: Version 3.0+ installed and configured
- **Kubernetes Cluster**: Access to a Kubernetes cluster with:
  - GPU nodes (for vLLM serving)
  - Sufficient resources for concurrent trials
  - Network access between pods
- **kubectl**: Configured to access your cluster
- **Python Dependencies**: Install with Helm support:
  ```bash
  pip install 'auto-tune-vllm[helm]'
  ```

### Kubernetes Permissions

The service account or user running auto-tune-vllm needs:
- `create`, `get`, `list`, `delete` on `jobs` (for benchmarks)
- `create`, `get`, `list`, `delete` on `services` (for service discovery)
- `get`, `list` on `pods` (for log extraction)
- Helm release management permissions in the target namespace

## Configuration

### Basic Configuration

Add Helm configuration to your study config file:

```yaml
execution:
  backend: "helm"
  helm:
    chart_repo: "llm-d-modelservice/llm-d-modelservice"
    chart_name: "llm-d-modelservice"
    chart_version: "v0.3.8"
    namespace: "llm-d-trials"
    benchmark_image: "guidellm:latest"  # Optional: container image for benchmarks
```

### Configuration Options

#### `chart_path` (optional)
Path to a local Helm chart directory:
```yaml
helm:
  chart_path: "/path/to/helm-chart"
```

#### `chart_repo` + `chart_name` (required if not using chart_path)
Helm chart repository and chart name:
```yaml
helm:
  chart_repo: "https://llm-d-incubation.github.io/llm-d-modelservice/"
  chart_name: "llm-d-modelservice"
  chart_version: "v0.3.8"  # Optional: specific version
```

#### `namespace` (default: "default")
Kubernetes namespace for deployments:
```yaml
helm:
  namespace: "llm-d-trials"
```

#### `kubeconfig` (optional)
Path to kubeconfig file (if not using default):
```yaml
helm:
  kubeconfig: "/path/to/kubeconfig"
```

#### `values_template` (optional)
Path to a Helm values template file:
```yaml
helm:
  values_template: "./helm-values-template.yaml"
```

#### `benchmark_image` (optional)
Container image for benchmark Jobs (default: "guidellm:latest"):
```yaml
helm:
  benchmark_image: "my-registry/guidellm:v1.0.0"
```

## Usage

### CLI Usage

```bash
# Basic usage with Helm backend
auto-tune-vllm optimize \
  --config study_config.yaml \
  --backend helm \
  --max-concurrent-trials 2

# With CLI overrides
auto-tune-vllm optimize \
  --config study_config.yaml \
  --backend helm \
  --k8s-namespace my-namespace \
  --helm-chart-repo https://repo.example.com/charts \
  --helm-chart-name vllm-chart \
  --kubeconfig ~/.kube/config \
  --max-concurrent-trials 2
```

### Configuration File Example

```yaml
study:
  name: "helm_optimization_study"

execution:
  backend: "helm"
  helm:
    chart_repo: "llm-d-modelservice/llm-d-modelservice"
    chart_name: "llm-d-modelservice"
    chart_version: "v0.3.8"
    namespace: "llm-d-trials"
    benchmark_image: "guidellm:latest"

optimization:
  preset: "high_throughput"
  n_trials: 50
  max_concurrent_trials: 2

benchmark:
  benchmark_type: "guidellm"
  model: "Qwen/Qwen3-0.6B"
  max_seconds: 300
  rate: 50

parameters:
  max_num_batched_tokens:
    enabled: true
    min: 1000
    max: 10000
    step: 1000
```

## How It Works

### Trial Execution Flow

1. **Helm Release Installation**: For each trial, a Helm release is installed with trial-specific vLLM parameters
2. **Service Discovery**: The system discovers the vLLM service URL from the Helm release
3. **Health Check**: Waits for the vLLM server to be ready
4. **Benchmark Job Creation**: Creates a Kubernetes Job that runs the benchmark workload
5. **Result Extraction**: Extracts benchmark results from the Job logs/volumes
6. **Cleanup**: Uninstalls Helm release and deletes benchmark Job

### Release Naming

Helm releases are named using the pattern: `{study_name}-{trial_id}` (sanitized for Helm compatibility).

Example: `study_12345-trial_5` becomes `study-12345-trial-5`

### Resource Management

- Each trial gets its own Helm release (isolated deployment)
- Benchmark Jobs are created per trial
- Resources are automatically cleaned up after trial completion
- Failed trials are cleaned up on study completion or interruption

## Troubleshooting

### Helm CLI Not Found

```bash
# Install Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

### Kubernetes Access Issues

```bash
# Verify cluster access
kubectl cluster-info

# Check namespace exists
kubectl get namespace llm-d-trials

# Create namespace if needed
kubectl create namespace llm-d-trials
```

### Helm Release Installation Fails

- Check Helm chart repository is accessible
- Verify chart name and version are correct
- Check Kubernetes resource quotas in namespace
- Review Helm release logs: `helm list -n <namespace>`

### Benchmark Job Fails

- Check Job logs: `kubectl logs job/<job-name> -n <namespace>`
- Verify benchmark image is accessible
- Check resource limits (CPU/memory)
- Ensure vLLM service is accessible from Job pods

### Service Discovery Issues

- Verify Services are created by Helm chart
- Check Service labels match expected patterns
- Review Service endpoints: `kubectl get svc -n <namespace>`

## Best Practices

1. **Namespace Isolation**: Use dedicated namespaces for optimization studies
2. **Resource Quotas**: Set appropriate resource quotas to prevent cluster overload
3. **Concurrent Trials**: Adjust `max_concurrent_trials` based on cluster capacity
4. **Helm Chart Compatibility**: Ensure Helm chart supports the vLLM parameters you're optimizing
5. **Benchmark Image**: Use a reliable, accessible container image for benchmarks
6. **Cleanup**: Monitor and clean up failed releases if studies are interrupted

## Comparison with Ray Backend

| Feature | Ray Backend | Helm Backend |
|---------|------------|--------------|
| Deployment Model | Ray actors on cluster | Helm releases in Kubernetes |
| Benchmark Execution | Subprocess on Ray worker | Kubernetes Job |
| Resource Management | Ray resource scheduling | Kubernetes resource requests/limits |
| Service Discovery | Ray object store | Kubernetes Services |
| Cleanup | Ray actor termination | Helm uninstall + Job deletion |
| Use Case | Ray clusters | Pure Kubernetes environments |

## Advanced Configuration

### Custom Values Template

Create a values template that defines base configuration:

```yaml
# helm-values-template.yaml
decode:
  replicas: 1
  containers:
    - name: vllm
      image: ghcr.io/llm-d/llm-d-cuda:v0.3.1
      resources:
        limits:
          nvidia.com/gpu: "1"
```

Trial-specific parameters will be merged into this template.

### Release Name Template

Customize release naming (future feature):
```yaml
helm:
  release_name_template: "vllm-{study_name}-{trial_id}"
```

## Limitations

- Requires Kubernetes cluster access
- Helm CLI must be installed and configured
- Benchmark container image must be accessible
- Service discovery depends on Helm chart structure
- Each trial creates a full Helm release (may be resource-intensive)

## Migration from Ray Backend

To migrate from Ray to Helm backend:

1. Update config file: Change `execution.backend` to `"helm"`
2. Add Helm configuration section
3. Ensure Kubernetes cluster is accessible
4. Install Helm dependencies: `pip install 'auto-tune-vllm[helm]'`
5. No changes needed to `parameters`, `benchmark`, or `optimization` sections

Existing study data and configurations remain compatible.
