import unittest

from fastapi.testclient import TestClient

from cloud.main import app


class SamsaraLabsVisionTests(unittest.TestCase):
    def test_private_article_is_served_only_as_ciphertext(self) -> None:
        with TestClient(app) as client:
            locked = client.get("/vision")
            self.assertEqual(locked.status_code, 200)
            self.assertIn("Private field notes", locked.text)
            self.assertIn('id="encrypted-vision"', locked.text)
            self.assertIn('"algorithm":"AES-GCM"', locked.text)
            self.assertNotIn("<article", locked.text.lower())
            self.assertNotIn('class="hero"', locked.text.lower())
            self.assertEqual(locked.headers["cache-control"], "no-store, private")

            old_route = client.get("/samsara-labs-vision")
            self.assertEqual(old_route.status_code, 404)

            exposed = client.get("/static/samsara-labs-vision.html")
            self.assertEqual(exposed.status_code, 404)


if __name__ == "__main__":
    unittest.main()
