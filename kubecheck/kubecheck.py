#!/usr/bin/env python3

import sys
import re
import json
import subprocess

from decimal import Decimal, InvalidOperation, ROUND_CEILING

# Kubernetes resource.Quantity: <sign><number><suffix>, where suffix is a
# binary SI unit (Ki..Ei), a decimal SI unit (n, u, m, k, M..E) or an
# exponent (e3, E-2). The match is anchored (fullmatch), so the regex
# backtracks as needed: "1E3" is an exponent, a bare "1E" is exa and "1Ei"
# is exbi, regardless of the order of the alternatives.
QUANTITY = re.compile(r'([+-]?)([0-9]+(?:\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+|Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?')
# Kubernetes caps quantities at 2^63-1 in magnitude.
MAX_QUANTITY = Decimal(2**63 - 1)
QUANTITY_SUFFIXES = {
  'n': Decimal('1e-9'), 'u': Decimal('1e-6'), 'm': Decimal('1e-3'),
  'k': Decimal('1e3'), 'M': Decimal('1e6'), 'G': Decimal('1e9'),
  'T': Decimal('1e12'), 'P': Decimal('1e15'), 'E': Decimal('1e18'),
  'Ki': Decimal(2**10), 'Mi': Decimal(2**20), 'Gi': Decimal(2**30),
  'Ti': Decimal(2**40), 'Pi': Decimal(2**50), 'Ei': Decimal(2**60),
}

def parse_quantity(s):
  """Parse a Kubernetes quantity string into a Decimal, or None if invalid."""
  m = QUANTITY.fullmatch(str(s).strip())
  if not m:
    return None
  sign, number, suffix = m.groups()
  if suffix is None:
    value = Decimal(number)
  elif suffix in QUANTITY_SUFFIXES:
    value = Decimal(number) * QUANTITY_SUFFIXES[suffix]
  else:
    # Exponent: let Decimal parse it directly, which avoids context
    # overflow and int() digit limits. Exponents beyond what Decimal can
    # represent at all are clamped: huge -> max, tiny -> 0.
    try:
      value = Decimal(number + suffix)
    except InvalidOperation:
      huge = suffix[1] != '-' and Decimal(number) != 0
      value = MAX_QUANTITY if huge else Decimal(0)
  value = min(value, MAX_QUANTITY)
  return -value if sign == '-' else value

def jget(obj, key, default):
  """obj[key], or default if the key is missing or its value is JSON null."""
  v = obj.get(key)
  return default if v is None else v

def to_millicores(q):
  """CPU Decimal (cores) -> integer millicores, rounded up like MilliValue()."""
  return int((q * 1000).to_integral_value(rounding=ROUND_CEILING))

def to_bytes(q):
  """Memory Decimal -> integer bytes, rounded up like Value()."""
  return int(q.to_integral_value(rounding=ROUND_CEILING))

ZERO = Decimal(0)

def parse_requests(req, where):
  """Return exact (cpu cores, memory bytes) Decimals from a resource list;
  unparsable values are reported on stderr and counted as zero, missing or
  null ones are zero."""
  values = []
  for resource in ('cpu', 'memory'):
    raw = req.get(resource)
    v = ZERO
    if raw is not None:
      v = parse_quantity(raw)
      if v is None:
        sys.stderr.write("warning: %s: cannot parse %s quantity %r, counting as 0\n" % (where, resource, raw))
        v = ZERO
    values.append(v)
  return tuple(values)

def resize_infeasible(status):
  """True if the kubelet declared a pending in-place resize infeasible:
  status.resize=Infeasible before Kubernetes 1.33, a PodResizePending
  condition with reason Infeasible since."""
  if status.get('resize') == 'Infeasible':
    return True
  return any(cond.get('type') == 'PodResizePending' and cond.get('reason') == 'Infeasible'
             for cond in jget(status, 'conditions', []))

def resource_requests(obj):
  """obj.resources.requests: the requests of a container, its status or a
  pod spec ({} if unset or null)."""
  return jget(jget(obj, 'resources', {}), 'requests', {})

def container_requests(cont, statuses, infeasible, where):
  """Exact (cpu, memory) request of one container. With in-place pod
  resize, if the container status reports resources, the scheduler takes
  the per-resource max of the desired (spec), actual (status.resources) and
  allocated requests; the desired one is left out when the resize is
  infeasible, since it will never be applied."""
  spec_req = parse_requests(resource_requests(cont), where)
  cs = statuses.get(cont.get('name'))
  if cs is None or cs.get('resources') is None:
    return spec_req
  candidates = [
    parse_requests(resource_requests(cs), where + '[status]'),
    parse_requests(jget(cs, 'allocatedResources', {}), where + '[allocated]'),
  ]
  if not infeasible:
    candidates.append(spec_req)
  return max(c for c, _ in candidates), max(m for _, m in candidates)

def pod_requests(spec, status, podname):
  """Effective pod request as the scheduler computes it:
  max(sum(containers) + sum(sidecars), max(each init container + sidecars
  started before it)) + pod overhead, evaluated per resource. Pod-level
  requests (spec.resources) replace the container aggregate for the
  resources they set; container requests account for in-place resizes.
  The sum is exact; only the pod total is rounded up, to integer
  millicores and bytes, as the scheduler does."""
  infeasible = resize_infeasible(status)
  statuses = {cs.get('name'): cs for cs in jget(status, 'containerStatuses', [])}
  init_statuses = {cs.get('name'): cs for cs in jget(status, 'initContainerStatuses', [])}

  cpu = mem = ZERO
  for cont in jget(spec, 'containers', []):
    c, m = container_requests(cont, statuses, infeasible,
                              '%s[%s]' % (podname, jget(cont, 'name', '?')))
    cpu += c
    mem += m

  sidecar_cpu = sidecar_mem = ZERO
  init_cpu = init_mem = ZERO
  for cont in jget(spec, 'initContainers', []):
    where = '%s[init:%s]' % (podname, jget(cont, 'name', '?'))
    if cont.get('restartPolicy') == 'Always':
      # Sidecar: keeps running alongside everything started after it, and
      # can be resized in place like a regular container.
      c, m = container_requests(cont, init_statuses, infeasible, where)
      sidecar_cpu += c
      sidecar_mem += m
      c, m = sidecar_cpu, sidecar_mem
    else:
      c, m = parse_requests(resource_requests(cont), where)
      c += sidecar_cpu
      m += sidecar_mem
    init_cpu = max(init_cpu, c)
    init_mem = max(init_mem, m)

  cpu = max(cpu + sidecar_cpu, init_cpu)
  mem = max(mem + sidecar_mem, init_mem)

  # Pod-level resources: each of cpu/memory set here overrides the value
  # aggregated from the containers.
  podreq = resource_requests(spec)
  if podreq:
    c, m = parse_requests(podreq, '%s[pod]' % podname)
    if podreq.get('cpu') is not None:
      cpu = c
    if podreq.get('memory') is not None:
      mem = m

  c, m = parse_requests(jget(spec, 'overhead', {}), '%s[overhead]' % podname)
  return to_millicores(cpu + c), to_bytes(mem + m)

def convert_cpu_to_string(cpu):
  return ("%dm" % cpu)

# Memory display units, largest first, ending with bytes.
MEM_BINARY_UNITS = [(s, QUANTITY_SUFFIXES[s]) for s in ('Ei', 'Pi', 'Ti', 'Gi', 'Mi', 'Ki')] + \
                   [('', Decimal(1))]
MEM_DECIMAL_UNITS = [(s, QUANTITY_SUFFIXES[s]) for s in ('E', 'P', 'T', 'G', 'M', 'k')] + \
                    [('', Decimal(1))]
MEM_DECIMALS = 3
MEM_STEP = Decimal(1).scaleb(-MEM_DECIMALS)

def format_decimal(q):
  """Decimal -> string with at most MEM_DECIMALS decimals, no trailing zeros."""
  return format(q.quantize(MEM_STEP).normalize(), 'f')

def unit_steps(v, units):
  """[natural unit, next smaller unit] of v, the natural unit being the
  largest one not exceeding |v| (the smallest unit if |v| is below all).
  The second entry is missing if the natural unit is the smallest."""
  for i, (_, unit) in enumerate(units):
    if abs(v) >= unit:
      return units[i:i + 2]
  return units[-1:]

def convert_mem_to_string(mem):
  """Integer bytes -> Kubernetes quantity string, exact when representable
  with at most MEM_DECIMALS decimals. Tried in order, the shorter form
  winning within a step (binary on a tie):
    1. the natural binary or decimal unit (1.5Gi, 2.5G, 100k),
    2. the next smaller binary or decimal unit (1088Mi, 1025),
    3. otherwise rounded in the natural binary unit and prefixed with '~'
       (e.g. ~2.703Gi).
  Units further down are not tried: plain bytes are always exact but
  unreadable for large values."""
  v = Decimal(mem)
  if v == 0:
    return '0'

  steps = [unit_steps(v, units) for units in (MEM_BINARY_UNITS, MEM_DECIMAL_UNITS)]
  for step in range(2):
    best = None
    for units in steps:
      if step >= len(units):
        continue
      suffix, unit = units[step]
      if (v / MEM_STEP) % unit == 0:
        s = format_decimal(v / unit) + suffix
        if best is None or len(s) < len(best):
          best = s
    if best is not None:
      return best

  suffix, unit = steps[0][0]
  return '~' + format_decimal(v / unit) + suffix

def kubectl_json(*args, required=True):
  """Run kubectl with the given arguments and return its parsed JSON output.
  If kubectl fails or its output is not a JSON object: exit with an error
  message if required, otherwise warn and return an empty list object
  ({'items': []}). If kubectl can't be run at all, always exit."""
  cmd = ['kubectl'] + list(args)
  level = 'error' if required else 'warning'
  try:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  except FileNotFoundError:
    sys.stderr.write("error: kubectl not found on PATH\n")
    sys.exit(1)
  except OSError as e:
    sys.stderr.write("error: cannot run kubectl: %s\n" % e.strerror)
    sys.exit(1)
  if result.returncode != 0:
    sys.stderr.write("%s: '%s' failed with error code: %d\n%s" % (
      level, ' '.join(cmd), result.returncode,
      result.stderr.decode('utf-8', errors='replace')))
    if required:
      sys.exit(1)
    return {'items': []}
  try:
    # ValueError covers both JSONDecodeError and UnicodeDecodeError.
    data = json.loads(result.stdout.decode('utf-8'))
    if not isinstance(data, dict):
      raise ValueError('expected a JSON object, got %s' % type(data).__name__)
  except ValueError as e:
    sys.stderr.write("%s: '%s' returned invalid JSON: %s\n" % (level, ' '.join(cmd), e))
    if required:
      sys.exit(1)
    return {'items': []}
  return data

def load_nodes(nodelist):
  """Node list JSON -> {name: {cpu, memory, maxpods, unschedulable, ready, pods}}."""
  nodebyname = {}
  for entry in jget(nodelist, 'items', []):
    name = jget(entry, 'metadata', {}).get('name')
    if not name:
      continue
    status = jget(entry, 'status', {})
    a = status.get('allocatable')
    if a is None:
      # The scheduler sees such a node as having no capacity; keep it so
      # its pods are still attributed to it.
      sys.stderr.write("warning: node %s has no allocatable resources, counting capacity as 0\n" % name)
      a = {}
    c, m = parse_requests(a, 'node %s allocatable' % name)
    maxpods = None
    if a.get('pods') is not None:
      maxpods = parse_quantity(a['pods'])
      if maxpods is None:
        sys.stderr.write("warning: node %s allocatable: cannot parse pods quantity %r\n" % (name, a['pods']))
    # Like 'kubectl get node': anything but Ready=True (False, Unknown,
    # missing) is NotReady.
    ready = any(cond.get('type') == 'Ready' and cond.get('status') == 'True'
                for cond in jget(status, 'conditions', []))
    nodebyname[name] = {
      'cpu': to_millicores(c), 'memory': to_bytes(m), 'pods': [],
      'maxpods': int(maxpods) if maxpods is not None else None,
      'unschedulable': bool(jget(entry, 'spec', {}).get('unschedulable')),
      'ready': ready,
    }
  return nodebyname

def load_priorities(pclist):
  """PriorityClass list JSON -> {name: value}."""
  priobyname = {}
  for entry in jget(pclist, 'items', []):
    name = jget(entry, 'metadata', {}).get('name')
    value = entry.get('value')
    if name and isinstance(value, int):
      priobyname[name] = value
  return priobyname

def load_pods(podlist, nodebyname, priobyname):
  """Pod list JSON -> (podbyname, podsleft, podsunknown). Each kept pod gets
  cpu/memory/prioname/priovalue/nodename added and, if bound to a known node,
  is appended to that node's 'pods' list. podbyname keeps API order."""
  podbyname = {}
  podsleft = []
  podsunknown = []  # bound (nodeName set) to a node missing from nodebyname
  for entry in jget(podlist, 'items', []):
    metadata = jget(entry, 'metadata', {})
    spec = entry.get('spec')
    if spec is None or not metadata.get('name') or not metadata.get('namespace'):
      continue
    name = '%s/%s' % (metadata['namespace'], metadata['name'])
    status = jget(entry, 'status', {})
    # Terminated pods no longer hold resources from the scheduler's view.
    if status.get('phase') in ('Succeeded', 'Failed'):
      continue
    nodename = spec.get('nodeName') or None
    if nodename is None:
      podsleft.append(name)
    elif nodename in nodebyname:
      nodebyname[nodename]['pods'].append(name)
    else:
      podsunknown.append(name)
    prioname = jget(spec, 'priorityClassName', '')
    # The priority admission controller resolves the value (including the
    # globalDefault class) into spec.priority; fall back to a class lookup.
    priovalue = spec.get('priority')
    if not isinstance(priovalue, int):
      priovalue = priobyname.get(prioname, 0)
    entry['cpu'], entry['memory'] = pod_requests(spec, status, name)
    entry['prioname'] = prioname
    entry['priovalue'] = priovalue
    entry['nodename'] = nodename
    podbyname[name] = entry
  return podbyname, podsleft, podsunknown

def format_prio(pod):
  return '%s[%d]' % (pod['prioname'], pod['priovalue'])

def by_priority(podbyname, podnames):
  """Pod names sorted by descending priority, then name."""
  return sorted(podnames, key=lambda podname: [-podbyname[podname]['priovalue'], podname])

def node_flags(nn):
  """' [NotReady,cordoned]'-style marker for nodes that can't take new pods."""
  flags = []
  if not nn['ready']:
    flags.append('NotReady')
  if nn['unschedulable']:
    flags.append('cordoned')
  return ' [%s]' % ','.join(flags) if flags else ''

def print_pods(podbyname):
  print('Pods:')
  for name, pod in podbyname.items():
    print('  %s: node=%s cpu=%s mem=%s prio=%s' % (name,
      pod['nodename'] if pod['nodename'] is not None else '',
      convert_cpu_to_string(pod['cpu']),
      convert_mem_to_string(pod['memory']),
      format_prio(pod)))

def print_nodes(nodebyname, podbyname):
  print('Nodes:')
  for nodename in sorted(nodebyname):
    nn = nodebyname[nodename]
    usedcpu = sum(podbyname[podname]['cpu'] for podname in nn['pods'])
    usedmem = sum(podbyname[podname]['memory'] for podname in nn['pods'])
    print('  %s%s: totalcpu=%s totalmem=%s usedcpu=%s usedmem=%s leftcpu=%s leftmem=%s pods=%d/%s' %
      (nodename,
      node_flags(nn),
      convert_cpu_to_string(nn['cpu']),
      convert_mem_to_string(nn['memory']),
      convert_cpu_to_string(usedcpu),
      convert_mem_to_string(usedmem),
      convert_cpu_to_string(nn['cpu'] - usedcpu),
      convert_mem_to_string(nn['memory'] - usedmem),
      len(nn['pods']),
      nn['maxpods'] if nn['maxpods'] is not None else '?'))
    for podname in by_priority(podbyname, nn['pods']):
      pod = podbyname[podname]
      print('    %s: cpu=%s mem=%s prio=%s' % (
        podname,
        convert_cpu_to_string(pod['cpu']),
        convert_mem_to_string(pod['memory']),
        format_prio(pod)))

def print_pod_group(title, podbyname, podnames, show_node=False):
  """Print a header with the group's request totals, then each pod by priority."""
  if not podnames:
    return
  totalcpu = sum(podbyname[podname]['cpu'] for podname in podnames)
  totalmem = sum(podbyname[podname]['memory'] for podname in podnames)
  print('%s: cpu=%s mem=%s pods=%d' % (title,
    convert_cpu_to_string(totalcpu),
    convert_mem_to_string(totalmem),
    len(podnames)))
  for podname in by_priority(podbyname, podnames):
    pod = podbyname[podname]
    print('  %s:%s cpu=%s mem=%s prio=%s' % (
      podname,
      ' node=%s' % pod['nodename'] if show_node else '',
      convert_cpu_to_string(pod['cpu']),
      convert_mem_to_string(pod['memory']),
      format_prio(pod)))

def main():
  nodelist = kubectl_json('get', 'node', '-o', 'json')
  # Only a fallback for pods lacking spec.priority, so don't fail without it
  # (e.g. no RBAC permission to list PriorityClasses).
  pclist = kubectl_json('get', 'pc', '-o', 'json', required=False)
  podlist = kubectl_json('get', 'pod', '-A', '-o', 'json')

  nodebyname = load_nodes(nodelist)
  priobyname = load_priorities(pclist)
  podbyname, podsleft, podsunknown = load_pods(podlist, nodebyname, priobyname)

  print_pods(podbyname)
  print_nodes(nodebyname, podbyname)
  print_pod_group('Waiting for scheduling', podbyname, podsleft)
  print_pod_group('Bound to unknown nodes', podbyname, podsunknown, show_node=True)

if __name__ == '__main__':
  main()
