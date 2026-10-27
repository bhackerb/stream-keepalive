import unittest
from unittest.mock import AsyncMock,MagicMock
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI
from regular_browser import CatalogUnavailable
import test_regular_browser as fixtures

class CatalogUnavailableTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegularTests.asyncSetUp
    asyncTearDown=fixtures.RegularTests.asyncTearDown

    def fake_client(self,content_type,payload=None):
        res=MagicMock();res.content_type=content_type;res.raise_for_status=MagicMock();res.json=AsyncMock(return_value=payload)
        cm=MagicMock();cm.__aenter__=AsyncMock(return_value=res);cm.__aexit__=AsyncMock(return_value=False)
        client=MagicMock();client.get=MagicMock(return_value=cm);return client

    async def test_html_challenge_page_raises_catalog_unavailable(self):
        self.k._client=self.fake_client('text/html')
        with self.assertRaises(CatalogUnavailable):await self.k._json('matches/live')

    async def test_json_catalog_is_returned(self):
        self.k._client=self.fake_client('application/json',[{'id':'a'}])
        self.assertEqual(await self.k._json('matches/live'),[{'id':'a'}])

    async def test_session_returns_502_with_reason_not_traceback(self):
        self.k._client=self.fake_client('text/html')
        async with TestClient(TestServer(StreamKeeperAPI(self.k,self.config).app)) as client:
            resp=await client.post('/session',json={'action':'plan','sports':['hockey']},headers={'Authorization':'Bearer test-key'})
            body=await resp.json()
        self.assertEqual(resp.status,502)
        self.assertIn('existing streams preserved',body['message'])
        self.assertIn('text/html',body['message'])

if __name__=='__main__':unittest.main()
