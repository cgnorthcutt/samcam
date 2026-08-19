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
            self.assertIn("Samsara AI Labs · Special Projects", locked.text)
            self.assertNotIn("Private draft", locked.text)
            self.assertIn('/static/samsara-labs-logo.svg', locked.text)
            self.assertIn('localStorage.setItem(cookieName, encoded)', locked.text)
            self.assertIn('id="server-unlock-key" type="application/json">null</script>', locked.text)
            self.assertNotIn("__VISION_SERVER_UNLOCK_KEY__", locked.text)
            self.assertIn('id="gate-modal"', locked.text)
            self.assertIn('id="vision-unlock"', locked.text)
            self.assertIn('id="encrypted-vision"', locked.text)
            self.assertIn('"algorithm":"AES-GCM"', locked.text)
            self.assertNotIn("Vision preview", locked.text)
            self.assertNotIn('class="roadmap-chart"', locked.text.lower())
            self.assertNotIn("Customer value and data", locked.text)
            self.assertEqual(locked.headers["cache-control"], "no-store, private")

            old_route = client.get("/samsara-labs-vision")
            self.assertEqual(old_route.status_code, 404)

            exposed = client.get("/static/samsara-labs-vision.html")
            self.assertEqual(exposed.status_code, 404)

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
