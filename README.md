# Auto-Tune vLLM

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A hyperparameter optimization framework for vLLM serving, built with Optuna. Automatically finds the best vLLM parameters (memory utilization, KV-cache dtype, batch sizes, scheduling, and more) for your model and hardware.

> **Fork note:** This is the [actus-ag fork](https://github.com/actus-ag/auto-tuning-vllm) of [openshift-psap/auto-tuning-vllm](https://github.com/openshift-psap/auto-tuning-vllm), adding local execution backend support and Docker Compose instructions. The local backend is the default — no Ray dependency required for single-machine optimization.

## Features

- 🖥️ **Local Execution**: Run on a single machine with no Ray overhead (default)
- 🚀 **Distributed Optimization**: Scale across multiple GPUs and nodes using Ray (optional)
- 🐳 **Docker Ready**: Run inside containers alongside vLLM
- 🎯 **Flexible Backends**: `--backend local` (default) or `--backend ray`
- 📊 **Rich Benchmarking**: Built-in GuideLLM support + custom benchmark providers
- ⚙️ **Easy Configuration**: YAML-based study and parameter configuration
- 📈 **Multi-Objective**: Support for throughput vs latency trade-offs
- 🔧 **Extensible**: Plugin system for custom benchmarks

## Quick Start (5 minutes)

For a detailed guide, see the [Quick Start Guide](docs/quick_start.md).

### Installation

```bash
git clone https://github.com/actus-ag/auto-tuning-vllm.git
cd auto-tuning-vllm
pip install -e .
```

### Basic Usage

```bash
# Run optimization (local backend, no Ray needed)
auto-tune-vllm optimize --config config.yaml --max-concurrent-trials 1

# Stream live logs
auto-tune-vllm logs --study-name my_study --log-path ./logs

# Resume interrupted study
auto-tune-vllm resume --config config.yaml --max-concurrent-trials 1
```

### Docker Compose

Run the optimization inside a container alongside vLLM — no host installation needed:

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
              count: 1
              capabilities: [gpu]
    ipc: host
    shm_size: '16gb'
```

See the [Quick Start Guide](docs/quick_start.md#docker-compose) for detailed Docker instructions.

## Documentation

- [Quick Start Guide](docs/quick_start.md) - **Start here**
- [Configuration Reference](docs/configuration.md)
- [Ray Cluster Setup](docs/ray_cluster_setup.md) - For distributed multi-node optimization

## Requirements

### Local Backend (default)
- Python 3.10+
- NVIDIA GPU with CUDA support
- vLLM, GuideLLM, Optuna (installed automatically)

### Ray Backend (optional, for multi-node)
- All of the above, plus Ray (`pip install ray[default]`)
- PostgreSQL database (recommended for distributed storage)

## Execution Backends

| Backend | Command | Use Case |
|---------|---------|----------|
| **Local** (default) | `--backend local` | Single machine, 1-2 GPUs, containers |
| **Ray** | `--backend ray --venv-path ./venv` | Multi-node clusters, many GPUs |

## Known Issues

### Ray Cluster Concurrency Validation

**Issue**: When using `--backend ray`, the `--max-concurrent-trials` parameter is not validated against available Ray cluster resources.

**Workaround**:
- Use `auto-tune-vllm check-env --ray-cluster` to inspect available resources
- Set concurrency based on available GPUs (typically 1 GPU per trial)

## License

Apache License 2.0 - see [LICENSE](LICENSE) file for details.

## Upstream

This fork is based on [openshift-psap/auto-tuning-vllm](https://github.com/openshift-psap/auto-tuning-vllm).
