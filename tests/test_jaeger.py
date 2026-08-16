import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run


class JaegerConfigTests(unittest.TestCase):
    def test_generated_config_preserves_source_and_adds_tracing(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "config.yaml").write_text(
                "# header\nconfig:\n  adminAddr: off\nllm: {}\n"
            )

            name = run.write_jaeger_config(config_dir, "http://10.0.0.2:4317")

            self.assertEqual(name, run.JAEGER_CONFIG_FILE)
            self.assertEqual(
                (config_dir / name).read_text(),
                "# header\nconfig:\n"
                "  tracing:\n"
                "    otlpEndpoint: http://10.0.0.2:4317\n"
                "    randomSampling: true\n"
                "  adminAddr: off\nllm: {}\n",
            )


class ContainerJaegerTests(unittest.TestCase):
    def test_endpoint_uses_runtime_address_without_optional_dns(self):
        backend = run.ContainerBackend()
        backend._list = mock.Mock(
            return_value=[
                {
                    "configuration": {"id": run.JAEGER_NAME},
                    "status": {"networks": [{"ipv4Address": "192.168.64.3/24"}]},
                }
            ]
        )

        self.assertEqual(backend.jaeger_endpoint(), "http://192.168.64.3:4317")

    def test_endpoint_brackets_ipv6_address(self):
        backend = run.ContainerBackend()
        backend._list = mock.Mock(
            return_value=[
                {
                    "configuration": {"id": run.JAEGER_NAME},
                    "status": {"networks": [{"ipv6Address": "fd00::3/64"}]},
                }
            ]
        )

        self.assertEqual(backend.jaeger_endpoint(), "http://[fd00::3]:4317")


if __name__ == "__main__":
    unittest.main()
