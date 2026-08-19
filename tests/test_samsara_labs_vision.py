import base64
import unittest

from fastapi.testclient import TestClient

from cloud.main import app


class SamsaraLabsVisionTests(unittest.TestCase):
    def test_public_preview_keeps_full_article_encrypted(self) -> None:
        with TestClient(app, base_url="https://testserver") as client:
            locked = client.get("/vision")
            self.assertEqual(locked.status_code, 200)
            self.assertIn("Vision: Robotic OS and 100B+", locked.text)
            self.assertIn("August 2026 · Curtis Northcutt", locked.text)
            self.assertIn("Samsara AI Labs ·", locked.text)
            self.assertIn('<span class="brand-projects">Special Projects</span>', locked.text)
            self.assertNotIn("Private draft", locked.text)
            self.assertIn('/static/samsara-labs-logo.svg', locked.text)
            self.assertIn('localStorage.setItem(cookieName, encoded)', locked.text)
            self.assertIn('id="server-unlock-key" type="application/json">null</script>', locked.text)
            self.assertNotIn("__VISION_SERVER_UNLOCK_KEY__", locked.text)
            self.assertIn('id="gate-modal"', locked.text)
            self.assertIn('id="gate-modal" role="dialog"', locked.text)
            self.assertIn('aria-modal="true"', locked.text)
            self.assertIn('inert', locked.text)
            self.assertIn('id="vision-unlock"', locked.text)
            self.assertIn('autocomplete="current-password"', locked.text)
            self.assertIn('enterkeyhint="go"', locked.text)
            self.assertNotIn('data-open-gate', locked.text)
            self.assertIn('window.addEventListener("scroll"', locked.text)
            self.assertIn('id="encrypted-vision"', locked.text)
            self.assertIn('"algorithm":"AES-GCM"', locked.text)
            self.assertNotIn("Vision preview", locked.text)
            self.assertNotIn('class="roadmap-chart"', locked.text.lower())
            self.assertNotIn("Customer value and data", locked.text)
            self.assertEqual(locked.headers["cache-control"], "no-store, private")
            self.assertIn('/static/samsara-labs-vision.css?v=21', locked.text)

            old_route = client.get("/samsara-labs-vision")
            self.assertEqual(old_route.status_code, 404)

            exposed = client.get("/static/samsara-labs-vision.html")
            self.assertEqual(exposed.status_code, 404)

    def test_mobile_shell_has_touch_and_keyboard_safe_layout(self) -> None:
        with TestClient(app, base_url="https://testserver") as client:
            shell = client.get("/vision")
            stylesheet = client.get("/static/samsara-labs-vision.css")

            self.assertEqual(stylesheet.status_code, 200)
            self.assertTrue(stylesheet.headers["content-type"].startswith("text/css"))
            self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', shell.text)

            css = stylesheet.text
            self.assertIn("overflow-y: auto", css)
            self.assertIn("overscroll-behavior: contain", css)
            self.assertIn("grid-template-columns: minmax(0, 1fr) auto", css)
            self.assertIn("font-size: 16px", css)
            self.assertIn("env(safe-area-inset-top)", css)
            self.assertIn("@media (max-height: 520px)", css)
            self.assertIn(".brand-name > span { white-space: nowrap; }", css)
            self.assertIn(".roadmap-mobile {", css)
            self.assertIn(".chart-scroll { display: none; }", css)
            self.assertIn(".bets-curve-chart-mobile { display: block", css)
            self.assertNotIn(".bets-curve-chart { min-width: 560px", css)
            self.assertNotIn(".bets-curve-chart { min-width: 620px", css)

    def test_unlock_key_survives_refresh_and_can_be_cleared(self) -> None:
        unlock_key = base64.b64encode(bytes(range(32))).decode("ascii")
        with TestClient(app, base_url="https://testserver") as client:
            remembered = client.post("/vision/session", json={"key": unlock_key})
            self.assertEqual(remembered.status_code, 204)
            set_cookie = remembered.headers["set-cookie"]
            self.assertIn("__Secure-samcam_vision_unlock=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("Max-Age=31536000", set_cookie)
            self.assertIn("Path=/vision", set_cookie)
            self.assertIn("SameSite=lax", set_cookie)
            self.assertIn("Secure", set_cookie)

            refreshed = client.get("/vision")
            self.assertIn(
                f'id="server-unlock-key" type="application/json">"{unlock_key}"</script>',
                refreshed.text,
            )
            self.assertEqual(refreshed.headers["vary"], "Cookie")

            cleared = client.post("/vision/session/clear")
            self.assertEqual(cleared.status_code, 204)
            locked_again = client.get("/vision")
            self.assertIn('id="server-unlock-key" type="application/json">null</script>', locked_again.text)

    def test_unlock_key_rejects_invalid_values(self) -> None:
        with TestClient(app, base_url="https://testserver") as client:
            self.assertEqual(client.post("/vision/session", json={"key": "not-base64"}).status_code, 400)
            short_key = base64.b64encode(b"too short").decode("ascii")
            self.assertEqual(client.post("/vision/session", json={"key": short_key}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
