#!/usr/bin/env python3
"""Browser-launched native host. Credentials stay here, never in the extension."""
import asyncio
import json
import struct
import sys
from pathlib import Path
import aiohttp
import yaml
from build_companion import extension_id

ROOT=Path(__file__).resolve().parent
GECKO_ID='streamkeeper@bhackerb.local'
NAME='com.bhackerb.streamkeeper'
MAX_MESSAGE=256*1024

def read_message(stream):
    header=stream.read(4)
    if not header:return None
    if len(header)!=4:raise ValueError('Truncated native header')
    size=struct.unpack('=I',header)[0]
    if not 0<size<=MAX_MESSAGE:raise ValueError('Native message too large')
    data=stream.read(size)
    if len(data)!=size:raise ValueError('Truncated native message')
    value=json.loads(data)
    if not isinstance(value,dict):raise ValueError('Expected native object')
    return value

def write_message(stream,value):
    data=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
    if len(data)>MAX_MESSAGE:raise ValueError('Native reply too large')
    stream.write(struct.pack('=I',len(data))+data);stream.flush()

def launch_allowed(args):
    chrome='chrome-extension://'+extension_id((ROOT/'companion'/'public-key.txt').read_text().strip())+'/'
    # Firefox passes native-manifest path followed by Gecko extension ID.
    return args==[chrome] or (len(args)==2 and Path(args[0]).name==NAME+'.json' and args[1]==GECKO_ID)

async def run():
    if not launch_allowed(sys.argv[1:]):raise ValueError('Unrecognized browser extension')
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    api=config.get('api',{});key=api.get('api_key','')
    if not key or key=='YOUR_API_KEY':raise ValueError('Native bridge requires configured API authentication')
    url=f"http://127.0.0.1:{int(api.get('port',8890))}/browser/bridge"
    reader=asyncio.StreamReader()
    protocol=asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda:protocol,sys.stdin.buffer)
    async def read_async():
        try:header=await reader.readexactly(4)
        except asyncio.IncompleteReadError:return None
        size=struct.unpack('=I',header)[0]
        if not 0<size<=MAX_MESSAGE:raise ValueError('Native message too large')
        value=json.loads(await reader.readexactly(size))
        if not isinstance(value,dict):raise ValueError('Expected native object')
        return value
    # Disconnect exits. The browser owns retry/backoff, avoiding orphan children.
    async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+key}) as session:
        async with session.ws_connect(url,max_msg_size=MAX_MESSAGE) as ws:
            async def from_browser():
                while True:
                    value=await read_async()
                    if value is None:return
                    await ws.send_json(value)
            async def to_browser():
                async for message in ws:
                    if message.type==aiohttp.WSMsgType.TEXT:write_message(sys.stdout.buffer,json.loads(message.data))
            tasks=[asyncio.create_task(from_browser()),asyncio.create_task(to_browser())]
            done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in pending:task.cancel()
            for task in done:task.result()

if __name__=='__main__':
    try:asyncio.run(run())
    except Exception as exc:
        print(type(exc).__name__+': native bridge disconnected',file=sys.stderr)
