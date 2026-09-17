import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import config
import config_manager
from web_server import app


class WebsiteConfigTests(unittest.TestCase):
    def test_dashboard_debug_checkbox_reconfigures_logging_in_both_directions(self):
        with patch.dict(config.__dict__, {"DEBUG": False, "WEB_USERNAME": "test", "WEB_PASSWORD": "test"}), \
                patch("config_manager.setup_logging") as setup, app.test_client() as client:
            response = client.post("/config", data={"debug": "on"}, auth=("test", "test"))
            self.assertEqual(response.status_code, 302)
            self.assertTrue(config.DEBUG)
            self.assertTrue(config_manager.get_config()["debug"])
            response = client.post("/config", data={}, auth=("test", "test"))
            self.assertEqual(response.status_code, 302)
            self.assertFalse(config.DEBUG)
            self.assertEqual(setup.call_count, 2)

    def test_blank_password_preserves_existing_value_without_exposing_it(self):
        with patch.dict(config.__dict__, {"OCTOPUS_LOGIN_PASSWD": "saved-secret"}):
            config_manager.update_config({"octopus_login_email": "user@example.invalid", "octopus_login_passwd": ""})
            self.assertEqual(config.OCTOPUS_LOGIN_PASSWD, "saved-secret")
            self.assertNotIn("saved-secret", str(config_manager.get_config()))
            self.assertTrue(config_manager.get_config()["octopus_login_password_configured"])

    def test_dashboard_saves_password_without_logging_or_rendering_it(self):
        with patch.dict(config.__dict__, {"WEB_USERNAME": "test", "WEB_PASSWORD": "test"}), app.test_client() as client:
            with self.assertLogs("octobot.web_server", level="INFO") as logs:
                response = client.post("/config", data={"octopus_login_email": "user@example.invalid",
                                                       "octopus_login_passwd": "new-secret"}, auth=("test", "test"))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(config.OCTOPUS_LOGIN_PASSWD, "new-secret")
            self.assertNotIn("new-secret", str(logs.output))
            page = client.get("/config", auth=("test", "test"))
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b"new-secret", page.data)


if __name__ == "__main__":
    unittest.main()
