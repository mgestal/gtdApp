import unittest
from unittest.mock import MagicMock, patch

import app as application


class OAuthRedirectTests(unittest.TestCase):
    def test_derives_current_https_host_and_script_root(self):
        with application.app.test_request_context(
            "/admin", base_url="https://example.test/gtdApp/"
        ):
            self.assertEqual(
                application.google_oauth_redirect_uri({}),
                "https://example.test/gtdApp/admin/google_oauth/callback",
            )

    def test_explicit_and_invalid_redirects(self):
        with application.app.test_request_context(
            "/admin", base_url="https://example.test/gtdApp/"
        ):
            def configured(uri):
                return {"app": {"google_oauth": {"redirect_uri": uri}}}

            valid = "https://example.test/gtdApp/admin/google_oauth/callback"
            self.assertEqual(application.google_oauth_redirect_uri(configured(valid)), valid)
            for uri in (
                "http://example.test/gtdApp/admin/google_oauth/callback",
                "https://other.test/gtdApp/admin/google_oauth/callback",
                "https://example.test/admin/google_oauth/callback",
                valid + "?x=1",
                "https://user@example.test/gtdApp/admin/google_oauth/callback",
            ):
                with self.subTest(uri=uri), self.assertRaises(ValueError):
                    application.google_oauth_redirect_uri(configured(uri))

    def test_callback_reuses_exact_session_redirect_uri(self):
        redirect_uri = "https://example.test/gtdApp/admin/google_oauth/callback"
        mock_flow = MagicMock()
        mock_flow.credentials.to_json.return_value = "{}"
        with application.app.test_request_context(
            "/admin/google_oauth/callback?state=state123&code=abc",
            base_url="http://example.test/gtdApp/",
        ):
            application.session["google_oauth_state"] = "state123"
            application.session["google_oauth_redirect_uri"] = redirect_uri
            with patch.object(application, "_build_admin_google_flow", return_value=mock_flow) as build, \
                 patch.object(application, "gmail_token_path") as token_path, \
                 patch.object(application.os, "chmod"), \
                 patch.object(application, "admin_required", return_value=True):
                application.admin_google_oauth_callback()
                build.assert_called_once_with(redirect_uri)
                mock_flow.fetch_token.assert_called_once_with(
                    authorization_response=redirect_uri + "?state=state123&code=abc"
                )
                token_path.return_value.write_text.assert_called_once()

    def test_oauth_start_preserves_redirect_for_callback(self):
        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = ("https://accounts.example/authorize", "state123")
        mock_flow.code_verifier = "verifier123"
        with application.app.test_request_context(
            "/admin", base_url="https://example.test/gtdApp/",
            method="POST", data={"action": "renew_google_token"},
        ):
            with patch.object(application, "admin_required", return_value=True), \
                 patch.object(application, "gmail_credentials_path") as credentials, \
                 patch.object(application, "load_config", return_value={}), \
                 patch.object(application, "_build_admin_google_flow", return_value=mock_flow) as build:
                credentials.return_value.exists.return_value = True
                response = application.admin()
                redirect_uri = "https://example.test/gtdApp/admin/google_oauth/callback"
                build.assert_called_once_with(redirect_uri)
                self.assertEqual(response.location, "https://accounts.example/authorize")
                self.assertEqual(application.session["google_oauth_redirect_uri"], redirect_uri)
                self.assertEqual(application.session["google_oauth_state"], "state123")


if __name__ == "__main__":
    unittest.main()
