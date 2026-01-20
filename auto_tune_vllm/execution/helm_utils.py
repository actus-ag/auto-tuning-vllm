"""Helm utilities for vLLM deployment and Kubernetes Job management."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..core.trial import TrialConfig

logger = logging.getLogger(__name__)


def sanitize_release_name(name: str) -> str:
    """Sanitize name for Helm release (lowercase, alphanumeric and hyphens only).
    
    Args:
        name: Original name
        
    Returns:
        Sanitized name suitable for Helm release
    """
    # Convert to lowercase
    name = name.lower()
    # Replace underscores and spaces with hyphens
    name = name.replace("_", "-").replace(" ", "-")
    # Remove invalid characters (keep only alphanumeric and hyphens)
    name = re.sub(r"[^a-z0-9-]", "", name)
    # Remove consecutive hyphens
    name = re.sub(r"-+", "-", name)
    # Remove leading/trailing hyphens
    name = name.strip("-")
    # Ensure it starts with a letter or number
    if name and not name[0].isalnum():
        name = "release-" + name
    # Helm release names must be <= 53 characters
    if len(name) > 53:
        name = name[:53].rstrip("-")
    return name or "release"


def convert_vllm_args_to_helm_values(vllm_args: List[str]) -> Dict[str, Any]:
    """Convert vLLM CLI arguments to Helm values format.
    
    Args:
        vllm_args: List of vLLM CLI arguments (e.g., ["--max-num-seqs", "256"])
        
    Returns:
        Dictionary of Helm values
    """
    values = {}
    i = 0
    while i < len(vllm_args):
        arg = vllm_args[i]
        if arg.startswith("--"):
            # Remove -- prefix
            key = arg[2:]
            # Convert to nested dict format if needed (e.g., max-num-seqs -> maxNumSeqs)
            # For now, keep as-is but convert dashes to camelCase for nested keys
            key_parts = key.split("-")
            if len(key_parts) > 1:
                # Convert to camelCase
                camel_key = key_parts[0] + "".join(p.capitalize() for p in key_parts[1:])
            else:
                camel_key = key
            
            # Check if next arg is a value (not another flag)
            if i + 1 < len(vllm_args) and not vllm_args[i + 1].startswith("--"):
                value = vllm_args[i + 1]
                # Try to convert to appropriate type
                try:
                    if value.lower() in ("true", "false"):
                        values[camel_key] = value.lower() == "true"
                    elif "." in value:
                        values[camel_key] = float(value)
                    else:
                        values[camel_key] = int(value)
                except ValueError:
                    values[camel_key] = value
                i += 2
            else:
                # Boolean flag (present = true)
                values[camel_key] = True
                i += 1
        else:
            i += 1
    
    return values


def generate_gaie_values(
    trial_config: TrialConfig, helm_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Generate GAIE Helm values from TrialConfig.
    
    Args:
        trial_config: Trial configuration
        helm_config: Helm-specific configuration
        
    Returns:
        Dictionary of GAIE Helm values ready for deployment
    """
    from pathlib import Path
    
    # Start with base GAIE values
    gaie_values = {
        "inferencePool": {
            "targetPorts": [{"number": 8000}],
            "modelServerType": "vllm",
            "modelServers": {
                "matchLabels": {
                    "llm-d.ai/role": "decode",
                    "llm-d.ai/inferenceServing": "true"
                }
            }
        },
        "inferenceExtension": {
            "replicas": 1,
            "image": {
                "name": "llm-d-inference-scheduler",
                "hub": "ghcr.io/llm-d",
                "tag": "v0.4.0",
                "pullPolicy": "Always"
            },
            "extProcPort": 9002,
            "extraContainerPorts": [
                {"name": "zmq", "containerPort": 5557, "protocol": "TCP"}
            ],
            "extraServicePorts": [
                {"name": "zmq", "port": 5557, "targetPort": 5557, "protocol": "TCP"}
            ],
            "flags": {
                "kv-cache-usage-percentage-metric": "vllm:kv_cache_usage_perc",
                "v": 4
            },
            "pluginsConfigFile": "precise-prefix-cache-config.yaml",
            "pluginsCustomConfig": {
                "precise-prefix-cache-config.yaml": """apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
  - type: single-profile-handler
  - type: precise-prefix-cache-scorer
    parameters:
      indexerConfig:
        tokenProcessorConfig:
          blockSize: 64
          hashSeed: "42"
        tokenizersPoolConfig:
          hf:
            tokenizersCacheDir: "/tmp/tokenizers"
  - type: kv-cache-utilization-scorer
  - type: queue-scorer
  - type: max-score-picker
schedulingProfiles:
  - name: default
    plugins:
      - pluginRef: precise-prefix-cache-scorer
        weight: 3.0
      - pluginRef: kv-cache-utilization-scorer
        weight: 2.0
      - pluginRef: queue-scorer
        weight: 2.0
      - pluginRef: max-score-picker"""
            }
        },
        "provider": {
            "name": "istio"
        }
    }
    
    # Load GAIE values template if provided and merge (template takes precedence)
    if helm_config.get("gaie_values_template"):
        gaie_values_path = Path(helm_config["gaie_values_template"])
        if gaie_values_path.exists():
            with open(gaie_values_path) as f:
                template_values = yaml.safe_load(f) or {}
                # Deep merge template values into gaie_values
                def deep_merge(base, update):
                    for key, value in update.items():
                        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                            deep_merge(base[key], value)
                        else:
                            base[key] = value
                deep_merge(gaie_values, template_values)
    
    # Apply trial-specific GAIE parameters
    # Common tunable parameters:
    # - gaie_replicas: inferenceExtension.replicas
    # - gaie_precise_prefix_cache_scorer_weight: pluginsCustomConfig schedulingProfiles[0].plugins[0].weight
    # - gaie_kv_cache_utilization_scorer_weight: pluginsCustomConfig schedulingProfiles[0].plugins[1].weight
    # - gaie_queue_scorer_weight: pluginsCustomConfig schedulingProfiles[0].plugins[2].weight
    # - gaie_block_size: pluginsCustomConfig tokenProcessorConfig.blockSize
    # - gaie_hash_seed: pluginsCustomConfig tokenProcessorConfig.hashSeed
    
    if trial_config.gaie_parameters:
        # Update replicas if specified
        if "gaie_replicas" in trial_config.gaie_parameters:
            gaie_values["inferenceExtension"]["replicas"] = int(trial_config.gaie_parameters["gaie_replicas"])
        
        # Update plugin weights if specified
        if any("weight" in key for key in trial_config.gaie_parameters.keys()):
            # Parse the plugins config YAML to update weights
            plugins_config_str = gaie_values["inferenceExtension"]["pluginsCustomConfig"]["precise-prefix-cache-config.yaml"]
            plugins_config = yaml.safe_load(plugins_config_str)
            
            if "schedulingProfiles" in plugins_config and len(plugins_config["schedulingProfiles"]) > 0:
                profile = plugins_config["schedulingProfiles"][0]
                if "plugins" in profile:
                    for plugin in profile["plugins"]:
                        plugin_ref = plugin.get("pluginRef", "")
                        if "gaie_precise_prefix_cache_scorer_weight" in trial_config.gaie_parameters and plugin_ref == "precise-prefix-cache-scorer":
                            plugin["weight"] = float(trial_config.gaie_parameters["gaie_precise_prefix_cache_scorer_weight"])
                        elif "gaie_kv_cache_utilization_scorer_weight" in trial_config.gaie_parameters and plugin_ref == "kv-cache-utilization-scorer":
                            plugin["weight"] = float(trial_config.gaie_parameters["gaie_kv_cache_utilization_scorer_weight"])
                        elif "gaie_queue_scorer_weight" in trial_config.gaie_parameters and plugin_ref == "queue-scorer":
                            plugin["weight"] = float(trial_config.gaie_parameters["gaie_queue_scorer_weight"])
            
            # Update the plugins config string
            gaie_values["inferenceExtension"]["pluginsCustomConfig"]["precise-prefix-cache-config.yaml"] = yaml.dump(plugins_config, default_flow_style=False, allow_unicode=True)
        
        # Update block size and hash seed if specified
        if "gaie_block_size" in trial_config.gaie_parameters or "gaie_hash_seed" in trial_config.gaie_parameters:
            plugins_config_str = gaie_values["inferenceExtension"]["pluginsCustomConfig"]["precise-prefix-cache-config.yaml"]
            plugins_config = yaml.safe_load(plugins_config_str)
            
            # Find the precise-prefix-cache-scorer plugin
            for plugin in plugins_config.get("plugins", []):
                if plugin.get("type") == "precise-prefix-cache-scorer":
                    params = plugin.get("parameters", {})
                    indexer_config = params.get("indexerConfig", {})
                    token_processor_config = indexer_config.get("tokenProcessorConfig", {})
                    
                    if "gaie_block_size" in trial_config.gaie_parameters:
                        token_processor_config["blockSize"] = int(trial_config.gaie_parameters["gaie_block_size"])
                    if "gaie_hash_seed" in trial_config.gaie_parameters:
                        token_processor_config["hashSeed"] = str(trial_config.gaie_parameters["gaie_hash_seed"])
                    
                    break
            
            # Update the plugins config string
            gaie_values["inferenceExtension"]["pluginsCustomConfig"]["precise-prefix-cache-config.yaml"] = yaml.dump(plugins_config, default_flow_style=False, allow_unicode=True)
    
    return gaie_values


def generate_helm_values(
    trial_config: TrialConfig, helm_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Generate Helm values from TrialConfig.
    
    Args:
        trial_config: Trial configuration
        helm_config: Helm-specific configuration (chart path, namespace, etc.)
        
    Returns:
        Dictionary of Helm values ready for deployment
    """
    values = {}
    
    # Start with template if provided
    if helm_config.get("values_template"):
        template_path = Path(helm_config["values_template"])
        if template_path.exists():
            with open(template_path) as f:
                values = yaml.safe_load(f) or {}
        else:
            logger.warning(f"Helm values template not found: {template_path}")
    
    # Require explicit chart type specification
    chart_type = helm_config.get("chart_type")
    if not chart_type:
        raise ValueError(
            "chart_type must be explicitly specified in Helm configuration. "
            "Set chart_type to either 'llm-d-modelservice' or 'vllm'. "
            "Example: helm: { chart_type: 'llm-d-modelservice', ... }"
        )
    
    chart_type_lower = chart_type.lower()
    if chart_type_lower not in ("llm-d-modelservice", "vllm"):
        raise ValueError(
            f"Invalid chart_type: '{chart_type}'. "
            "Must be either 'llm-d-modelservice' or 'vllm'."
        )
    
    is_llm_d_modelservice = chart_type_lower == "llm-d-modelservice"
    
    # Convert vLLM args to Helm values
    vllm_values = convert_vllm_args_to_helm_values(trial_config.vllm_args)
    
    # Merge vLLM values into main values dict
    if "decode" not in values:
        values["decode"] = {}
    if "containers" not in values["decode"]:
        values["decode"]["containers"] = []
    if not values["decode"]["containers"]:
        values["decode"]["containers"].append({})
    
    container = values["decode"]["containers"][0]
    
    # Set container image if not present (required by llm-d-modelservice chart)
    # For prefix-aware routing experiments, use llm-d-cuda image
    # For disaggregation experiments, use llm-d-inference-sim image
    if "image" not in container:
        if is_llm_d_modelservice:
            # Default to llm-d-cuda image for prefix-aware routing experiments
            container["image"] = "ghcr.io/llm-d/llm-d-cuda:v0.3.1"
        else:
            # For vanilla vLLM charts, use vLLM image
            container["image"] = "ghcr.io/vllm-project/vllm-openai:latest"
    
    # Set modelCommand if not present
    if "modelCommand" not in container:
        container["modelCommand"] = "vllmServe" if is_llm_d_modelservice else None
    
    # Check if using custom modelCommand (for prefix-aware routing with KV events)
    is_custom_command = container.get("modelCommand") == "custom"
    
    if is_llm_d_modelservice:
        # For llm-d-modelservice: Chart constructs args from parallelism, modelArtifacts, etc.
        # Only add non-standard vLLM args to container.args (they get appended)
        # Standard args like --model, --tensor-parallel-size are constructed by chart
        
        # Initialize args list for additional parameters
        if "args" not in container:
            container["args"] = []
        
        # Map parallelism parameters from trial config
        # tensor_parallel_size -> decode.parallelism.tensor
        if "tensor_parallel_size" in trial_config.parameters:
            if "parallelism" not in values["decode"]:
                values["decode"]["parallelism"] = {}
            values["decode"]["parallelism"]["tensor"] = trial_config.parameters["tensor_parallel_size"]
        
        # data_parallel_size -> decode.parallelism.data
        if "data_parallel_size" in trial_config.parameters:
            if "parallelism" not in values["decode"]:
                values["decode"]["parallelism"] = {}
            values["decode"]["parallelism"]["data"] = trial_config.parameters["data_parallel_size"]
        
        # data_parallel_size_local -> decode.parallelism.dataLocal
        if "data_parallel_size_local" in trial_config.parameters:
            if "parallelism" not in values["decode"]:
                values["decode"]["parallelism"] = {}
            values["decode"]["parallelism"]["dataLocal"] = trial_config.parameters["data_parallel_size_local"]
        
        if is_custom_command:
            # For custom command: construct shell command string
            # Format: "vllm serve MODEL --host 0.0.0.0 --port 8200 [args...]"
            cmd_parts = ["vllm", "serve"]
            
            # Add model
            if trial_config.benchmark_config and trial_config.benchmark_config.model:
                cmd_parts.append(trial_config.benchmark_config.model)
            
            # Add standard args
            cmd_parts.extend(["--host", "0.0.0.0", "--port", "8200"])
            
            # Add all vLLM args (including standard ones for custom command)
            for arg in trial_config.vllm_args:
                if arg not in cmd_parts:
                    cmd_parts.append(arg)
            
            # Join into single shell command string
            container["args"] = [" \\\n        ".join(cmd_parts)]
        else:
            # For vllmServe/imageDefault: Add only non-standard vLLM args to container.args
            # Standard args (--model, --tensor-parallel-size, --data-parallel-size, etc.) are constructed by chart
            standard_args = {
                "--model", "--tensor-parallel-size", "--data-parallel-size", 
                "--data-parallel-size-local", "--served-model-name", "--port", "--host"
            }
            
            for arg in trial_config.vllm_args:
                # Skip standard args that chart will construct
                if arg in standard_args or any(arg.startswith(f"{std}=") for std in standard_args):
                    continue
                # Add non-standard args (e.g., --max-num-seqs, --gpu-memory-utilization)
                if arg not in container["args"]:
                    container["args"].append(arg)
    else:
        # For vanilla vLLM charts: Add all vLLM args directly to container.args
        if "args" not in container:
            container["args"] = []
        
        # Add all vLLM args
        for arg in trial_config.vllm_args:
            if arg not in container["args"]:
                container["args"].append(arg)
        
        # Ensure model is specified in args if not already present
        if trial_config.benchmark_config and trial_config.benchmark_config.model:
            has_model = any(
                arg.startswith("--model") or arg == trial_config.benchmark_config.model
                for arg in container["args"]
            )
            if not has_model:
                container["args"].extend(["--model", trial_config.benchmark_config.model])
    
    # Add environment variables
    if "env" not in container:
        container["env"] = []
    
    env_dict = {env["name"]: env.get("value", "") for env in container["env"] if "name" in env}
    env_dict.update(trial_config.environment_vars)
    container["env"] = [{"name": k, "value": str(v)} for k, v in env_dict.items()]
    
    # Set model from benchmark config
    if "modelArtifacts" not in values:
        values["modelArtifacts"] = {}
    if trial_config.benchmark_config and trial_config.benchmark_config.model:
        values["modelArtifacts"]["uri"] = f"hf://{trial_config.benchmark_config.model}"
        values["modelArtifacts"]["name"] = trial_config.benchmark_config.model
    
    # Set resource requirements
    if "resources" not in container:
        container["resources"] = {}
    if "limits" not in container["resources"]:
        container["resources"]["limits"] = {}
    if "requests" not in container["resources"]:
        container["resources"]["requests"] = {}
    
    num_gpus = trial_config.resource_requirements.get("num_gpus", 1)
    container["resources"]["limits"]["nvidia.com/gpu"] = str(int(num_gpus))
    container["resources"]["requests"]["nvidia.com/gpu"] = str(int(num_gpus))
    
    # Ensure routing and service configuration for llm-d-modelservice chart
    # The chart creates services through routing.proxy sidecar configuration
    if is_llm_d_modelservice:
        # Configure routing section (required for service creation)
        if "routing" not in values:
            values["routing"] = {}
        # Set servicePort for routing (this is what the service will expose)
        if "servicePort" not in values["routing"]:
            values["routing"]["servicePort"] = 8000
        
        # Configure routing proxy (sidecar that creates the service)
        if "proxy" not in values["routing"]:
            values["routing"]["proxy"] = {}
        # Enable proxy if not explicitly disabled
        if "enabled" not in values["routing"]["proxy"]:
            values["routing"]["proxy"]["enabled"] = True
        # Set targetPort to match vLLM container port (8200 for prefix-aware routing)
        if "targetPort" not in values["routing"]["proxy"]:
            container_port = 8200
            if container.get("ports") and len(container["ports"]) > 0:
                # Find the http port
                for port in container["ports"]:
                    if port.get("name") == "http" or port.get("containerPort") == 8200:
                        container_port = port.get("containerPort", 8200)
                        break
            values["routing"]["proxy"]["targetPort"] = container_port
        # Set proxy image if not present
        if "image" not in values["routing"]["proxy"]:
            values["routing"]["proxy"]["image"] = "ghcr.io/llm-d/llm-d-routing-sidecar:v0.4.0-rc.1"
    
    return values


def get_service_url(release_name: str, namespace: str, helm_config: dict = None) -> str:
    """Get service URL from Helm release.
    
    Args:
        release_name: Helm release name (modelservice release)
        namespace: Kubernetes namespace
        helm_config: Optional Helm configuration to check for full stack deployment
        
    Returns:
        Service URL (e.g., "http://service-name.namespace.svc.cluster.local:8000/v1")
    """
    import json
    import time
    
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        raise RuntimeError("kubernetes library required for Helm backend")
    
    # #region agent log
    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
        f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"A","location":"helm_utils.py:334","message":"Starting get_service_url","data":{"release_name":release_name,"namespace":namespace,"helm_config":str(helm_config)},"timestamp":int(time.time()*1000)})+"\n")
    # #endregion
    
    # For full stack deployment, use inference gateway service (LoadBalancer/NodePort)
    if helm_config and helm_config.get("deploy_full_stack", False):
        # #region agent log
        with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:340","message":"Checking for full stack deployment","data":{"deploy_full_stack":True},"timestamp":int(time.time()*1000)})+"\n")
        # #endregion
        release_name_postfix = helm_config.get("release_name_postfix", "kv-events")
        
        try:
            # #region agent log
            with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:344","message":"Loading kubeconfig for gateway service","data":{},"timestamp":int(time.time()*1000)})+"\n")
            # #endregion
            config.load_incluster_config()
        except config.ConfigException:
            # #region agent log
            with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:348","message":"Loading kubeconfig from file","data":{},"timestamp":int(time.time()*1000)})+"\n")
            # #endregion
            config.load_kube_config()
        
        v1 = client.CoreV1Api()
        
        # Try multiple gateway service name patterns (as per llm-d docs)
        # Pattern 1: infra-{postfix}-inference-gateway-istio (Istio gateway)
        # Pattern 2: infra-{postfix}-inference-gateway (regular gateway)
        gateway_service_names = [
            f"infra-{release_name_postfix}-inference-gateway-istio",
            f"infra-{release_name_postfix}-inference-gateway",
        ]
        
        service = None
        gateway_service_name = None
        for name in gateway_service_names:
            try:
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:371","message":"Trying gateway service name","data":{"service_name":name,"namespace":namespace},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
                service = v1.read_namespaced_service(name=name, namespace=namespace)
                gateway_service_name = name
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:378","message":"Found gateway service","data":{"service_name":gateway_service_name},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
                break
            except ApiException as e:
                if e.status == 404:
                    # Try next pattern
                    continue
                # Other errors should be raised
                raise
        
        # If not found by known patterns, search all services for pattern matching .*-inference-gateway(-.*)?$
        # This matches the approach in llm-d docs: select(.metadata.name | test(".*-inference-gateway(-.*)?$"))
        if service is None:
            # #region agent log
            with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:390","message":"Searching all services for inference-gateway pattern","data":{"namespace":namespace},"timestamp":int(time.time()*1000)})+"\n")
            # #endregion
            import re
            services = v1.list_namespaced_service(namespace=namespace)
            gateway_pattern = re.compile(r".*-inference-gateway(-.*)?$")
            for svc in services.items:
                if gateway_pattern.match(svc.metadata.name):
                    service = svc
                    gateway_service_name = svc.metadata.name
                    # #region agent log
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:400","message":"Found gateway service by pattern match","data":{"service_name":gateway_service_name},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                    break
        
        if service:
            port = 80  # Default HTTP port for gateway
            if service.spec.ports:
                # Find HTTP port (usually 80)
                for svc_port in service.spec.ports:
                    if svc_port.name == "default" or svc_port.port == 80:
                        port = svc_port.port
                        break
            
            # Use LoadBalancer external IP if available, otherwise use ClusterIP
            if service.spec.type == "LoadBalancer" and service.status.load_balancer.ingress:
                ingress = service.status.load_balancer.ingress[0]
                host = ingress.hostname or ingress.ip
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:365","message":"Using gateway LoadBalancer","data":{"service_name":gateway_service_name,"host":host,"port":port},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
                return f"http://{host}:{port}/v1"
            elif service.spec.type == "NodePort" and service.spec.ports:
                node_port = service.spec.ports[0].node_port
                # For NodePort, we'd need node IP - use service DNS instead
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:372","message":"Using gateway NodePort","data":{"service_name":gateway_service_name,"node_port":node_port,"port":port},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
                return f"http://{gateway_service_name}.{namespace}.svc.cluster.local:{port}/v1"
            else:
                # ClusterIP - use ClusterIP directly
                cluster_ip = service.spec.cluster_ip
                if cluster_ip:
                    # #region agent log
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"GAIE","location":"helm_utils.py:380","message":"Using gateway ClusterIP","data":{"service_name":gateway_service_name,"cluster_ip":cluster_ip,"port":port},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                    return f"http://{cluster_ip}:{port}/v1"
                return f"http://{gateway_service_name}.{namespace}.svc.cluster.local:{port}/v1"
        else:
            # No gateway service found with any pattern
            logger.warning(
                f"Gateway service not found in namespace '{namespace}' for patterns: {gateway_service_names}. "
                f"Falling back to modelservice service."
            )
    
    try:
        # Load kubeconfig
        # #region agent log
        with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"A","location":"helm_utils.py:375","message":"Loading kubeconfig for modelservice","data":{},"timestamp":int(time.time()*1000)})+"\n")
        # #endregion
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        
        v1 = client.CoreV1Api()
        
        # Try to find service created by Helm release
        # Helm typically creates services with release name or app name
        # #region agent log
        with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"A","location":"helm_utils.py:386","message":"Listing services in namespace","data":{"namespace":namespace},"timestamp":int(time.time()*1000)})+"\n")
        # #endregion
        services = v1.list_namespaced_service(namespace=namespace)
        
        # #region agent log
        with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"A","location":"helm_utils.py:310","message":"Listed services","data":{"service_count":len(services.items),"service_names":[s.metadata.name for s in services.items]},"timestamp":int(time.time()*1000)})+"\n")
        # #endregion
        
        for service in services.items:
            # Check if service belongs to this release
            labels = service.metadata.labels or {}
            if (
                labels.get("app.kubernetes.io/instance") == release_name
                or labels.get("release") == release_name
                or service.metadata.name.startswith(release_name)
            ):
                # Get service port (default to 8000 for vLLM)
                port = 8000
                if service.spec.ports:
                    port = service.spec.ports[0].port
                
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"A","location":"helm_utils.py:320","message":"Found matching service","data":{"service_name":service.metadata.name,"port":port},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
                
                # Construct URL based on service type
                if service.spec.type == "LoadBalancer" and service.status.load_balancer.ingress:
                    ingress = service.status.load_balancer.ingress[0]
                    host = ingress.hostname or ingress.ip
                    return f"http://{host}:{port}/v1"
                elif service.spec.type == "NodePort" and service.spec.ports:
                    node_port = service.spec.ports[0].node_port
                    # For NodePort, we'd need node IP - use service DNS instead
                    return f"http://{service.metadata.name}.{namespace}.svc.cluster.local:{port}/v1"
                else:
                    # ClusterIP (default)
                    # Use ClusterIP directly instead of DNS name for external access
                    cluster_ip = service.spec.cluster_ip
                    if cluster_ip:
                        return f"http://{cluster_ip}:{port}/v1"
                    # Fallback to DNS name if no ClusterIP (shouldn't happen)
                    return f"http://{service.metadata.name}.{namespace}.svc.cluster.local:{port}/v1"
        
        # Fallback: try to find service created by auto-tune-vllm (shorter name)
        # Check for service with auto-tune-vllm managed-by label
        for service in services.items:
            labels = service.metadata.labels or {}
            if labels.get("app.kubernetes.io/managed-by") == "auto-tune-vllm":
                port = 8000
                if service.spec.ports:
                    port = service.spec.ports[0].port
                # Use ClusterIP directly instead of DNS name for external access
                cluster_ip = service.spec.cluster_ip
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"C","location":"helm_utils.py:352","message":"Found auto-tune-vllm service","data":{"service_name":service.metadata.name,"cluster_ip":cluster_ip,"port":port},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
                if cluster_ip:
                    return f"http://{cluster_ip}:{port}/v1"
                # Fallback to DNS name if no ClusterIP
                return f"http://{service.metadata.name}.{namespace}.svc.cluster.local:{port}/v1"
        
        # Last resort: construct expected service name (may not exist and may be too long)
        service_name = f"{release_name}-llm-d-modelservice-decode"
        # #region agent log
        with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"get-service-url","hypothesisId":"C","location":"helm_utils.py:365","message":"Using fallback service name","data":{"service_name":service_name,"port":8000},"timestamp":int(time.time()*1000)})+"\n")
        # #endregion
        return f"http://{service_name}.{namespace}.svc.cluster.local:8000/v1"  # Use service port 8000, not target port
    
    except ApiException as e:
        logger.error(
            f"Kubernetes API error while getting service URL for release '{release_name}' in namespace '{namespace}': "
            f"status={e.status}, reason={e.reason}, message={e.body if hasattr(e, 'body') else str(e)}"
        )
        raise RuntimeError(
            f"Failed to get service URL for release {release_name} in namespace {namespace}: "
            f"Kubernetes API returned status {e.status} ({e.reason})"
        )


def create_readiness_check_job(
    service_url: str, namespace: str, job_name: str
) -> str:
    """Create a Kubernetes Job to check service readiness from inside the cluster.
    
    Args:
        service_url: Service URL to check (e.g., http://172.30.243.32:80/v1/models)
        namespace: Kubernetes namespace
        job_name: Name for the readiness check job
        
    Returns:
        Job name
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        raise RuntimeError("kubernetes library required for Helm backend")
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()
    
    # Create a readiness check job that retries with logging
    # Use /v1/models endpoint as recommended in llm-d docs
    check_url = service_url
    if not check_url.endswith("/v1/models"):
        # Ensure we're checking the models endpoint
        if "/v1" in check_url:
            check_url = check_url.replace("/v1", "/v1/models")
        else:
            check_url = f"{check_url}/v1/models"
    
    # Check for debug mode environment variable
    debug_mode = os.getenv("AUTO_TUNE_VLLM_DEBUG_READINESS_CHECK", "").lower() in ("1", "true", "yes")
    
    # Create a script that retries with logging
    # In debug mode, run indefinitely; otherwise run for up to 5 minutes (300 seconds) checking every 5 seconds
    if debug_mode:
        # Debug mode: infinite retries, no timeout
        check_script = f"""#!/bin/sh
set -e
INTERVAL=5
ATTEMPT=0
URL="{check_url}"

echo "Starting readiness check for URL: $URL (DEBUG MODE - no timeout)"
echo "Will check every $INTERVAL seconds until service is ready"

while true; do
    ATTEMPT=$((ATTEMPT + 1))
    echo "[Attempt $ATTEMPT] Checking $URL..."
    
    HTTP_CODE=$(curl -f -s -o /dev/null -w '%{{http_code}}' "$URL" || echo "000")
    echo "[Attempt $ATTEMPT] HTTP response code: $HTTP_CODE"
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo "Service is READY! HTTP 200 received."
        echo "READY"
        exit 0
    else
        echo "[Attempt $ATTEMPT] Service not ready yet (HTTP $HTTP_CODE), waiting $INTERVAL seconds..."
        sleep $INTERVAL
    fi
done
"""
        max_attempts = None  # No limit in debug mode
        active_deadline_seconds = None  # No timeout in debug mode
        ttl_seconds = 300  # Still clean up after 5 minutes
    else:
        # Normal mode: 60 attempts with 5 second intervals (5 minutes total)
        check_script = f"""#!/bin/sh
set -e
MAX_ATTEMPTS=60
INTERVAL=5
ATTEMPT=0
URL="{check_url}"

echo "Starting readiness check for URL: $URL"
echo "Will check up to $MAX_ATTEMPTS times with $INTERVAL second intervals"

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    echo "[Attempt $ATTEMPT/$MAX_ATTEMPTS] Checking $URL..."
    
    HTTP_CODE=$(curl -f -s -o /dev/null -w '%{{http_code}}' "$URL" || echo "000")
    echo "[Attempt $ATTEMPT/$MAX_ATTEMPTS] HTTP response code: $HTTP_CODE"
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo "Service is READY! HTTP 200 received."
        echo "READY"
        exit 0
    else
        echo "[Attempt $ATTEMPT/$MAX_ATTEMPTS] Service not ready yet (HTTP $HTTP_CODE), waiting $INTERVAL seconds..."
        sleep $INTERVAL
    fi
done

echo "Service did not become ready after $MAX_ATTEMPTS attempts"
echo "NOT_READY"
exit 1
"""
        max_attempts = 60
        active_deadline_seconds = 300  # Kill job after 5 minutes
        ttl_seconds = 300  # Clean up after 5 minutes
    
    job_manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app": "auto-tune-vllm-readiness-check",
            },
        },
        "spec": {
            "backoffLimit": 0,  # No retries (we handle retries in the script)
            "ttlSecondsAfterFinished": ttl_seconds,
            "template": {
                "metadata": {
                    "labels": {
                        "app": "auto-tune-vllm-readiness-check",
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "readiness-check",
                            "image": "curlimages/curl:latest",  # Lightweight curl image
                            "command": ["/bin/sh", "-c"],
                            "args": [check_script],
                        },
                    ],
                },
            },
        },
    }
    
    # Only add activeDeadlineSeconds if not in debug mode
    if active_deadline_seconds is not None:
        job_manifest["spec"]["activeDeadlineSeconds"] = active_deadline_seconds
    
    if debug_mode:
        logger.info(
            f"DEBUG MODE ENABLED: Readiness check Job '{job_name}' will run "
            f"indefinitely (no timeout). Set AUTO_TUNE_VLLM_DEBUG_READINESS_CHECK=0 "
            f"to disable."
        )
    
    try:
        batch_v1.create_namespaced_job(namespace=namespace, body=job_manifest)
        logger.debug(f"Created readiness check Job: {job_name}")
        return job_name
    except ApiException as e:
        logger.error(
            f"Kubernetes API error while creating readiness check Job '{job_name}' in namespace '{namespace}': "
            f"status={e.status}, reason={e.reason}, message={e.body if hasattr(e, 'body') else str(e)}"
        )
        raise RuntimeError(
            f"Failed to create readiness check Job '{job_name}' in namespace '{namespace}': "
            f"Kubernetes API returned status {e.status} ({e.reason})"
        )


def check_readiness_job_result(job_name: str, namespace: str) -> bool:
    """Check if the readiness check Job succeeded.
    
    Args:
        job_name: Job name
        namespace: Kubernetes namespace
        
    Returns:
        True if job succeeded, False otherwise
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        return False
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()
    core_v1 = client.CoreV1Api()
    
    try:
        job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
        
        if job.status.succeeded:
            # Job succeeded, check logs to confirm
            try:
                # Get pods for this job
                label_selector = f"job-name={job_name}"
                pods = core_v1.list_namespaced_pod(
                    namespace=namespace, label_selector=label_selector
                )
                
                if pods.items:
                    pod = pods.items[0]
                    pod_name = pod.metadata.name
                    logs = core_v1.read_namespaced_pod_log(
                        name=pod_name, namespace=namespace
                    )
                    # #region agent log
                    import json
                    import time
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"readiness-check","hypothesisId":"D","location":"helm_utils.py:685","message":"Readiness check Job logs","data":{"job_name":job_name,"logs":logs[:500]},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                    if "READY" in logs:
                        logger.debug(f"Readiness check Job {job_name} confirmed service is ready")
                        return True
                    elif "NOT_READY" in logs:
                        logger.debug(f"Readiness check Job {job_name} confirmed service is not ready")
                        return False
            except Exception as e:
                logger.debug(f"Error reading readiness check logs: {e}, but job succeeded so assuming ready")
                return True  # Job succeeded, assume ready even if we can't read logs
            
            return True
        elif job.status.failed:
            logger.debug(f"Readiness check Job {job_name} failed")
            return False
        
        # Job still running
        return False
    except ApiException as e:
        if e.status == 404:
            logger.debug(f"Readiness check Job '{job_name}' not found")
            return False
        logger.error(
            f"Kubernetes API error while reading readiness check Job '{job_name}' in namespace '{namespace}': "
            f"status={e.status}, reason={e.reason}"
        )
        return False


def delete_readiness_check_job(job_name: str, namespace: str):
    """Delete the readiness check Job.
    
    Args:
        job_name: Job name
        namespace: Kubernetes namespace
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        return
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()
    
    try:
        batch_v1.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background"
        )
        logger.debug(f"Deleted readiness check Job: {job_name}")
    except ApiException as e:
        if e.status != 404:  # Ignore if already deleted
            logger.debug(f"Error deleting readiness check Job '{job_name}': {e}")


def wait_for_service_ready(
    service_name: str, namespace: str, timeout: int = 300
) -> bool:
    """Wait for Kubernetes service to be ready.
    
    Args:
        service_name: Service name or URL
        namespace: Kubernetes namespace
        timeout: Timeout in seconds
        
    Returns:
        True if service is ready, False if timeout
    """
    import json
    
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
        import requests
    except ImportError:
        logger.error("kubernetes or requests library not available")
        return False
    
    # #region agent log
    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
        f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:360","message":"Starting wait_for_service_ready","data":{"service_name":service_name,"namespace":namespace,"timeout":timeout},"timestamp":int(time.time()*1000)})+"\n")
    # #endregion
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    v1 = client.CoreV1Api()
    start_time = time.time()
    
    # Extract service name or IP from URL if full URL provided
    is_cluster_ip = False
    health_url = None
    service_obj = None
    
    if "://" in service_name:
        # Parse URL
        from urllib.parse import urlparse
        parsed = urlparse(service_name)
        hostname = parsed.hostname
        port = parsed.port or 8000
        
        # Check if it's a ClusterIP (IP address format like 172.30.x.x)
        if hostname and all(c.isdigit() or c == "." for c in hostname) and hostname.count(".") == 3:
            is_cluster_ip = True
            # For ClusterIP, we can't connect from outside cluster
            # Instead, check pod readiness via Kubernetes API
            logger.debug(f"Detected ClusterIP service ({hostname}), will check pod readiness instead of HTTP connection")
            health_url = None  # Will use pod readiness check instead
            # #region agent log
            with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:603","message":"Detected ClusterIP, setting health_url=None","data":{"hostname":hostname,"is_cluster_ip":True},"timestamp":int(time.time()*1000)})+"\n")
            # #endregion
        else:
            # DNS name - try to get service object
            service_name_only = hostname.split(".")[0] if hostname else service_name
            try:
                service_obj = v1.read_namespaced_service(name=service_name_only, namespace=namespace)
                if service_obj.spec.type == "ClusterIP" and service_obj.spec.cluster_ip:
                    # ClusterIP service - check pod readiness instead
                    is_cluster_ip = True
                    logger.debug(f"Service '{service_name_only}' is ClusterIP, will check pod readiness instead of HTTP connection")
                    health_url = None
                    # #region agent log
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:613","message":"Service is ClusterIP, setting health_url=None","data":{"service_name":service_name_only,"is_cluster_ip":True},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                else:
                    # LoadBalancer or NodePort - can connect directly
                    health_url = f"http://{hostname}:{port}/v1/models"
            except ApiException as e:
                logger.debug(
                    f"Kubernetes API error while reading service '{service_name_only}' in namespace '{namespace}': "
                    f"status={e.status}, reason={e.reason}. Using DNS name fallback."
                )
                health_url = f"http://{hostname}:{port}/v1/models"
            except Exception as e:
                logger.debug(f"Error reading service '{service_name_only}': {e}. Using DNS name fallback.")
                health_url = f"http://{hostname}:{port}/v1/models"
    else:
        # Service name only - get service object
        try:
            service_obj = v1.read_namespaced_service(name=service_name, namespace=namespace)
            port = 8000
            if service_obj.spec.ports:
                port = service_obj.spec.ports[0].port
            if service_obj.spec.type == "ClusterIP" and service_obj.spec.cluster_ip:
                # ClusterIP service - check pod readiness instead
                is_cluster_ip = True
                logger.debug(f"Service '{service_name}' is ClusterIP, will check pod readiness instead of HTTP connection")
                health_url = None
                # #region agent log
                with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:641","message":"Service is ClusterIP, setting health_url=None","data":{"service_name":service_name,"is_cluster_ip":True},"timestamp":int(time.time()*1000)})+"\n")
                # #endregion
            else:
                # LoadBalancer or NodePort - can connect directly
                health_url = f"http://{service_name}.{namespace}.svc.cluster.local:{port}/v1/models"
        except ApiException as e:
            logger.debug(
                f"Kubernetes API error while reading service '{service_name}' in namespace '{namespace}': "
                f"status={e.status}, reason={e.reason}. Using DNS name fallback."
            )
            # Fallback to DNS name
            health_url = f"http://{service_name}.{namespace}.svc.cluster.local:8000/v1/models"
        except Exception as e:
            logger.debug(f"Error reading service '{service_name}': {e}. Using DNS name fallback.")
            # Fallback to DNS name
            health_url = f"http://{service_name}.{namespace}.svc.cluster.local:8000/v1/models"
    
    # #region agent log
    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
        f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:390","message":"Determined health URL","data":{"health_url":health_url,"service_name":service_name,"is_cluster_ip":is_cluster_ip},"timestamp":int(time.time()*1000)})+"\n")
    # #endregion
    
    # Extract service name for verification
    service_name_for_check = None
    if "://" in service_name:
        from urllib.parse import urlparse
        parsed = urlparse(service_name)
        hostname = parsed.hostname
        # Extract service name if it's a DNS name, not an IP
        if hostname and not (all(c.isdigit() or c == "." for c in hostname) and hostname.count(".") == 3):
            service_name_for_check = hostname.split(".")[0] if hostname else None
        elif is_cluster_ip and service_obj:
            # For ClusterIP, use the service object we already have
            service_name_for_check = service_obj.metadata.name
    else:
        service_name_for_check = service_name
    
    # If we have a service object but no service_name_for_check, use it
    if service_obj and not service_name_for_check:
        service_name_for_check = service_obj.metadata.name
    
    # For ClusterIP services, create a readiness check Job once and poll it
    readiness_check_job_name = None
    if is_cluster_ip and health_url is None:
        import uuid
        readiness_check_job_name = f"readiness-check-{uuid.uuid4().hex[:8]}"
        # Kubernetes names must be <= 63 characters
        if len(readiness_check_job_name) > 63:
            readiness_check_job_name = readiness_check_job_name[:63].rstrip("-")
        
        # #region agent log
        with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:686","message":"Creating readiness check Job for ClusterIP service","data":{"service_url":service_name,"job_name":readiness_check_job_name},"timestamp":int(time.time()*1000)})+"\n")
        # #endregion
        
        try:
            create_readiness_check_job(service_name, namespace, readiness_check_job_name)
        except Exception as e:
            logger.error(f"Failed to create readiness check Job: {e}")
            readiness_check_job_name = None
    
    while time.time() - start_time < timeout:
        try:
            # For ClusterIP services, check readiness via the Kubernetes Job we created
            if readiness_check_job_name:
                if check_readiness_job_result(readiness_check_job_name, namespace):
                    # Service is ready!
                    logger.debug(f"Service ready: readiness check Job {readiness_check_job_name} succeeded")
                    # #region agent log
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:700","message":"Service ready via readiness check Job","data":{"job_name":readiness_check_job_name},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                    delete_readiness_check_job(readiness_check_job_name, namespace)
                    return True
            
            # For non-ClusterIP services or if pod check failed, try HTTP connection
            if health_url:
                try:
                    response = requests.get(health_url, timeout=5)
                    # #region agent log
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:740","message":"Readiness check response","data":{"status_code":response.status_code,"url":health_url},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                    # /v1/models returns 200 when server is ready
                    if response.status_code == 200:
                        # Verify it's a valid models response (should have JSON with "data" or "object" field)
                        try:
                            data = response.json()
                            if "data" in data or "object" in data:
                                logger.debug(f"Service ready: /v1/models returned valid response")
                                return True
                        except (ValueError, KeyError):
                            # If not JSON or unexpected format, still consider 200 as ready
                            logger.debug(f"Service ready: /v1/models returned 200 (non-JSON response)")
                            return True
                except Exception as e:
                    # #region agent log
                    with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                        f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:754","message":"Readiness check failed","data":{"error":str(e),"url":health_url},"timestamp":int(time.time()*1000)})+"\n")
                    # #endregion
                    pass
        except Exception as e:
            # #region agent log
            with open("/home/thibrahi/workspace/auto-tune/llm-d-integration/.cursor/debug.log", "a") as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"wait-service-ready","hypothesisId":"D","location":"helm_utils.py:761","message":"Exception in wait loop","data":{"error":str(e)},"timestamp":int(time.time()*1000)})+"\n")
            # #endregion
            pass
        
        time.sleep(2)
    
    # Cleanup readiness check job if it still exists
    if readiness_check_job_name:
        delete_readiness_check_job(readiness_check_job_name, namespace)
    
    return False


def _build_benchmark_env(trial_config: TrialConfig, benchmark_type: str) -> List[Dict[str, str]]:
    """Build environment variables for benchmark job based on benchmark type."""
    env = []
    if benchmark_type == "guidellm":
        env.append({
            "name": "GUIDELLM__LOGGING__CONSOLE_LOG_LEVEL",
            "value": trial_config.benchmark_config.logging_level
        })
    # MLPerf doesn't require specific environment variables for now
    return env


def create_benchmark_job(
    trial_config: TrialConfig, server_url: str, namespace: str, benchmark_image: Optional[str] = None
) -> str:
    """Create Kubernetes Job for benchmark execution.
    
    Args:
        trial_config: Trial configuration
        server_url: vLLM server URL
        namespace: Kubernetes namespace
        
    Returns:
        Job name
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        raise RuntimeError("kubernetes library required for Helm backend")
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()
    
    # Generate job name
    job_name = sanitize_release_name(f"{trial_config.study_name}-{trial_config.trial_id}-benchmark")
    # Kubernetes names must be <= 63 characters
    if len(job_name) > 63:
        job_name = job_name[:63].rstrip("-")
    
    # Create appropriate benchmark provider based on benchmark type
    from ..benchmarks.providers import GuideLLMBenchmark, MLPerfBenchmark
    
    benchmark_type = trial_config.benchmark_config.benchmark_type
    if benchmark_type == "guidellm":
        benchmark_provider = GuideLLMBenchmark()
        default_image = "ghcr.io/vllm-project/guidellm:v0.5.1"
        working_dir: Optional[str] = None
    elif benchmark_type == "mlperf":
        benchmark_provider = MLPerfBenchmark()
        default_image = "quay.io/rh-ee-nmiriyal/mlperf-6.0:harness"
        working_dir = "mlperf-inference-6.0-redhat/harness"
    else:
        raise ValueError(
            f"Unsupported benchmark type for Helm execution: {benchmark_type}"
        )
    
    benchmark_provider.set_trial_context(trial_config.study_name, trial_config.trial_id)
    results_file = "/tmp/benchmark-results.json"
    
    # Build command args based on benchmark type
    if benchmark_type == "guidellm":
        cmd = benchmark_provider._build_guidellm_command(
            server_url, trial_config.benchmark_config, results_file
        )
    elif benchmark_type == "mlperf":
        cmd = benchmark_provider._build_mlperf_command(
            server_url, trial_config.benchmark_config, results_file
        )
    else:
        raise ValueError(f"Unsupported benchmark type: {benchmark_type}")
    
    # Create Job manifest
    job_manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app": "auto-tune-vllm-benchmark",
                "study-name": trial_config.study_name,
                "trial-id": trial_config.trial_id,
                "benchmark-type": benchmark_type,
            },
        },
        "spec": {
            "backoffLimit": 0,  # No retries
            "ttlSecondsAfterFinished": 300,  # Clean up after 5 minutes
            "template": {
                "metadata": {
                    "labels": {
                        "app": "auto-tune-vllm-benchmark",
                        "study-name": trial_config.study_name,
                        "trial-id": trial_config.trial_id,
                        "benchmark-type": benchmark_type,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "benchmark",
                            "image": benchmark_image or default_image,
                            **({"workingDir": working_dir} if working_dir else {}),
                            "command": [cmd[0]] if cmd else (["python", "harness_main.py"] if benchmark_type == "mlperf" else ["guidellm"]),
                            "args": cmd[1:] if len(cmd) > 1 else [],
                            "env": _build_benchmark_env(trial_config, benchmark_type),
                            "volumeMounts": [
                                {
                                    "name": "results",
                                    "mountPath": "/tmp",
                                },
                            ],
                        },
                    ],
                    "volumes": [
                        {
                            "name": "results",
                            "emptyDir": {},
                        },
                    ],
                },
            },
        },
    }
    
    try:
        # Create Job
        batch_v1.create_namespaced_job(
            namespace=namespace, body=job_manifest
        )
        logger.info(f"Created benchmark Job: {job_name}")
        return job_name
    except ApiException as e:
        logger.error(
            f"Kubernetes API error while creating benchmark Job '{job_name}' in namespace '{namespace}': "
            f"status={e.status}, reason={e.reason}, message={e.body if hasattr(e, 'body') else str(e)}"
        )
        raise RuntimeError(
            f"Failed to create benchmark Job '{job_name}' in namespace '{namespace}': "
            f"Kubernetes API returned status {e.status} ({e.reason})"
        )


def wait_for_job_completion(job_name: str, namespace: str, timeout: int = 3600) -> bool:
    """Wait for Kubernetes Job to complete.
    
    Args:
        job_name: Job name
        namespace: Kubernetes namespace
        timeout: Timeout in seconds
        
    Returns:
        True if job completed successfully, False if failed or timeout
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        return False
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
            
            if job.status.succeeded:
                logger.info(f"Job {job_name} completed successfully")
                return True
            elif job.status.failed:
                logger.error(f"Job {job_name} failed")
                return False
            
            # Job still running
            time.sleep(5)
        except ApiException as e:
            if e.status == 404:
                logger.warning(
                    f"Kubernetes API error while reading Job '{job_name}' in namespace '{namespace}': "
                    f"Job not found (status={e.status}, reason={e.reason})"
                )
                return False
            logger.error(
                f"Kubernetes API error while reading Job '{job_name}' in namespace '{namespace}': "
                f"status={e.status}, reason={e.reason}, message={e.body if hasattr(e, 'body') else str(e)}"
            )
            raise
    
    logger.warning(f"Job {job_name} timed out after {timeout}s")
    return False


def extract_job_results(job_name: str, namespace: str) -> Dict[str, Any]:
    """Extract benchmark results from Kubernetes Job.
    
    Args:
        job_name: Job name
        namespace: Kubernetes namespace
        
    Returns:
        Dictionary of benchmark results
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        raise RuntimeError("kubernetes library required for Helm backend")
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    core_v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()
    
    try:
        # Get Job to find pods and determine benchmark type
        job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
        
        # Determine benchmark type from job labels
        benchmark_type = job.metadata.labels.get("benchmark-type", "guidellm")
        
        # Get pods for this job
        label_selector = f"job-name={job_name}"
        pods = core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        )
        
        if not pods.items:
            raise RuntimeError(f"No pods found for Job {job_name}")
        
        # Get logs from the first pod
        pod = pods.items[0]
        pod_name = pod.metadata.name
        
        # Create appropriate benchmark provider
        from ..benchmarks.providers import GuideLLMBenchmark, MLPerfBenchmark
        
        if benchmark_type == "guidellm":
            benchmark_provider = GuideLLMBenchmark()
        elif benchmark_type == "mlperf":
            benchmark_provider = MLPerfBenchmark()
        else:
            logger.warning(f"Unknown benchmark type {benchmark_type}, defaulting to GuideLLM")
            benchmark_provider = GuideLLMBenchmark()
        
        # Try to extract results file from pod volume
        # Benchmarks write results to /tmp/benchmark-results.json
        # We need to copy it from the pod or read from logs
        
        # First, try to exec into pod and read the results file
        # Note: Pod must still be running for exec to work
        try:
            from kubernetes.stream import stream
            exec_command = ["cat", "/tmp/benchmark-results.json"]
            resp = stream(
                core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                command=exec_command,
                container="benchmark",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
            )
            
            if resp:
                try:
                    results_data = json.loads(resp)
                    # Create temp file for parsing
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                        json.dump(results_data, f)
                        temp_file = f.name
                    try:
                        benchmark_provider._results_file = temp_file
                        results = benchmark_provider.parse_results()
                        return results
                    finally:
                        os.unlink(temp_file)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse results from pod exec: {e}")
        except Exception as e:
            logger.debug(f"Failed to exec into pod to read results file (pod may have terminated): {e}")
        
        # Fallback: try to extract JSON from logs
        logs = core_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, container="benchmark"
        )
        
        # Try to find JSON results in logs (benchmarks may output JSON)
        lines = logs.split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    results_data = json.loads(line)
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                        json.dump(results_data, f)
                        temp_file = f.name
                    try:
                        benchmark_provider._results_file = temp_file
                        results = benchmark_provider.parse_results()
                        return results
                    finally:
                        os.unlink(temp_file)
                except (json.JSONDecodeError, KeyError):
                    continue
        
        # If we get here, we couldn't extract results
        raise RuntimeError(
            f"Could not extract benchmark results from Job {job_name}. "
            f"Results file not found in pod or logs."
        )
    
    except ApiException as e:
        operation = "reading Job" if "read_namespaced_job" in str(e) else "listing pods" if "list_namespaced_pod" in str(e) else "reading pod logs" if "read_namespaced_pod_log" in str(e) else "Kubernetes API operation"
        logger.error(
            f"Kubernetes API error while {operation} for Job '{job_name}' in namespace '{namespace}': "
            f"status={e.status}, reason={e.reason}, message={e.body if hasattr(e, 'body') else str(e)}"
        )
        raise RuntimeError(
            f"Failed to extract results from Job '{job_name}' in namespace '{namespace}': "
            f"Kubernetes API returned status {e.status} ({e.reason}) during {operation}"
        )


def delete_benchmark_job(job_name: str, namespace: str) -> None:
    """Delete Kubernetes Job.
    
    Args:
        job_name: Job name
        namespace: Kubernetes namespace
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException
    except ImportError:
        logger.error("kubernetes library not available")
        return
    
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()
    
    try:
        batch_v1.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background",
        )
        logger.info(f"Deleted benchmark Job: {job_name}")
    except ApiException as e:
        if e.status == 404:
            logger.debug(
                f"Kubernetes API: Job '{job_name}' in namespace '{namespace}' not found (already deleted)"
            )
        else:
            logger.warning(
                f"Kubernetes API error while deleting Job '{job_name}' in namespace '{namespace}': "
                f"status={e.status}, reason={e.reason}, message={e.body if hasattr(e, 'body') else str(e)}"
            )
