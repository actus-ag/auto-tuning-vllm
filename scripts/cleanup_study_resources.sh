#!/bin/bash
# Cleanup script for Kubernetes resources left behind by unclean exits
# This script cleans up services, deployments, jobs, and Helm releases
# that match a study name prefix

set -euo pipefail

# Default values
NAMESPACE="${NAMESPACE:-}"
STUDY_NAME=""
DRY_RUN=false
VERBOSE=false
SKIP_CONFIRM=false
KUBECTL_CMD=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

usage() {
    cat <<EOF
Usage: $0 [OPTIONS] --study-name STUDY_NAME_OR_PREFIX

Clean up Kubernetes resources (services, deployments, jobs, Helm releases)
left behind by unclean exits from the study controller.

Resources are identified by matching the study name prefix. The study name
is sanitized (lowercase, hyphens) to match how resources are named.

IMPORTANT: This script will clean up ALL studies/resources that match the
provided prefix. For example, if you specify "my-study", it will clean up:
  - my-study-trial-1
  - my-study-trial-2
  - my-study-other-trial
  - etc.

Options:
    -n, --namespace NAMESPACE    Kubernetes namespace (default: default)
    -s, --study-name PREFIX      Study name or prefix (required)
    -d, --dry-run                Show what would be deleted without deleting
    -v, --verbose                Verbose output
    -y, --yes                    Skip confirmation prompt (use with caution)
    -h, --help                   Show this help message

Examples:
    # Clean up all resources for studies starting with "my-study"
    $0 --study-name my-study

    # Dry run to see what would be deleted
    $0 --study-name my-study --dry-run

    # Clean up in specific namespace
    $0 --study-name my-study --namespace my-namespace

    # Clean up without confirmation (useful for automation)
    $0 --study-name my-study --yes

EOF
    exit 1
}

log() {
    echo -e "${GREEN}[INFO]${NC} $*" >&2
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

verbose() {
    if [[ "$VERBOSE" == "true" ]]; then
        echo -e "${GREEN}[DEBUG]${NC} $*" >&2
    fi
}

# Sanitize study name to match how resources are named
# This matches the sanitize_release_name and sanitize_k8s_name functions
sanitize_name() {
    local name="$1"
    # Convert to lowercase
    name=$(echo "$name" | tr '[:upper:]' '[:lower:]')
    # Replace underscores and spaces with hyphens
    name=$(echo "$name" | tr '_ ' '-')
    # Remove invalid characters (keep only alphanumeric and hyphens)
    name=$(echo "$name" | sed 's/[^a-z0-9-]//g')
    # Remove consecutive hyphens
    name=$(echo "$name" | sed 's/-\+/-/g')
    # Remove leading/trailing hyphens
    name=$(echo "$name" | sed 's/^-\|-$//g')
    echo "$name"
}

# Detect and set kubectl/oc command
detect_kubectl() {
    if command -v oc &> /dev/null; then
        KUBECTL_CMD="oc"
        verbose "Using OpenShift CLI (oc)"
    elif command -v kubectl &> /dev/null; then
        KUBECTL_CMD="kubectl"
        verbose "Using kubectl"
    else
        error "Neither kubectl nor oc is installed or not in PATH"
        exit 1
    fi
    verbose "$KUBECTL_CMD found: $($KUBECTL_CMD version --client --short 2>/dev/null || echo 'version check failed')"
}

# Check if kubectl is available (backward compatibility)
check_kubectl() {
    detect_kubectl
}

# Check if helm is available
check_helm() {
    if ! command -v helm &> /dev/null; then
        warn "helm is not installed or not in PATH - will skip Helm release cleanup"
        return 1
    fi
    verbose "helm found: $(helm version --short 2>/dev/null || echo 'version check failed')"
    return 0
}

# Auto-detect namespace if not specified
detect_namespace() {
    if [[ -z "$NAMESPACE" ]]; then
        # Try to get current namespace from kubeconfig/context
        if [[ "$KUBECTL_CMD" == "oc" ]]; then
            # Try oc project -q first (most reliable for OpenShift)
            NAMESPACE=$($KUBECTL_CMD project -q 2>/dev/null || echo "")
            # Fallback to oc config view
            if [[ -z "$NAMESPACE" ]]; then
                NAMESPACE=$($KUBECTL_CMD config view --minify -o jsonpath='{..namespace}' 2>/dev/null || echo "")
            fi
        else
            NAMESPACE=$($KUBECTL_CMD config view --minify -o jsonpath='{..namespace}' 2>/dev/null || echo "")
        fi
        
        # If still no namespace, try to get from current context
        if [[ -z "$NAMESPACE" ]]; then
            # For OpenShift, try to list projects and use the first one (if only one)
            if [[ "$KUBECTL_CMD" == "oc" ]]; then
                local projects
                projects=$($KUBECTL_CMD get projects -o name 2>/dev/null | head -1 | sed 's|project.project.openshift.io/||' || echo "")
                if [[ -n "$projects" ]] && [[ $(echo "$projects" | wc -l) -eq 1 ]]; then
                    NAMESPACE="$projects"
                    verbose "Using only available project: $NAMESPACE"
                fi
            fi
        fi
        
        # Fallback to default
        if [[ -z "$NAMESPACE" ]]; then
            NAMESPACE="default"
            verbose "No namespace specified or detected, using default: $NAMESPACE"
        else
            verbose "Auto-detected namespace: $NAMESPACE"
        fi
    fi
}

# Check if namespace exists
check_namespace() {
    # For OpenShift, check projects instead of namespaces
    local check_cmd="namespace"
    if [[ "$KUBECTL_CMD" == "oc" ]]; then
        # Try project first (OpenShift)
        if $KUBECTL_CMD get project "$NAMESPACE" &> /dev/null; then
            verbose "Project '$NAMESPACE' exists"
            return 0
        fi
        # Fallback to namespace check
        check_cmd="namespace"
    fi
    
    if ! $KUBECTL_CMD get "$check_cmd" "$NAMESPACE" &> /dev/null; then
        error "Namespace/Project '$NAMESPACE' does not exist"
        if [[ "$KUBECTL_CMD" == "oc" ]]; then
            error "Available projects:"
            $KUBECTL_CMD get projects -o name 2>/dev/null | sed 's|project.project.openshift.io/||' | sed 's/^/  /' || true
            error ""
            error "Tip: Set your namespace with: oc project <project-name>"
            error "Or specify it with: --namespace <project-name>"
        else
            error "Available namespaces:"
            $KUBECTL_CMD get namespaces -o name 2>/dev/null | sed 's|namespace/||' | sed 's/^/  /' || true
            error ""
            error "Tip: Specify namespace with: --namespace <namespace-name>"
        fi
        exit 1
    fi
    verbose "Namespace '$NAMESPACE' exists"
}

# Clean up Helm releases
cleanup_helm_releases() {
    local sanitized_study="$1"
    local helm_available="$2"
    
    if [[ "$helm_available" != "0" ]]; then
        verbose "Skipping Helm release cleanup (helm not available)"
        return 0
    fi
    
    log "Checking for Helm releases matching study prefix..."
    
    # List all Helm releases in namespace
    local releases
    releases=$(helm list -n "$NAMESPACE" -q 2>/dev/null || echo "")
    
    if [[ -z "$releases" ]]; then
        verbose "No Helm releases found in namespace '$NAMESPACE'"
        return 0
    fi
    
    # Find releases that start with study name prefix
    local matching_releases=()
    while IFS= read -r release; do
        if [[ -n "$release" ]] && [[ "$release" == "$sanitized_study"* ]]; then
            matching_releases+=("$release")
        fi
    done <<< "$releases"
    
    if [[ ${#matching_releases[@]} -eq 0 ]]; then
        verbose "No Helm releases found matching study prefix '$sanitized_study'"
        return 0
    fi
    
    log "Found ${#matching_releases[@]} Helm release(s) matching prefix '$sanitized_study':"
    for release in "${matching_releases[@]}"; do
        echo "  - $release"
    done
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log "DRY RUN: Would uninstall ${#matching_releases[@]} Helm release(s)"
        return 0
    fi
    
    # Uninstall each release
    local failed=0
    for release in "${matching_releases[@]}"; do
        log "Uninstalling Helm release: $release"
        if helm uninstall "$release" --namespace "$NAMESPACE" 2>/dev/null; then
            log "Successfully uninstalled Helm release: $release"
        else
            warn "Failed to uninstall Helm release: $release (may already be deleted)"
            failed=$((failed + 1))
        fi
    done
    
    if [[ $failed -gt 0 ]]; then
        warn "$failed Helm release(s) failed to uninstall (may already be deleted)"
    fi
}

# Clean up Kubernetes resources by type
cleanup_k8s_resources() {
    local resource_type="$1"
    local sanitized_study="$2"
    local label_selector="${3:-}"
    
    log "Checking for $resource_type matching study prefix..."
    
    # Build $KUBECTL_CMD/oc command
    local cmd=("$KUBECTL_CMD" "get" "$resource_type" "-n" "$NAMESPACE" "-o" "name")
    if [[ -n "$label_selector" ]]; then
        cmd+=("-l" "$label_selector")
    fi
    
    # Get resources
    local resources
    resources=$("${cmd[@]}" 2>/dev/null || echo "")
    
    if [[ -z "$resources" ]]; then
        verbose "No $resource_type found in namespace '$NAMESPACE'"
        return 0
    fi
    
    # Filter resources by name prefix (match resources that START with the prefix)
    local matching_resources=()
    while IFS= read -r resource; do
        if [[ -n "$resource" ]]; then
            # Extract resource name (format: resource-type/name)
            local resource_name="${resource#*/}"
            # Match if resource name starts with the sanitized study prefix
            if [[ "$resource_name" == "$sanitized_study"* ]]; then
                matching_resources+=("$resource")
            fi
        fi
    done <<< "$resources"
    
    if [[ ${#matching_resources[@]} -eq 0 ]]; then
        verbose "No $resource_type found matching study prefix '$sanitized_study'"
        return 0
    fi
    
    log "Found ${#matching_resources[@]} $resource_type matching prefix '$sanitized_study':"
    for resource in "${matching_resources[@]}"; do
        echo "  - $resource"
    done
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log "DRY RUN: Would delete ${#matching_resources[@]} $resource_type"
        return 0
    fi
    
    # Delete resources
    local failed=0
    for resource in "${matching_resources[@]}"; do
        log "Deleting $resource"
        if $KUBECTL_CMD delete "$resource" -n "$NAMESPACE" --ignore-not-found=true 2>/dev/null; then
            verbose "Successfully deleted $resource"
        else
            warn "Failed to delete $resource (may already be deleted)"
            failed=$((failed + 1))
        fi
    done
    
    if [[ $failed -gt 0 ]]; then
        warn "$failed $resource_type failed to delete (may already be deleted)"
    fi
}

# Clean up readiness check jobs (they have a different naming pattern)
cleanup_readiness_jobs() {
    local sanitized_study="$1"
    
    log "Checking for readiness check jobs..."
    
    # Readiness check jobs are named like "readiness-check-{uuid}"
    # They have label "app=auto-tune-vllm-readiness-check"
    local resources
    resources=$($KUBECTL_CMD get jobs -n "$NAMESPACE" -l app=auto-tune-vllm-readiness-check -o name 2>/dev/null || echo "")
    
    if [[ -z "$resources" ]]; then
        verbose "No readiness check jobs found"
        return 0
    fi
    
    local matching_resources=()
    while IFS= read -r resource; do
        if [[ -n "$resource" ]]; then
            matching_resources+=("$resource")
        fi
    done <<< "$resources"
    
    if [[ ${#matching_resources[@]} -eq 0 ]]; then
        verbose "No readiness check jobs found"
        return 0
    fi
    
    log "Found ${#matching_resources[@]} readiness check job(s):"
    for resource in "${matching_resources[@]}"; do
        echo "  - $resource"
    done
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log "DRY RUN: Would delete readiness check jobs"
        return 0
    fi
    
    # Delete resources
    local failed=0
    for resource in "${matching_resources[@]}"; do
        log "Deleting $resource"
        if $KUBECTL_CMD delete "$resource" -n "$NAMESPACE" --ignore-not-found=true 2>/dev/null; then
            verbose "Successfully deleted $resource"
        else
            warn "Failed to delete $resource (may already be deleted)"
            failed=$((failed + 1))
        fi
    done
    
    if [[ $failed -gt 0 ]]; then
        warn "$failed readiness check job(s) failed to delete (may already be deleted)"
    fi
}

# Count total resources that will be cleaned up
count_resources_to_cleanup() {
    local sanitized_study="$1"
    local helm_available="$2"
    local namespace="$3"
    local total=0
    
    # Count Helm releases
    if [[ "$helm_available" == "0" ]]; then
        local releases
        releases=$(helm list -n "$namespace" -q 2>/dev/null || echo "")
        if [[ -n "$releases" ]]; then
            while IFS= read -r release; do
                if [[ -n "$release" ]] && [[ "$release" == "$sanitized_study"* ]]; then
                    total=$((total + 1))
                fi
            done <<< "$releases"
        fi
    fi
    
    # Count Kubernetes resources
    # Use kubectl/oc from parent scope (passed as $4)
    local kube_cmd="${4:-kubectl}"
    for resource_type in services deployments jobs; do
        local resources
        resources=$($kube_cmd get "$resource_type" -n "$namespace" -o name 2>/dev/null || echo "")
        if [[ -n "$resources" ]]; then
            while IFS= read -r resource; do
                if [[ -n "$resource" ]]; then
                    local resource_name="${resource#*/}"
                    # Debug: show what we're comparing (if VERBOSE is set in environment)
                    if [[ "${VERBOSE:-false}" == "true" ]]; then
                        echo "Comparing: '$resource_name' with prefix '$sanitized_study'" >&2
                    fi
                    if [[ "$resource_name" == "$sanitized_study"* ]]; then
                        total=$((total + 1))
                    fi
                fi
            done <<< "$resources"
        fi
    done
    
    echo "$total"
}

# Main cleanup function
main() {
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            -n|--namespace)
                NAMESPACE="$2"
                shift 2
                ;;
            -s|--study-name)
                STUDY_NAME="$2"
                shift 2
                ;;
            -d|--dry-run)
                DRY_RUN=true
                shift
                ;;
            -v|--verbose)
                VERBOSE=true
                shift
                ;;
            -y|--yes)
                SKIP_CONFIRM=true
                shift
                ;;
            -h|--help)
                usage
                ;;
            *)
                error "Unknown option: $1"
                usage
                ;;
        esac
    done
    
    # Validate required arguments
    if [[ -z "$STUDY_NAME" ]]; then
        error "Study name is required"
        usage
    fi
    
    # Sanitize study name
    SANITIZED_STUDY=$(sanitize_name "$STUDY_NAME")
    verbose "Original study name/prefix: $STUDY_NAME"
    verbose "Sanitized study prefix: $SANITIZED_STUDY"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log "DRY RUN MODE - No resources will be deleted"
    fi
    
    # Check prerequisites
    detect_kubectl
    local helm_available=0
    check_helm || helm_available=1
    detect_namespace
    check_namespace
    
    log "Starting cleanup for studies matching prefix '$STUDY_NAME' (sanitized: '$SANITIZED_STUDY') in namespace '$NAMESPACE'"
    log "NOTE: This will clean up ALL studies/resources that match this prefix!"
    echo ""
    
    # Count resources and ask for confirmation (unless --yes or --dry-run)
    if [[ "$DRY_RUN" != "true" ]] && [[ "$SKIP_CONFIRM" != "true" ]]; then
        log "Scanning for matching resources in namespace '$NAMESPACE'..."
        verbose "Using command: $KUBECTL_CMD"
        verbose "Looking for resources starting with: '$SANITIZED_STUDY'"
        local resource_count
        resource_count=$(VERBOSE="$VERBOSE" count_resources_to_cleanup "$SANITIZED_STUDY" "$helm_available" "$NAMESPACE" "$KUBECTL_CMD")
        
        if [[ $resource_count -gt 0 ]]; then
            echo ""
            warn "Found approximately $resource_count resource(s) matching prefix '$SANITIZED_STUDY'"
            warn "This will clean up ALL studies/resources starting with this prefix!"
            echo -n "Do you want to proceed with cleanup? [y/N] "
            read -r response
            if [[ ! "$response" =~ ^[Yy]$ ]]; then
                log "Cleanup cancelled by user"
                exit 0
            fi
            echo ""
        else
            warn "No resources found matching prefix '$SANITIZED_STUDY' in namespace '$NAMESPACE'"
            verbose "Tip: Use --verbose to see detailed matching information"
            verbose "Tip: Check if you're in the correct namespace (current: $NAMESPACE)"
            verbose "Tip: List resources manually: $KUBECTL_CMD get deployments -n $NAMESPACE | grep $SANITIZED_STUDY"
            exit 0
        fi
    fi
    
    # Clean up resources in order
    # 1. Helm releases (this will also clean up associated resources)
    cleanup_helm_releases "$SANITIZED_STUDY" "$helm_available"
    echo ""
    
    # 2. Services
    cleanup_k8s_resources "services" "$SANITIZED_STUDY"
    echo ""
    
    # 3. Deployments
    cleanup_k8s_resources "deployments" "$SANITIZED_STUDY"
    echo ""
    
    # 4. Jobs (benchmark jobs and others)
    cleanup_k8s_resources "jobs" "$SANITIZED_STUDY"
    echo ""
    
    # 5. Readiness check jobs (special case)
    cleanup_readiness_jobs "$SANITIZED_STUDY"
    echo ""
    
    # 6. Pods (orphaned pods that might not be cleaned up by deployments)
    log "Checking for orphaned pods..."
    local pods
    pods=$($KUBECTL_CMD get pods -n "$NAMESPACE" -o name 2>/dev/null || echo "")
    if [[ -n "$pods" ]]; then
        local matching_pods=()
        while IFS= read -r pod; do
            if [[ -n "$pod" ]]; then
                local pod_name="${pod#*/}"
                # Check if pod name contains study name or has matching labels
                if [[ "$pod_name" == *"$SANITIZED_STUDY"* ]] || \
                   $KUBECTL_CMD get "$pod" -n "$NAMESPACE" -o jsonpath='{.metadata.labels.study-name}' 2>/dev/null | grep -q "$SANITIZED_STUDY"; then
                    matching_pods+=("$pod")
                fi
            fi
        done <<< "$pods"
        
        if [[ ${#matching_pods[@]} -gt 0 ]]; then
            log "Found ${#matching_pods[@]} orphaned pod(s):"
            for pod in "${matching_pods[@]}"; do
                echo "  - $pod"
            done
            
            if [[ "$DRY_RUN" != "true" ]]; then
                for pod in "${matching_pods[@]}"; do
                    log "Deleting $pod"
                    $KUBECTL_CMD delete "$pod" -n "$NAMESPACE" --ignore-not-found=true 2>/dev/null || true
                done
            else
                log "DRY RUN: Would delete orphaned pods"
            fi
        else
            verbose "No orphaned pods found matching study name"
        fi
    else
        verbose "No pods found in namespace"
    fi
    
    echo ""
    log "Cleanup completed for studies matching prefix '$STUDY_NAME'"
}

# Run main function
main "$@"
