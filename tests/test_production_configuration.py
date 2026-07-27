import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductionConfigurationTests(unittest.TestCase):
    def test_render_enables_https_session_cookie_and_one_proxy_hop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env.pop("SESSION_COOKIE_SECURE", None)
            env.update(
                {
                    "APP_ENV": "production",
                    "NGSS_DB": str(Path(temp_dir) / "production-config.db"),
                    "RENDER": "true",
                    "SECRET_KEY": "production-config-test-secret",
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json;"
                        "from app_core.dashboard_v5 import app;"
                        "from werkzeug.middleware.proxy_fix import ProxyFix;"
                        "print('CONFIG=' + json.dumps({"
                        "'secure': app.config['SESSION_COOKIE_SECURE'],"
                        "'httponly': app.config['SESSION_COOKIE_HTTPONLY'],"
                        "'samesite': app.config['SESSION_COOKIE_SAMESITE'],"
                        "'proxy_fix': isinstance(app.wsgi_app, ProxyFix)"
                        "}))"
                    ),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            config_line = next(
                line for line in result.stdout.splitlines() if line.startswith("CONFIG=")
            )
            config = json.loads(config_line.removeprefix("CONFIG="))

        self.assertEqual(
            config,
            {
                "secure": True,
                "httponly": True,
                "samesite": "Lax",
                "proxy_fix": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
