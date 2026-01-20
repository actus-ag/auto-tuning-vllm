---
name: Helm vLLM Deployment Integration
overview: Integrate Helm-based vLLM deployments as an alternate execution backend. The system will deploy vLLM servers via Helm charts, track Helm releases per trial for experiment reproducibility, and work with existing configuration files.
todos: []
---

# Helm-Based vLLM Deployment Integration Plan

## Overview

This plan adds Helm-based vLLM deployment as an alternate execution backend alongside the existing Ray and local backends. Helm releases will be tracked per trial to preserve experiment reproducibility, and the system will work with existing YAML configuration files.

## Architecture

The integration follows the existing backend pattern:

- New `HelmExecutionBackend` implementing `ExecutionBackend` interface
- Helm release lifecycle management (install, upgrade, delete)
- Values file generation from `TrialConfig` 
- Service discovery from Helm-deployed Kubernetes Services
- **Kubernetes Jobs for benchmark execution** (when Helm backend is used)
- Release tracking in trial metadata for reproducibility

**Key Design Decision**: When Helm backend is selected, both vLLM server AND benchmark workload run in Kubernetes:

- vLLM server: Deployed via Helm chart
- Benchmark: Deployed as Kubernetes Job
- This ensures consistent Kubernetes-native execution model

## Implementation Details

### 1. New Backend: `HelmExecutionBackend`

**Location**: `auto_tune_vllm/execution/backends.py`

- Implements `ExecutionBackend` abstract class
- Manages Helm releases per trial (one release per trial for isolation)
- Release naming: `{study_name}-{trial_id}` (sanitized for Helm)
- Tracks release names and Kubernetes Job names in `JobHandle` for cleanup
- Uses `helm` CLI or Python `helm` library for operations
- Manages Kubernetes Jobs for benchmark execution

**Key Methods**:

- `submit_trial()`: Install Helm release with generated values, create benchmark Job
- `poll_trials()`: Check Helm release status, Service readiness, and Job completion
- `cleanup_all_trials()`: Uninstall all active Helm releases and delete benchmark Jobs
- `shutdown()`: Cleanup any remaining releases and Jobs

### 2. Helm Values Generation

**Location**: `auto_tune_vllm/execution/helm_utils.py` (new file)

- Convert `TrialConfig` to Helm values YAML format
- Map vLLM CLI args to Helm chart values structure
- Handle environment variables from `trial_config.environment_vars`
- Generate values for model, resources, ports, etc.
- Support both direct Helm chart values and helmfile-compatible format

**Key Functions**:

- `generate_helm_values(trial_config: TrialConfig, helm_config: dict) -> dict`
- `convert_vllm_args_to_helm_values(vllm_args: List[str]) -> dict`
- `sanitize_release_name(name: str) -> str`

### 3. Configuration Extensions

**Location**: `auto_tune_vllm/core/config.py`

Add optional Helm configuration section to `StudyConfig`:

```python
@dataclass
class HelmConfig:
    chart_path: Optional[str] = None  # Path to Helm chart
    chart_repo: Optional[str] = None  # Helm chart repository
    chart_name: Optional[str] = None  # Chart name from repo
    chart_version: Optional[str] = None  # Chart version
    namespace: str = "default"  # Kubernetes namespace
    kubeconfig: Optional[str] = None  # Path to kubeconfig
    values_template: Optional[str] = None  # Path to values template
    release_name_template: Optional[str] = None  # Custom release name pattern
```

**YAML Config Format**:

```yaml
execution:
  backend: "helm"
  helm:
    chart_repo: "llm-d-modelservice/llm-d-modelservice"
    chart_version: "v0.3.8"
    namespace: "llm-d-trials"
    values_template: "./helm-values-template.yaml"  # Optional
```

### 4. Service Discovery

**Location**: `auto_tune_vllm/execution/helm_utils.py`

- Query Kubernetes Services created by Helm releases
- Extract service URL from Service manifest
- Support ClusterIP, LoadBalancer, and NodePort service types
- Handle service readiness checks

**Key Functions**:

- `get_service_url(release_name: str, namespace: str) -> str`
- `wait_for_service_ready(service_name: str, namespace: str, timeout: int) -> bool`
- `create_benchmark_job(trial_config: TrialConfig, server_url: str, namespace: str) -> str`
- `wait_for_job_completion(job_name: str, namespace: str, timeout: int) -> bool`
- `extract_job_results(job_name: str, namespace: str) -> dict`
- `delete_benchmark_job(job_name: str, namespace: str) -> None`

### 5. Kubernetes Job for Benchmark Execution

**Location**: `auto_tune_vllm/execution/helm_utils.py`

When using Helm backend, benchmarks run as Kubernetes Jobs instead of subprocesses:

- Create Kubernetes Job manifest from `BenchmarkConfig`
- Job runs GuideLLM (or other benchmark) CLI in container
- Results stored in PersistentVolume or ConfigMap
- Monitor Job status (Pending, Running, Succeeded, Failed)
- Extract results from Job logs or mounted volume
- Clean up Jobs after result extraction

**Key Functions**:

- `create_benchmark_job(trial_config: TrialConfig, server_url: str, namespace: str) -> str`
- `wait_for_job_completion(job_name: str, namespace: str, timeout: int) -> bool`
- `extract_job_results(job_name: str, namespace: str) -> dict`
- `delete_benchmark_job(job_name: str, namespace: str) -> None`

**Job Manifest Structure**:

- Container image: GuideLLM-compatible image (or user-specified)
- Command: `guidellm` CLI with trial-specific parameters
- Environment variables: Server URL, benchmark config
- Volume mounts: Results storage (PVC or emptyDir)
- Resource limits: CPU/memory for benchmark workload
- Backoff limit: 0 (no retries, fail fast)

### 6. Trial Controller Integration

**Location**: `auto_tune_vllm/execution/trial_controller.py`

- Create `HelmTrialController` (similar to `LocalTrialController`)
- Handle Helm-deployed vLLM server lifecycle
- Monitor Helm release status instead of process status
- Use Kubernetes Service for health checks
- **Use Kubernetes Jobs for benchmark execution** (instead of subprocess)

**Key Methods**:

- `_start_vllm_server()`: Trigger Helm install via backend
- `_wait_for_server_ready()`: Poll Kubernetes Service health endpoint
- `_start_benchmark()`: Create Kubernetes Job for benchmark (replaces subprocess)
- `_wait_for_benchmark_completion()`: Poll Kubernetes Job status
- `_extract_benchmark_results()`: Read results from Job logs/volume
- `cleanup_resources()`: Request Helm uninstall and Job deletion via backend

### 7. Release Tracking & Reproducibility

**Location**: `auto_tune_vllm/core/trial.py`

- Store Helm release name in `TrialResult.execution_info`
- Add `helm_release_name` field to `ExecutionInfo` dataclass
- Persist release names in Optuna trial user attributes
- Enable querying trials by Helm release name

**Usage**:

- Users can query: "What Helm release was used for trial X?"
- Support resuming studies with existing Helm releases
- Document release names in trial logs

### 8. CLI Integration

**Location**: `auto_tune_vllm/cli/main.py`

- Add `--backend helm` option to `optimize` command
- Add Helm-specific CLI flags:
  - `--helm-chart-path`: Path to local Helm chart
  - `--helm-chart-repo`: Helm chart repository URL
  - `--helm-namespace`: Kubernetes namespace
  - `--kubeconfig`: Path to kubeconfig file
- Validate Helm prerequisites (helm CLI, kubeconfig access)

### 9. Benchmark Provider Abstraction

**Location**: `auto_tune_vllm/benchmarks/providers.py`

Extend `BenchmarkProvider` interface to support Kubernetes Job mode:

- Add abstract method: `create_job_manifest(server_url: str, config: BenchmarkConfig, namespace: str) -> dict`
- `GuideLLMBenchmark` implements Job manifest generation
- Job manifest includes:
  - Container image with GuideLLM CLI
  - Command/args from `_build_guidellm_command()`
  - Environment variables
  - Volume mounts for results
  - Resource requirements

**Alternative Approach** (simpler):

- Keep `BenchmarkProvider` interface unchanged
- `HelmTrialController` generates Job manifest directly from `BenchmarkConfig`
- Reuse existing `_build_guidellm_command()` logic for Job args

### 10. Dependencies

**Location**: `pyproject.toml`, `requirements.txt`

- Add optional dependency group: `helm = ["kubernetes>=28.0.0", "pyyaml>=6.0"]`
- Use `subprocess` for Helm CLI calls (or consider `python-helm` library)
- Kubernetes client library for service discovery and Job management
- Consider adding `kubernetes` to main dependencies if Helm becomes primary backend

## File Changes Summary

### New Files

1. `auto_tune_vllm/execution/helm_utils.py` - Helm utilities, values generation, and Kubernetes Job management
2. `docs/helm_deployment.md` - Documentation for Helm backend usage

### Modified Files

1. `auto_tune_vllm/execution/backends.py` - Add `HelmExecutionBackend`
2. `auto_tune_vllm/execution/trial_controller.py` - Add `HelmTrialController`
3. `auto_tune_vllm/core/config.py` - Add `HelmConfig` dataclass
4. `auto_tune_vllm/core/trial.py` - Add `helm_release_name` to `ExecutionInfo`
5. `auto_tune_vllm/cli/main.py` - Add Helm backend CLI options
6. `pyproject.toml` - Add helm optional dependencies

## Configuration Compatibility

Existing config files will continue to work:

- If `execution.backend` is not specified, defaults to "ray" (current behavior)
- Helm-specific config only required when using Helm backend
- All existing `parameters`, `benchmark`, `optimization` sections work unchanged
- When `execution.backend: helm` is set, benchmarks automatically use Kubernetes Jobs (no config change needed)

## Testing Strategy

1. Unit tests for Helm values generation
2. Unit tests for Kubernetes Job manifest generation
3. Integration tests with mock Helm CLI and Kubernetes API
4. End-to-end tests requiring Kubernetes cluster access (Helm install + Job execution)
5. Validation that existing configs still work with Ray/local backends
6. Test Job result extraction from logs and volumes

## Migration Path

- No migration needed - Helm is additive
- Users opt-in by setting `execution.backend: helm` in config
- Existing studies and data remain unchanged