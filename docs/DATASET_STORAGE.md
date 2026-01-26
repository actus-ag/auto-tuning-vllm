# Dataset Storage Approaches for MLPerf Benchmarking

This document describes two approaches for providing datasets to MLPerf benchmark pods.

## Dataset Download

MLPerf v4 datasets are **not included in this repository** due to file size limitations (>500MB total).

### Download Instructions:

1. **From MLPerf GitHub:**
   ```bash
   git clone https://github.com/openshift-psap/mlperf-inference-6.0-redhat.git
   cd mlperf-inference-6.0-redhat/harness/data/v4
   ```

2. **Or download directly:**
   - Performance evaluation: `perf_eval_ref.parquet` (183MB)
   - Accuracy evaluation: `acc_eval_ref.parquet` (184MB)
   - Input token IDs and lengths: `.npy` files (375MB + 26KB)

3. **Place datasets in:**
   - For Docker image approach: `mlperf-build/mlperf-datasets/v4/`
   - For PVC approach: Upload to `/mnt/models/datasets/v4/` on PVC

## Approach 1: Datasets in Docker Image (Current)

### Pros
- ✅ Self-contained - everything in one image
- ✅ No additional PVC setup required
- ✅ Datasets always available with image

### Cons
- ❌ Large image size (2.66GB with 584MB datasets)
- ❌ Must rebuild image when datasets change
- ❌ Slower image pulls
- ❌ Duplicate storage across image versions

### Implementation

**Dockerfile:**
```dockerfile
# Copy dataset files from build context
COPY mlperf-datasets/v4/* /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4/

# Make dataset directory writable
RUN chmod -R 777 /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4
```

**Study Config:**
```yaml
benchmark:
  dataset_path: "/vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4/perf_eval_ref.parquet"
```

**Current Image:**
- `quay.io/rh-ee-aansharm/mlperf-6.0@sha256:89c3c57281c4bab40b8b901316f27eee5d16b2b3db824508260e0494ba21d0fc`

---

## Approach 2: Datasets on PVC (Recommended for Production)

### Pros
- ✅ Smaller Docker images (~2.1GB vs 2.66GB)
- ✅ Faster image builds/pulls
- ✅ Update datasets without rebuilding images
- ✅ Share datasets across multiple studies
- ✅ Easier to manage large datasets

### Cons
- ❌ Requires one-time PVC setup
- ❌ Additional storage resource needed (though model PVC can be reused)

### Implementation

#### Step 1: Upload Datasets to PVC

Use the provided script:
```bash
./scripts/upload-datasets-to-pvc.sh
```

Or manually:
```bash
# Create uploader pod
kubectl run dataset-uploader --image=busybox -n autotune-aanya \
  --overrides='...' # See script for full command

# Copy datasets
kubectl cp mlperf-build/mlperf-datasets/v4 \
  autotune-aanya/dataset-uploader:/mnt/models/datasets/v4

# Verify
kubectl exec -n autotune-aanya dataset-uploader -- \
  ls -lah /mnt/models/datasets/v4/

# Cleanup
kubectl delete pod dataset-uploader -n autotune-aanya
```

#### Step 2: Update Study Config

```yaml
execution:
  backend: "k8s"
  k8s:
    model_pvc: "model-cache-pvc"  # Ensure PVC is mounted

benchmark:
  dataset_path: "/mnt/models/datasets/v4/perf_eval_ref.parquet"  # PVC path
```

#### Step 3: Simplify Dockerfile (Optional)

Remove dataset-related commands to reduce image size:
```dockerfile
# Remove these lines:
# RUN mkdir -p /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4
# COPY mlperf-datasets/v4/* /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4/
# RUN chmod -R 777 /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4
```

Then rebuild with a new tag:
```bash
cd /Users/aansharm/mlperf-build
docker buildx build --platform linux/amd64 -t quay.io/rh-ee-aansharm/mlperf-6.0:pvc-datasets .
docker push quay.io/rh-ee-aansharm/mlperf-6.0:pvc-datasets
```

---

## Dataset Files

MLPerf v4 dataset consists of:
- `perf_eval_ref.parquet` - 184MB (main dataset with tokenized prompts)
- `input_ids_padded_perf_eval.npy` - 375MB (padded input token IDs)
- `input_lens_perf_eval.npy` - 26KB (input lengths)

**Total:** ~584MB

---

## Troubleshooting

### Dataset Not Found Error
```
FileNotFoundError: Dataset file not found: /path/to/dataset
```

**Solution for Approach 1:**
- Verify dataset was copied during Docker build: `docker run --rm --entrypoint ls <image> -lah /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4/`
- Rebuild image if missing

**Solution for Approach 2:**
- Verify dataset exists on PVC: Create debug pod and check `/mnt/models/datasets/v4/`
- Re-run upload script if missing
- Ensure `model_pvc` is configured in study config

### Permission Denied
```
PermissionError: [Errno 13] Permission denied
```

**Solution for Approach 1:**
- Ensure Dockerfile has: `RUN chmod -R 777 /vllm-workspace/mlperf-inference-6.0-redhat/harness/data/v4`

**Solution for Approach 2:**
- After upload, fix permissions on PVC:
  ```bash
  kubectl exec dataset-uploader -- chmod -R 777 /mnt/models/datasets
  ```

---

## Recommendation

**For development/testing:** Approach 1 (Docker image) is simpler - everything in one place.

**For production:** Approach 2 (PVC) is better:
- Faster iterations (no image rebuild for dataset changes)
- Smaller images = faster deployments
- Better separation of concerns (code vs data)
- Easier to manage multiple dataset versions
