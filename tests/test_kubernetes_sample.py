# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Keep the sample Kubernetes deployment's load-bearing choices in place.

``deploy/kubernetes.yaml`` is documentation as much as configuration;
``docs/setup.md`` ("Run on Kubernetes") explains each of these choices.
The checks read the text, so they need no YAML library.
"""

from pathlib import Path
import re
import unittest

SAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "kubernetes.yaml"


class KubernetesSampleTests(unittest.TestCase):
    """Check the sample deployment's text."""

    @classmethod
    def setUpClass(cls):
        cls.text = SAMPLE.read_text()

    def test_one_pod_replaced_not_rolled(self):
        # Both processes write one SQLite file: never two pods at once.
        self.assertIn("  replicas: 1\n", self.text)
        self.assertIn("    type: Recreate\n", self.text)

    def test_config_is_written_before_both_containers(self):
        # Only the init container writes the first config, so the two
        # containers never race; the web container migrates behind its
        # holding page (labrat/startup.py), so no init container does.
        self.assertRegex(self.text, r"initContainers:\n\s+- name: config\n.*\n\s+args: \[\"true\"\]")
        self.assertNotIn('"upgrade", "head"', self.text)

    def test_probes_answer_while_starting(self):
        # /api/v1/health is 503 until ready; probing it would restart the
        # pod during a long migration and hide the holding page.
        probes = re.findall(r"(?:readiness|liveness)Probe:\n(?: {12,}.*\n)+", self.text)
        for probe in probes:
            self.assertIn("path: /api/v1/live\n", probe)

    def test_probes_name_an_allowed_host(self):
        # The kubelet probes by pod address, which allowed_hosts refuses.
        probes = re.findall(r"(?:readiness|liveness)Probe:\n(?: {12,}.*\n)+", self.text)
        self.assertEqual(len(probes), 2)
        for probe in probes:
            self.assertIn("- name: Host\n", probe)
            self.assertIn("value: localhost\n", probe)

    def test_every_container_runs_the_same_image(self):
        images = re.findall(r"^\s+image: (\S+)$", self.text, re.MULTILINE)
        self.assertEqual(len(images), 3)
        self.assertEqual(len(set(images)), 1)

    def test_collector_keeps_its_default_interval(self):
        self.assertIn('args: ["collector"]\n', self.text)

    def test_host_network_is_opt_in(self):
        self.assertNotRegex(self.text, r"(?m)^\s+hostNetwork:")
        self.assertIn("# hostNetwork: true\n", self.text)


if __name__ == "__main__":
    unittest.main()
