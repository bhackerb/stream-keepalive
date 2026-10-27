import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from api_server import StreamKeeperAPI

KEY = 'test-key'


class ReloadStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.keeper = MagicMock()
        self.config = {'api': {'enabled': True, 'api_key': KEY}}

    async def post_reload(self):
        async with TestClient(TestServer(StreamKeeperAPI(self.keeper, self.config).app)) as client:
            response = await client.post('/reload', json={}, headers={'Authorization': f'Bearer {KEY}'})
            return response.status, await response.json()

    async def test_disconnected_companion_is_a_conflict_not_a_server_error(self):
        self.keeper.force_reload = AsyncMock(side_effect=ValueError('Install/connect the StreamKeeper companion in regular firefox first'))
        status, body = await self.post_reload()
        self.assertEqual(status, 409)
        self.assertFalse(body['success'])
        self.assertIn('companion', body['message'])

    async def test_command_timeout_is_a_gateway_timeout(self):
        self.keeper.force_reload = AsyncMock(side_effect=TimeoutError())
        status, _ = await self.post_reload()
        self.assertEqual(status, 504)

    async def test_successful_reload_is_unchanged(self):
        self.keeper.force_reload = AsyncMock(return_value='Explicit reload requested')
        status, body = await self.post_reload()
        self.assertEqual(status, 200)
        self.assertEqual(body['message'], 'Explicit reload requested')


if __name__ == '__main__':
    unittest.main()
