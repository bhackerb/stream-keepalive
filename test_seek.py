"""Hermetic VOD seek control and duration reporting."""
import unittest
from unittest.mock import AsyncMock
import test_audio_control as fixtures


class SeekTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.AudioTests.asyncSetUp(self)
        self.k._frames.clear()

    async def asyncTearDown(self):
        await fixtures.AudioTests.asyncTearDown(self)

    async def test_duration_reaches_status(self):
        await self.k._event('firefox',{'event':'health','stream_id':'game','frame_id':1,'health':{'state':'PLAYING','current_time':5,'width':1920,'height':1080,'volume':1,'stall_seconds':0,'duration':10046.15}})
        self.assertAlmostEqual(self.k.active_streams['game'].health_monitor.stats['duration'],10046.1,delta=.11)
        await self.k._event('firefox',{'event':'health','stream_id':'game','frame_id':1,'health':{'state':'PLAYING','current_time':6,'width':1920,'height':1080,'volume':1,'stall_seconds':0,'duration':None}})
        self.assertIsNone(self.k.active_streams['game'].health_monitor.stats['duration'])

    async def test_relative_and_absolute_seek(self):
        self.k.command=AsyncMock(return_value={'confirmed':True,'current_time':3660,'duration':10046})
        result=await self.k.seek('game',delta=60)
        self.assertEqual(self.k.command.await_args.kwargs,{'stream_id':'game','delta':60.0})
        self.assertEqual(result['message'],'Moved to 01:01:00')
        await self.k.seek('game',to=3600)
        self.assertEqual(self.k.command.await_args.kwargs,{'stream_id':'game','to':3600.0})

    async def test_rejects_ambiguous_or_unconfirmed(self):
        self.k.command=AsyncMock(return_value={'confirmed':False})
        with self.assertRaises(ValueError):await self.k.seek('game')
        with self.assertRaises(ValueError):await self.k.seek('game',to=1,delta=1)
        with self.assertRaises(ValueError):await self.k.seek('game',to=-5)
        with self.assertRaises(ValueError):await self.k.seek('game',delta=60)


if __name__=='__main__':unittest.main()
