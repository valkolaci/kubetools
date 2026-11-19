# kubecheck

Shows how much CPU and memory the pods on a Kubernetes cluster have
*requested*, per node and in total. It also lists the pods that are still
waiting to be scheduled. It answers questions like "why doesn't this pod
fit?" or "how much room is left on each node?" by doing the same request
calculation the scheduler does.

It reports requests, not actual usage. For live usage, use `kubectl top`.

## Requirements

- Python 3 (tested with 3.11), standard library only
- `kubectl` on `PATH`, configured for the target cluster
- Permission to list nodes and to list pods in all namespaces. Permission to
  list PriorityClasses is optional (see [Priorities](#priorities)).

## Usage

```
./kubecheck.py
```

There are no options. It uses kubectl's current context. To check a different
cluster, set `KUBECONFIG` or run `kubectl config use-context` first.

The report goes to stdout. Warnings, such as quantities that can't be parsed,
go to stderr. If `kubectl` can't be found or run, or listing nodes or pods
fails or returns something other than a JSON object, the script prints an
error and exits with code 1.

## Output

```
Pods:
  a/p1: node=n1 cpu=500m mem=1.5Gi prio=high[1000]
  a/p2: node=n1 cpu=0m mem=100M prio=[0]
  b/pend: node= cpu=1000m mem=2Gi prio=high[1000]
  b/ghost: node=gone cpu=0m mem=0 prio=[0]
  c/podlevel: node=n2 cpu=2000m mem=4Gi prio=[0]
Nodes:
  n1 [cordoned]: totalcpu=3920m totalmem=~15.411Gi usedcpu=500m usedmem=~1.593Gi leftcpu=3420m leftmem=~13.818Gi pods=2/110
    a/p1: cpu=500m mem=1.5Gi prio=high[1000]
    a/p2: cpu=0m mem=100M prio=[0]
  n2 [NotReady]: totalcpu=2000m totalmem=8Gi usedcpu=2000m usedmem=4Gi leftcpu=0m leftmem=4Gi pods=1/110
    c/podlevel: cpu=2000m mem=4Gi prio=[0]
Waiting for scheduling: cpu=1000m mem=2Gi pods=1
  b/pend: cpu=1000m mem=2Gi prio=high[1000]
Bound to unknown nodes: cpu=0m mem=0 pods=1
  b/ghost: node=gone cpu=0m mem=0 prio=[0]
```

The report has these sections:

- **Pods**: every pod that holds resources, in API order, with its node, its
  effective request and its priority.
- **Nodes**: one line per node, sorted by name, then that node's pods sorted
  by priority (highest first) and then by name.
  - `total*` is the node's *allocatable* capacity, `used*` is the sum of its
    pods' requests and `left*` is the difference. `left*` goes negative when a
    node is overcommitted.
  - `pods=N/max` compares the number of pods with the node's allocatable pod
    count. It shows `?` if the node doesn't report one.
  - `[NotReady]` means the node's Ready condition isn't `True`. `[cordoned]`
    means the node is unschedulable. New pods won't land on either.
  - A node that reports no allocatable resources is shown with zero capacity,
    which is how the scheduler sees it, and a warning is printed on stderr.
- **Waiting for scheduling**: pods that have no node yet. The header line
  shows their total request.
- **Bound to unknown nodes**: pods assigned to a node that isn't in the node
  list, for example because the node was deleted. This section and the one
  above are left out when empty.

Number formats:

- CPU is always in millicores (`1500m` = 1.5 cores).
- Memory is in whole bytes, shown exactly with at most 3 decimals where
  possible:
  1. in the value's natural unit, the largest binary or decimal unit not
     above it (`1.5Gi`, `100M`, `512`),
  2. otherwise in the next smaller unit (`1088Mi` rather than `~1.062Gi`).

  Binary is preferred when both are exact and equally short. Units further
  down are not tried, so a value that would only be exact in plain bytes is
  rounded in its natural binary unit and prefixed with `~` (`~15.411Gi`).
- Priority is shown as `class[value]`. A pod with no priority class shows
  `[0]`.

## How requests are calculated

kubecheck calculates a pod's request the same way the scheduler does, for CPU
and memory separately:

1. Add up the requests of the regular containers.
2. Add sidecars: init containers with `restartPolicy: Always`. They keep
   running next to everything that starts after them.
3. Take the larger of that total and the biggest init container. Each init
   container counts together with the sidecars started before it.
4. If the pod sets requests for the whole pod (`spec.resources.requests`),
   those replace the result for each resource they set.
5. Add the pod overhead (`spec.overhead`).
6. Round the total up to whole millicores of CPU and whole bytes of memory.
   Like the scheduler, kubecheck adds exact values and only rounds the pod
   total. Node capacity is rounded the same way.

While a container is being resized in place, kubecheck uses the largest of
its requested, actual (`status.resources`) and allocated requests. If the
kubelet has marked the resize as infeasible, the requested value is ignored,
because it will never be applied.

Pods in phase `Succeeded` or `Failed` are skipped, because they no longer hold
resources. A request that can't be parsed is reported on stderr and counted
as 0. Quantities are capped at 2^63-1, the same limit Kubernetes uses.

## Priorities

A pod's priority is read from `spec.priority`, which Kubernetes fills in when
the pod is created. For a pod without it, kubecheck looks up the value of its
`priorityClassName` in the PriorityClass list. If kubecheck isn't allowed to
list PriorityClasses, or the list isn't valid JSON, it prints a warning and
continues, and those pods get priority 0.

## Limitations

- Only CPU and memory are checked. Ephemeral storage, hugepages, extended
  resources (such as GPUs) and DRA claims are not.
- In-place resizing of pod-level resources (alpha in Kubernetes 1.35) is not
  handled. Pod-level requests are read from the spec only.
- Nothing is flagged when a node is overcommitted or full. Look for negative
  `left*` values or `pods=N/max` where N has reached max.

## Tests

```
python3 -m unittest -v test_kubecheck
```

Run this from this directory. The tests use only the standard library. The
end-to-end tests put a fake `kubectl` on `PATH` that returns JSON test data,
so no cluster is needed.
