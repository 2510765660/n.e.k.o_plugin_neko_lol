import unittest
from unittest.mock import patch

from neko_lol import live_client


class TestLiveClient(unittest.IsolatedAsyncioTestCase):
    def test_only_accepts_the_local_game_endpoint(self):
        for url in (
            "https://example.invalid:2999/liveclientdata",
            "https://127.0.0.1:80/liveclientdata",
            "https://127.0.0.1:2999/other",
            "https://user@127.0.0.1:2999/liveclientdata",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                live_client.LiveClient(url)
        client = live_client.LiveClient("https://localhost:2999/liveclientdata/")
        self.assertEqual(client.base_url, live_client.DEFAULT_BASE_URL)

    async def test_redirect_does_not_retry_or_follow_location(self):
        client = live_client.LiveClient()
        with patch.object(live_client.http.client, "HTTPSConnection") as https:
            with patch.object(live_client.http.client, "HTTPConnection") as http:
                conn = https.return_value
                response = conn.getresponse.return_value.__enter__.return_value
                response.status = 302
                with self.assertRaisesRegex(live_client.NotInGame, "HTTP 302"):
                    await client.all_game_data()

                http.assert_not_called()
                https.assert_called_once()
                conn.request.assert_called_once_with("GET", "/liveclientdata/allgamedata")
                conn.close.assert_called_once()

    async def test_snapshot_uses_http_fallback_only_after_connection_failure(self):
        client = live_client.LiveClient()
        match = {"gameData": {"gameTime": 42}}
        with patch.object(live_client.http.client, "HTTPSConnection") as https:
            https.return_value.request.side_effect = ConnectionRefusedError
            with patch.object(live_client.http.client, "HTTPConnection") as http:
                conn = http.return_value
                response = conn.getresponse.return_value.__enter__.return_value
                response.status = 200
                response.read.side_effect = [
                    b'{"Events": [{"EventID": 1}, null]}',
                    b'{"gameData": {"gameTime": 42}}',
                ]
                self.assertEqual(await client.snapshot(), ([{"EventID": 1}], match))
                self.assertEqual(client.scheme, "http")
                https.assert_called_once()
                self.assertEqual(
                    [call.args for call in conn.request.call_args_list],
                    [("GET", "/liveclientdata/eventdata"), ("GET", "/liveclientdata/allgamedata")],
                )

    async def test_invalid_json_is_reported_as_unavailable(self):
        client = live_client.LiveClient(live_client.HTTP_BASE_URL)
        with patch.object(live_client.http.client, "HTTPConnection") as http:
            response = http.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"<html>"
            with self.assertRaisesRegex(live_client.NotInGame, "JSON"):
                await client.all_game_data()
