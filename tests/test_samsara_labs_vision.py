import unittest

from fastapi.testclient import TestClient

from cloud.main import app


class SamsaraLabsVisionTests(unittest.TestCase):
    def test_public_preview_keeps_full_article_encrypted(self) -> None:
        with TestClient(app) as client:
            locked = client.get("/vision")
            self.assertEqual(locked.status_code, 200)
            self.assertIn("Vision to 100B", locked.text)
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


if __name__ == "__main__":
    unittest.main()
