## Quick Start Guide

The steps below help you set up the environment, validate your configuration, and launch an optimization study.

### Prerequisites

- Python 3.12 recommended
- NVIDIA GPU with recent drivers
- Internet access for pulling Python wheels and models

### 1) Installation

#### Option A: Native Install

```bash
# Clone the fork
git clone https://github.com/actus-ag/auto-tuning-vllm.git
cd auto-tuning-vllm

# Create virtual environment (uv recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv venv --python 3.12 venv
source venv/bin/activate

# Install
uv pip install -e .
# OR: pip install -e .
```

Verify the CLI is on PATH:

```bash
auto-tune-vllm --help
```

#### Option B: Docker Compose (Recommended for Production) {#docker-compose}

No host installation needed. Create a `docker-compose.yml`:

```yaml
services:
  auto-tune:
    image: vllm/vllm-openai:latest
    command: >
      bash -c "
        pip install git+https://github.com/actus-ag/auto-tuning-vllm.git &&
        pip install guidellm &&
        auto-tune-vllm optimize
          --config /workspace/study_config.yaml
          --max-concurrent-trials 1
      "
    volumes:
      - ./study_config.yaml:/workspace/study_config.yaml   # Your study config
      - ./results:/workspace/optuna_studies                 # Persist results
      - ./logs:/tmp/auto-tune-vllm/logs                    # Persist logs
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1              # Number of GPUs to expose
              capabilities: [gpu]
    ipc: host                       # Required for PyTorch shared memory
    shm_size: '16gb'                # Sufficient shared memory for large models
```

Run it:

```bash
docker compose up
```

**Key points about Docker execution:**
- The container needs GPU access (`deploy.resources.reservations.devices`)
- `ipc: host` and `shm_size` are required for PyTorch/vLLM shared memory
- auto-tune-vllm manages the full vLLM lifecycle inside the container — it starts/stops vLLM as a subprocess for each trial, not via Docker
- Results persist to `./results/` via the volume mount
- To pin specific GPUs (e.g., leave GPU 1 free for other tasks), add to your study config:
  ```yaml
  static_environment_variables:
    CUDA_VISIBLE_DEVICES: "0"
  ```

**Multi-GPU Docker example** (use both GPUs for tensor parallelism):

```yaml
services:
  auto-tune:
    image: vllm/vllm-openai:latest
    command: >
      bash -c "
        pip install git+https://github.com/actus-ag/auto-tuning-vllm.git &&
        pip install guidellm &&
        auto-tune-vllm optimize
          --config /workspace/study_config.yaml
          --max-concurrent-trials 1
      "
    volumes:
      - ./study_config.yaml:/workspace/study_config.yaml
      - ./results:/workspace/optuna_studies
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 2
              capabilities: [gpu]
    ipc: host
    shm_size: '16gb'
```

With a study config that includes:

```yaml
static_parameters:
  tensor_parallel_size: 2   # Use both GPUs for model parallelism
```

### 2) Configure the Study

Start from [`examples/study_config_local_exec.yaml`](../examples/study_config_local_exec.yaml) for a full example.

Key configuration areas:
- Set/confirm the study name and model ([Study Configuration](configuration.md#study-configuration))
- Choose the optimization objective(s) (e.g., throughput) ([Optimization Configuration](configuration.md#optimization-configuration))
- Adjust parameter ranges for tunables you want to explore ([Parameter Configuration](configuration.md#parameter-configuration))

Here's what a basic configuration looks like:

```yaml
# Basic study configuration
study:
  # name: "my_optimization_study"  # Optional: auto-generates if omitted

optimization:
  preset: "high_throughput"  # Use preset for common scenarios
  n_trials: 200             # Number of optimization trials

benchmark:
  benchmark_type: "guidellm"
  model: "RedHatAI/Qwen3-1.7B-FP8-dynamic"
  max_seconds: 240          # Benchmark duration per trial
  rate: 30                  # Request rate (req/sec)
  prompt_tokens: 2000       # Input length
  output_tokens: 2000       # Output length

logging:
  file_path: "/tmp/auto-tune-vllm/logs"
  log_level: "INFO"

parameters:
  gpu_memory_utilization:
    enabled: true
    min: 0.9
    max: 0.95
  kv_cache_dtype:
    enabled: true
    options: ["auto", "fp8"]
```

#### Using Optimization Presets (Recommended)

**Use presets for common optimization scenarios** - they're pre-configured for best results:

```yaml
optimization:
  preset: "high_throughput"  # Maximize token generation rate
  n_trials: 100
```

**Available presets:**
- `"high_throughput"`: Maximize output tokens per second
- `"low_latency"`: Minimize request latency (95th percentile)
- `"balanced"`: Multi-objective optimization (throughput vs latency)

#### What Gets Optimized

The optimizer will tune parameters you mark as `enabled: true` in your config. Parameters you don't specify use vLLM defaults.

> 💡 **Tip**: Start simple with 2-3 key parameters, then expand based on results. See [Parameter Configuration](configuration.md#parameter-configuration) for all available parameters.

### 3) Validate Configuration

Always validate before launching optimization:

```bash
auto-tune-vllm validate --config examples/study_config_local_exec.yaml
```

### 4) Run Optimization

#### Local Backend (Default)

No Ray, no extra flags — just run:

```bash
auto-tune-vllm optimize \
  --config examples/study_config_local_exec.yaml \
  --max-concurrent-trials 1
```

For multiple GPUs running independent trials:

```bash
auto-tune-vllm optimize \
  --config examples/study_config_local_exec.yaml \
  --max-concurrent-trials 2
```

#### Ray Backend (Multi-Node)

For distributed optimization across a cluster, use `--backend ray`. Ray workers must run in a Python environment you control:

```bash
# With an existing Ray cluster
auto-tune-vllm optimize \
  --config examples/study_config_local_exec.yaml \
  --backend ray \
  --venv-path "$(pwd)/venv" \
  --max-concurrent-trials 4

# Auto-start a local Ray head (single machine)
auto-tune-vllm optimize \
  --config examples/study_config_local_exec.yaml \
  --backend ray \
  --venv-path "$(pwd)/venv" \
  --max-concurrent-trials 2 \
  --start-ray-head
```

### 5) Monitor Logs

Open a separate terminal. After optimization starts, the CLI prints an exact logs command. For file-based logging:

```bash
auto-tune-vllm logs --study-name <your_study_name> --log-path ./logs
```

If you configured PostgreSQL logging:

```bash
auto-tune-vllm logs --study-name <your_study_name> --database-url postgresql://user:pass@host:5432/db
```

> See [Logging Configuration](configuration.md#logging-configuration) for detailed logging options.

### 6) View Results in Optuna Dashboard

Use the Optuna Dashboard Web UI (no local install needed):

1. Open the dashboard in your browser: https://optuna.github.io/optuna-dashboard/#/
2. After your study finishes, locate the SQLite file:
   - `optuna_studies/<study_name>/study.db`
3. Drag-and-drop the `study.db` file into the dashboard page.

You can now explore optimization history, parameter importance, and parallel coordinates.

### Troubleshooting

- **Error: "At least one Python environment option must be specified"**
  - This only applies to `--backend ray`. Either provide `--venv-path`, or use `--backend local` (the default).

- **Ray is already running on this port**
  - A cluster is active. Either connect without `--start-ray-head`, or stop it:
    ```bash
    ray stop --force
    ```

- **Docker: vLLM OOM during model loading**
  - Increase `shm_size` in your compose file
  - Lower `gpu_memory_utilization` range in your study config
  - Use a quantized model (FP8/INT4)

- **Docker: Results not persisted**
  - Ensure `./results:/workspace/optuna_studies` volume is mounted
  - Check that the `study.storage_file` path in your config is under `/workspace/optuna_studies`

- **Important**: Always add `--max-concurrent-trials <count>` or set `max_concurrent_trials: <count>` in your YAML config.

### Next Steps

- Explore advanced configuration options: [Configuration Guide](configuration.md)
- For multi-node: [Ray Cluster Setup](ray_cluster_setup.md)
- Inspect Ray cluster resources: `auto-tune-vllm check-env --ray-cluster`

Project home: [actus-ag/auto-tuning-vllm (GitHub)](https://github.com/actus-ag/auto-tuning-vllm)
Upstream: [openshift-psap/auto-tuning-vllm (GitHub)](https://github.com/openshift-psap/auto-tuning-vllm)
