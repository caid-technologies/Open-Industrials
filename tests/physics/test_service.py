import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp

from tools.geospatial.export_scene import write_json
from tools.physics.demo import fixture
from tools.physics.export import build_bundle


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        scene, config = fixture()
        write_json(cls.root/'scene.json',scene);write_json(cls.root/'physics.json',config)
        build_bundle(cls.root/'scene.json',cls.root/'physics.json',cls.root/'bundle')

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    async def asyncSetUp(self):
        self.run = tempfile.TemporaryDirectory(dir=self.root)
        self.db = Path(self.run.name)/'runtime.sqlite'
        with socket.socket() as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.url=f'http://127.0.0.1:{self.port}'
        self.session=aiohttp.ClientSession()
        self.process=None
        await self.start()

    async def start(self):
        self.process=subprocess.Popen([sys.executable,'-m','tools.physics.service',str(self.root/'bundle'),'--database',str(self.db),'--port',str(self.port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(150):
            if self.process.poll() is not None:raise AssertionError('Physics service failed to start')
            try:
                async with self.session.get(self.url+'/physics/api/status') as response:
                    if response.status==200:return
            except aiohttp.ClientError:pass
            await asyncio.sleep(.05)
        raise AssertionError('Service startup timeout')

    async def asyncTearDown(self):
        if self.process and self.process.poll() is None:self.process.kill();self.process.wait()
        await self.session.close();self.run.cleanup()

    async def status(self):
        async with self.session.get(self.url+'/physics/api/status') as response:return await response.json()

    async def command(self,request):
        async with self.session.post(self.url+'/physics/api/commands',json=request,headers={'X-OI-Physics':'1','Origin':self.url}) as response:
            self.assertEqual(response.status,200,await response.text());return await response.json()

    async def test_autonomous_progress_hard_restart_and_replay(self):
        request={'id':'run','expectedRevision':0,'action':'play'}
        first=await self.command(request)
        await asyncio.sleep(.3)
        before=await self.status();self.assertGreater(before['step'],50)
        self.process.kill();self.process.wait()
        await self.start()
        self.assertEqual(first,await self.command(request))
        await asyncio.sleep(.2)
        after=await self.status();self.assertGreater(after['step'],before['step'])
        paused=await self.command({'id':'pause','expectedRevision':1,'action':'pause'})
        await asyncio.sleep(.1)
        self.assertEqual((await self.status())['step'],paused['step'])
        async with self.session.post(self.url+'/physics/api/replay',json={'start':0,'end':paused['step']},headers={'X-OI-Physics':'1'}) as response:
            self.assertTrue((await response.json())['passed'])
        async with self.session.ws_connect(self.url+'/physics/api/stream',origin=self.url) as ws:
            packet=await ws.receive();self.assertEqual(packet.type,aiohttp.WSMsgType.BINARY);self.assertEqual(packet.data[:4],b'OIP1')

    async def test_origin_host_paths_and_single_worker(self):
        async with self.session.post(self.url+'/physics/api/commands',json={'id':'bad'},headers={'X-OI-Physics':'1','Origin':'https://untrusted.example'}) as r:self.assertEqual(r.status,403)
        async with self.session.post(self.url+'/physics/api/commands',json={'id':'bad'}) as r:self.assertEqual(r.status,403)
        async with self.session.get(self.url+'/physics/api/status',headers={'Host':'untrusted.example'}) as r:self.assertEqual(r.status,403)
        async with self.session.get(self.url+'/physics/bundle/model.xml') as r:self.assertEqual(r.status,404)
        async with self.session.get(self.url+'/physics/bundle/runtime.sqlite') as r:self.assertEqual(r.status,404)
        second=await asyncio.create_subprocess_exec(sys.executable,'-m','tools.physics.service',str(self.root/'bundle'),'--database',str(self.db),'--port',str(self.port+1 if self.port<65535 else self.port-1),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        _,err=await asyncio.wait_for(second.communicate(),10)
        self.assertNotEqual(second.returncode,0);self.assertIn(b'Another physics worker',err)


if __name__=='__main__':unittest.main()
