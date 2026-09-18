from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PUBLIC_ORIGIN = "http://111.231.136.4"
INSECURE_PRODUCTION_ORIGIN = "http://sokobanaidemo.top"
PRODUCTION_ORIGIN = "https://sokobanaidemo.top"
RELEASE_KEY = "iframe-host-v5-20260918-2"


class PublicEndpointConfigurationTests(unittest.TestCase):
    def test_runtime_sources_do_not_embed_legacy_public_origin(self):
        targets = [
            REPOSITORY_ROOT / "Assets" / "Scripts",
            REPOSITORY_ROOT / "Assets" / "WebGLTemplates" / "SokobanPixel",
            REPOSITORY_ROOT / "Frontend",
            REPOSITORY_ROOT / "CoCreationPrototype" / "Frontend",
            REPOSITORY_ROOT / "CoCreationPrototype" / "Backend" / "app.py",
        ]
        offenders = []

        for target in targets:
            paths = [target] if target.is_file() else target.rglob("*")
            for path in paths:
                if not path.is_file() or path.suffix.lower() not in {
                    ".cs",
                    ".html",
                    ".js",
                    ".py",
                }:
                    continue
                if LEGACY_PUBLIC_ORIGIN in path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ):
                    offenders.append(str(path.relative_to(REPOSITORY_ROOT)))

        self.assertEqual([], offenders)

    def test_runtime_sources_do_not_embed_insecure_production_origin(self):
        targets = [
            REPOSITORY_ROOT / "Assets" / "Scripts",
            REPOSITORY_ROOT / "Assets" / "WebGLTemplates" / "SokobanPixel",
            REPOSITORY_ROOT / "Frontend",
            REPOSITORY_ROOT / "CoCreationPrototype" / "Frontend",
            REPOSITORY_ROOT / "CoCreationPrototype" / "Backend" / "app.py",
        ]
        offenders = []

        for target in targets:
            paths = [target] if target.is_file() else target.rglob("*")
            for path in paths:
                if not path.is_file() or path.suffix.lower() not in {
                    ".cs",
                    ".html",
                    ".js",
                    ".py",
                }:
                    continue
                if INSECURE_PRODUCTION_ORIGIN in path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ):
                    offenders.append(str(path.relative_to(REPOSITORY_ROOT)))

        self.assertEqual([], offenders)

    def test_unity_resolver_uses_browser_origin_and_https_fallback(self):
        source = (
            REPOSITORY_ROOT
            / "Assets"
            / "Scripts"
            / "Networking"
            / "PublicEndpointResolver.cs"
        ).read_text(encoding="utf-8")

        self.assertIn("Application.absoluteURL", source)
        self.assertIn(PRODUCTION_ORIGIN, source)
        self.assertIn("Uri.UriSchemeHttp", source)
        self.assertIn("Uri.UriSchemeHttps", source)
        self.assertIn("ResolveOriginForPageUrl", source)
        self.assertIn("LegacyPublicHost", source)
        self.assertIn("NormalizeProductionScheme", source)

    def test_frontend_release_keys_are_kept_in_sync(self):
        cocreation_index = (
            REPOSITORY_ROOT
            / "CoCreationPrototype"
            / "Frontend"
            / "index.html"
        ).read_text(encoding="utf-8")
        webgl_template = (
            REPOSITORY_ROOT
            / "Assets"
            / "WebGLTemplates"
            / "SokobanPixel"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn(RELEASE_KEY, cocreation_index)
        self.assertIn(RELEASE_KEY, webgl_template)

    def test_nginx_preserves_cloudflare_https_and_redirects_insecure_requests(self):
        nginx = (
            REPOSITORY_ROOT
            / "CoCreationPrototype"
            / "Deployment"
            / "nginx-sokoban.conf"
        ).read_text(encoding="utf-8")

        self.assertIn("server_name sokobanaidemo.top;", nginx)
        self.assertIn("server_name www.sokobanaidemo.top;", nginx)
        self.assertIn("server_name 111.231.136.4 _;", nginx)
        self.assertIn("return 308 https://sokobanaidemo.top$request_uri;", nginx)
        self.assertIn("$http_x_forwarded_proto", nginx)
        self.assertIn("proxy_set_header X-Forwarded-Proto $sokoban_public_proto;", nginx)
        self.assertIn("proxy_read_timeout 320s;", nginx)
        self.assertNotIn("listen 443", nginx)
        self.assertNotIn("Strict-Transport-Security", nginx)


if __name__ == "__main__":
    unittest.main()
