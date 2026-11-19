#!/usr/bin/env python3
"""Tests for kubecheck. Run from this directory with:

  python3 -m unittest -v test_kubecheck

The end-to-end tests put a fake 'kubectl' on PATH that serves JSON fixtures,
so no cluster is needed.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kubecheck as k  # noqa: E402

GI = 2**30
MI = 2**20
MAX = Decimal(2**63 - 1)


class ParseQuantityTest(unittest.TestCase):
  def test_valid(self):
    cases = [
      ('100m', Decimal('0.1')), ('1', 1), ('1.5Gi', 3 * GI // 2),
      ('1E', Decimal('1e18')), ('1E3', 1000), ('1e-2', Decimal('0.01')),
      ('1Ei', 2**60), ('+2k', 2000), ('-1', -1), ('.5', Decimal('0.5')),
      ('1.', 1), (' 2Mi ', 2 * MI), ('1e18', Decimal('1e18')),
    ]
    for s, expected in cases:
      with self.subTest(s=s):
        self.assertEqual(k.parse_quantity(s), expected)

  def test_invalid(self):
    for s in ['5x', '', '1e', 'Mi', '1.5.5', 'm', '1 Gi']:
      with self.subTest(s=s):
        self.assertIsNone(k.parse_quantity(s))

  def test_huge_and_tiny_are_capped(self):
    cases = [
      ('1e1000000', MAX), ('1e99999999999999999999', MAX),
      ('-1e99999999999999999999', -MAX), ('1e-99999999999999999999', 0),
      ('0e99999999999999999999', 0), ('1e400', MAX), ('100Ei', MAX),
    ]
    for s, expected in cases:
      with self.subTest(s=s):
        self.assertEqual(k.parse_quantity(s), expected)

  def test_huge_values_format_without_error(self):
    mem = k.to_bytes(k.parse_quantity('1e400'))
    self.assertEqual(mem, int(MAX))
    for v in (mem, -mem):
      self.assertTrue(k.convert_mem_to_string(v))
    self.assertEqual(k.convert_mem_to_string(mem), '~8Ei')  # 2^63-1 is not exactly 8Ei
    self.assertEqual(k.to_millicores(k.parse_quantity('1e400')), int(MAX * 1000))

  def test_rounding_up(self):
    q = k.parse_quantity
    self.assertEqual(k.to_millicores(q('0.0001')), 1)
    self.assertEqual(k.to_millicores(q('250m')), 250)
    self.assertEqual(k.to_millicores(q('2')), 2000)
    self.assertEqual(k.to_bytes(q('500m')), 1)
    self.assertEqual(k.to_bytes(q('1Ki')), 1024)


class FormatTest(unittest.TestCase):
  def test_convert_mem_to_string(self):
    cases = [
      (0, '0'), (1, '1'), (1024, '1Ki'), (1000, '1k'), (1536, '1.5Ki'), (1025, '1.025k'),
      (1.5 * GI, '1.5Gi'), (2.5e9, '2.5G'), (-GI, '-1Gi'),
      (129e6, '129M'), (2902458368 + 1, '~2.703Gi'),
      # Exact in the next smaller unit only.
      (2902458368, '2768Mi'), (GI + MI, '1025Mi'), (GI + 64 * MI, '1088Mi'),
      (-(GI + MI), '-1025Mi'), (1.0005e9, '1000.5M'),
      # The natural unit wins even if the next smaller one is shorter.
      (1.125 * GI, '1.125Gi'),
      # Exact only two or more units down: rounded instead.
      (GI + 1024, '~1Gi'), (3.5 * GI + 1, '~3.5Gi'),
    ]
    for v, expected in cases:
      with self.subTest(v=v):
        self.assertEqual(k.convert_mem_to_string(v), expected)

  def test_convert_cpu_to_string(self):
    self.assertEqual(k.convert_cpu_to_string(1500), '1500m')


def container(name, cpu=None, mem=None, **extra):
  req = {}
  if cpu is not None:
    req['cpu'] = cpu
  if mem is not None:
    req['memory'] = mem
  return dict(name=name, resources={'requests': req}, **extra)


class PodRequestsTest(unittest.TestCase):
  def test_containers_and_overhead(self):
    spec = {'containers': [container('a', '1', '1Gi'), container('b', '500m', '1Mi')],
            'overhead': {'cpu': '100m', 'memory': '1Mi'}}
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (1600, GI + 2 * MI))

  def test_sidecars(self):
    # Init container i1 (2 CPU) runs alone; sidecar s1 then runs next to
    # i2 and the app container.
    spec = {'initContainers': [container('i1', '2'),
                               container('s1', '500m', restartPolicy='Always'),
                               container('i2', '1')],
            'containers': [container('c', '1', '1Gi')],
            'overhead': {'cpu': '100m'}}
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (2100, GI))
    spec['initContainers'][0] = container('i1', '1')
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (1600, GI))

  def test_pod_level_resources_override_per_resource(self):
    spec = {'resources': {'requests': {'cpu': '2'}},
            'containers': [container('c', '1', '1Gi')],
            'overhead': {'memory': '1Mi'}}
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (2000, GI + MI))
    spec['resources']['requests']['memory'] = '4Gi'
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (2000, 4 * GI + MI))

  def test_pod_level_resources_absent_or_null(self):
    self.assertEqual(k.pod_requests({'resources': None, 'containers': []}, {}, 'p'), (0, 0))
    self.assertEqual(k.pod_requests({'resources': {}, 'containers': [container('c', '1')]}, {}, 'p'), (1000, 0))

  RESIZE_SPEC = {'containers': [container('c', '2', '1Gi')]}
  RESIZE_STATUS = {'containerStatuses': [{
    'name': 'c',
    'allocatedResources': {'cpu': '1', 'memory': '2Gi'},
    'resources': {'requests': {'cpu': '1', 'memory': '2Gi'}},
  }]}

  def test_resize_takes_max_of_spec_status_allocated(self):
    self.assertEqual(k.pod_requests(self.RESIZE_SPEC, self.RESIZE_STATUS, 'p'), (2000, 2 * GI))

  def test_infeasible_resize_ignores_spec(self):
    for marker in ({'resize': 'Infeasible'},
                   {'conditions': [{'type': 'PodResizePending', 'reason': 'Infeasible', 'status': 'True'}]}):
      with self.subTest(marker=marker):
        status = dict(self.RESIZE_STATUS, **marker)
        self.assertEqual(k.pod_requests(self.RESIZE_SPEC, status, 'p'), (1000, 2 * GI))

  def test_deferred_resize_is_not_infeasible(self):
    status = dict(self.RESIZE_STATUS,
                  conditions=[{'type': 'PodResizePending', 'reason': 'Deferred', 'status': 'True'}])
    self.assertEqual(k.pod_requests(self.RESIZE_SPEC, status, 'p'), (2000, 2 * GI))

  def test_status_without_resources_uses_spec(self):
    status = {'containerStatuses': [{'name': 'c'}]}
    self.assertEqual(k.pod_requests(self.RESIZE_SPEC, status, 'p'), (2000, GI))

  def test_rounds_pod_total_not_each_container(self):
    # The scheduler sums exact quantities and rounds the pod total up.
    spec = {'containers': [container('a', '0.0005', '500m'), container('b', '0.0005', '250m')],
            'overhead': {'memory': '250m'}}
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (1, 1))
    spec['overhead']['memory'] = '251m'
    self.assertEqual(k.pod_requests(spec, {}, 'p'), (1, 2))

  def test_null_fields(self):
    spec = {'containers': [{'name': 'c', 'resources': {'requests': {'cpu': '1', 'memory': None}}},
                           {'name': 'd', 'resources': None},
                           {'name': 'e', 'resources': {'requests': None}}],
            'initContainers': None, 'overhead': None,
            'resources': {'requests': {'cpu': None, 'memory': '1Gi'}}}
    status = {'conditions': None, 'initContainerStatuses': None,
              'containerStatuses': [{'name': 'c', 'resources': {'requests': None},
                                     'allocatedResources': None}]}
    self.assertEqual(k.pod_requests(spec, status, 'p'), (1000, GI))

  def test_sidecar_resize_but_not_plain_init(self):
    spec = {'initContainers': [container('s', '100m', restartPolicy='Always'),
                               container('i', '50m')],
            'containers': []}
    status = {'initContainerStatuses': [
      {'name': 's', 'resources': {'requests': {'cpu': '300m'}}},
      {'name': 'i', 'resources': {'requests': {'cpu': '5'}}},
    ]}
    self.assertEqual(k.pod_requests(spec, status, 'p'), (350, 0))


class LoadTest(unittest.TestCase):
  def test_load_nodes(self):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
      nodes = k.load_nodes(json.loads(NODES))
    self.assertEqual(stderr.getvalue(), 'warning: node bad has no allocatable resources, counting capacity as 0\n')
    self.assertEqual(sorted(nodes), ['bad', 'n1', 'n2'])
    self.assertEqual(nodes['n1']['cpu'], 3920)
    self.assertEqual(nodes['n1']['maxpods'], 110)
    self.assertTrue(nodes['n1']['unschedulable'])
    self.assertTrue(nodes['n1']['ready'])
    self.assertFalse(nodes['n2']['ready'])
    # No status: kept with zero capacity, like the scheduler sees it.
    self.assertEqual((nodes['bad']['cpu'], nodes['bad']['memory'], nodes['bad']['maxpods']), (0, 0, None))
    self.assertFalse(nodes['bad']['ready'])

  def test_load_null_fields(self):
    nodes = k.load_nodes({'items': [
      {'metadata': {'name': 'n'}, 'spec': None,
       'status': {'allocatable': {'cpu': '1', 'memory': None, 'pods': None}, 'conditions': None}},
      {'metadata': None}]})
    self.assertEqual(list(nodes), ['n'])
    self.assertEqual((nodes['n']['cpu'], nodes['n']['memory'], nodes['n']['maxpods']), (1000, 0, None))
    self.assertFalse(nodes['n']['unschedulable'])
    self.assertEqual(k.load_priorities({'items': [{'metadata': {'name': 'x'}, 'value': None}]}), {})
    pods, left, unknown = k.load_pods({'items': [
      {'metadata': {'namespace': 'a', 'name': 'p'}, 'status': None,
       'spec': {'nodeName': None, 'priority': None, 'priorityClassName': None, 'containers': None}},
      {'metadata': {'namespace': 'a', 'name': 'nospec'}, 'spec': None}]}, nodes, {})
    self.assertEqual(list(pods), ['a/p'])
    self.assertEqual(left, ['a/p'])
    self.assertEqual((pods['a/p']['cpu'], pods['a/p']['priovalue'], pods['a/p']['prioname']), (0, 0, ''))
    self.assertEqual(k.load_nodes({'items': None}), {})

  def test_load_pods(self):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
      nodes = k.load_nodes(json.loads(NODES))
      prios = k.load_priorities(json.loads(PRIORITY_CLASSES))
      pods, left, unknown = k.load_pods(json.loads(PODS), nodes, prios)
    self.assertIn("a/p2[c]: cannot parse cpu quantity 'bogus'", stderr.getvalue())
    self.assertEqual(pods['a/p2']['cpu'], 0)
    self.assertNotIn('a/done', pods)  # Succeeded pods are skipped
    self.assertEqual(nodes['n1']['pods'], ['a/p1', 'a/p2'])
    self.assertEqual(left, ['b/pend'])
    self.assertEqual(unknown, ['b/ghost'])
    self.assertEqual(nodes['bad']['pods'], ['d/onbad'])  # not an unknown node
    self.assertEqual(pods['b/pend']['priovalue'], 1000)  # class lookup fallback
    self.assertEqual(pods['c/podlevel']['cpu'], 2000)


NODES = '''{"items": [
  {"metadata": {"name": "n1"}, "spec": {"unschedulable": true},
   "status": {"allocatable": {"cpu": "3920m", "memory": "16159532Ki", "pods": "110"},
              "conditions": [{"type": "Ready", "status": "True"}]}},
  {"metadata": {"name": "n2"},
   "status": {"allocatable": {"cpu": "2", "memory": "8Gi", "pods": "110"},
              "conditions": [{"type": "Ready", "status": "Unknown"}]}},
  {"metadata": {"name": "bad"}}
]}'''

PRIORITY_CLASSES = '''{"items": [
  {"metadata": {"name": "high"}, "value": 1000},
  {"metadata": {"name": "system-node-critical"}, "value": 2000001000}
]}'''

PODS = '''{"items": [
  {"metadata": {"namespace": "a", "name": "p1"},
   "spec": {"nodeName": "n1", "priorityClassName": "high", "priority": 1000,
            "containers": [{"name": "c", "resources": {"requests": {"cpu": "500m", "memory": "1.5Gi"}}}]},
   "status": {"phase": "Running"}},
  {"metadata": {"namespace": "a", "name": "p2"},
   "spec": {"nodeName": "n1",
            "containers": [{"name": "c", "resources": {"requests": {"cpu": "bogus", "memory": "100M"}}}]},
   "status": {"phase": "Running"}},
  {"metadata": {"namespace": "a", "name": "done"},
   "spec": {"nodeName": "n1", "containers": [{"name": "c", "resources": {"requests": {"cpu": "4"}}}]},
   "status": {"phase": "Succeeded"}},
  {"metadata": {"namespace": "b", "name": "pend"},
   "spec": {"priorityClassName": "high",
            "containers": [{"name": "c", "resources": {"requests": {"cpu": "1", "memory": "2Gi"}}}]},
   "status": {"phase": "Pending"}},
  {"metadata": {"namespace": "b", "name": "ghost"},
   "spec": {"nodeName": "gone", "containers": [{"name": "c"}]},
   "status": {"phase": "Running"}},
  {"metadata": {"namespace": "c", "name": "podlevel"},
   "spec": {"nodeName": "n2", "resources": {"requests": {"cpu": "2", "memory": "4Gi"}},
            "containers": [{"name": "c"}]},
   "status": {"phase": "Running"}},
  {"metadata": {"namespace": "d", "name": "onbad"},
   "spec": {"nodeName": "bad", "containers": [{"name": "c", "resources": {"requests": {"cpu": "100m"}}}]},
   "status": {"phase": "Running"}}
]}'''

# Serves <dir>/<resource>.json for 'kubectl get <resource> ...'; a missing
# file makes it fail like an API error.
FAKE_KUBECTL = '''#!/bin/sh
f="$(dirname "$0")/$2.json"
if [ -f "$f" ]; then cat "$f"; else echo "Error from server (Forbidden): $2 is forbidden" >&2; exit 1; fi
'''


class EndToEndTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    kubectl = os.path.join(self.tmp.name, 'kubectl')
    with open(kubectl, 'w') as f:
      f.write(FAKE_KUBECTL)
    os.chmod(kubectl, 0o755)
    self.fixtures = {'node': NODES, 'pc': PRIORITY_CLASSES, 'pod': PODS}

  def run_kubecheck(self, path=None):
    for name, content in self.fixtures.items():
      with open(os.path.join(self.tmp.name, name + '.json'),
                'wb' if isinstance(content, bytes) else 'w') as f:
        f.write(content)
    if path is None:
      path = self.tmp.name + os.pathsep + os.environ.get('PATH', '')
    env = dict(os.environ, PATH=path)
    return subprocess.run([sys.executable, '-I', os.path.join(HERE, 'kubecheck.py')],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)

  def test_report(self):
    r = self.run_kubecheck()
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn("cannot parse cpu quantity 'bogus'", r.stderr)
    self.assertIn('warning: node bad has no allocatable resources, counting capacity as 0', r.stderr)
    out = r.stdout
    self.assertIn('  a/p1: node=n1 cpu=500m mem=1.5Gi prio=high[1000]', out)
    self.assertNotIn('a/done', out)
    self.assertIn('  n1 [cordoned]: totalcpu=3920m totalmem=~15.411Gi usedcpu=500m '
                  'usedmem=~1.593Gi leftcpu=3420m leftmem=~13.818Gi pods=2/110', out)
    self.assertIn('  n2 [NotReady]: totalcpu=2000m totalmem=8Gi usedcpu=2000m '
                  'usedmem=4Gi leftcpu=0m leftmem=4Gi pods=1/110', out)
    self.assertIn('  bad [NotReady]: totalcpu=0m totalmem=0 usedcpu=100m '
                  'usedmem=0 leftcpu=-100m leftmem=0 pods=1/?\n'
                  '    d/onbad: cpu=100m mem=0 prio=[0]', out)
    self.assertIn('Waiting for scheduling: cpu=1000m mem=2Gi pods=1\n'
                  '  b/pend: cpu=1000m mem=2Gi prio=high[1000]', out)
    self.assertIn('Bound to unknown nodes: cpu=0m mem=0 pods=1\n'
                  '  b/ghost: node=gone cpu=0m mem=0 prio=[0]', out)

  def test_priority_classes_forbidden_is_a_warning(self):
    del self.fixtures['pc']
    r = self.run_kubecheck()
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn("warning: 'kubectl get pc -o json' failed", r.stderr)
    self.assertIn('a/p1: node=n1 cpu=500m mem=1.5Gi prio=high[1000]', r.stdout)  # from spec.priority

  def test_pods_forbidden_is_an_error(self):
    del self.fixtures['pod']
    r = self.run_kubecheck()
    self.assertEqual(r.returncode, 1)
    self.assertIn("error: 'kubectl get pod -A -o json' failed", r.stderr)
    self.assertEqual(r.stdout, '')

  def test_invalid_json_is_an_error(self):
    for bad in ('not json', '[]', b'\xff'):  # last: not UTF-8
      with self.subTest(bad=bad):
        self.fixtures['node'] = bad
        r = self.run_kubecheck()
        self.assertEqual(r.returncode, 1)
        self.assertIn("error: 'kubectl get node -o json' returned invalid JSON:", r.stderr)
        self.assertNotIn('Traceback', r.stderr)
        self.assertEqual(r.stdout, '')

  def test_invalid_priority_class_json_is_a_warning(self):
    self.fixtures['pc'] = 'not json'
    r = self.run_kubecheck()
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn("warning: 'kubectl get pc -o json' returned invalid JSON:", r.stderr)
    self.assertIn('a/p1: node=n1 cpu=500m mem=1.5Gi prio=high[1000]', r.stdout)

  def test_kubectl_missing(self):
    empty = os.path.join(self.tmp.name, 'empty')
    os.mkdir(empty)
    r = self.run_kubecheck(path=empty)
    self.assertEqual(r.returncode, 1)
    self.assertEqual(r.stderr, 'error: kubectl not found on PATH\n')
    self.assertEqual(r.stdout, '')

  @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can execute anything')
  def test_kubectl_not_executable(self):
    os.chmod(os.path.join(self.tmp.name, 'kubectl'), 0o644)
    r = self.run_kubecheck(path=self.tmp.name)
    self.assertEqual(r.returncode, 1)
    self.assertEqual(r.stderr, 'error: cannot run kubectl: Permission denied\n')
    self.assertEqual(r.stdout, '')


if __name__ == '__main__':
  unittest.main()
