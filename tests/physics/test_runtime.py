import copy
import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

import mujoco
import numpy as np

from tools.geospatial.export_scene import write_json
from tools.physics.demo import fixture
from tools.physics.export import build_bundle
from tools.physics.runtime import Runtime, Conflict, SIGNATURE


class PhysicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        scene, config = fixture()
        # Nonzero, rotated instance origin must survive both representations.
        scene['instances'][0]['position'] = [2,0,-1]
        scene['instances'][0]['rotation'] = [0,35,0]
        write_json(cls.root/'scene.json',scene);write_json(cls.root/'physics.json',config)
        cls.manifest = build_bundle(cls.root/'scene.json',cls.root/'physics.json',cls.root/'bundle')
        cls.scene = scene

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.dbdir = tempfile.TemporaryDirectory(dir=self.root)
        self.path = Path(self.dbdir.name)/'run.sqlite'
        self.runtime = Runtime(self.root/'bundle',self.path)

    def tearDown(self):
        self.runtime.close();self.dbdir.cleanup()

    def command(self, action, **kwargs):
        rev=self.runtime.status()['revision']
        return self.runtime.command({'id':f'cmd-{rev}','expectedRevision':rev,'action':action,**kwargs})

    def test_origin_and_separate_collision_hulls(self):
        r=self.runtime
        self.assertEqual(len(r.body_ids),3)
        for index, instance in enumerate(self.scene['instances']):
            np.testing.assert_allclose(r.data.xpos[r.body_ids[index]],instance['position'],atol=1e-12)
        # Internal MuJoCo mesh recentering is compensated by geom transforms.
        model=r.model
        for geom in range(1,model.ngeom):
            mid=model.geom_dataid[geom]
            addr,n=model.mesh_vertadr[mid],model.mesh_vertnum[mid]
            points=model.mesh_vert[addr:addr+n] @ r.data.geom_xmat[geom].reshape(3,3).T + r.data.geom_xpos[geom]
            self.assertLess(np.max(np.abs(points)),5)  # provenance offset 12/34/56 never reapplied
            body=model.geom_bodyid[geom]
            index=r.body_ids.index(body)
            # CoACD is approximate; test MuJoCo's frame against the exact emitted
            # hull, not the original surface (which CoACD may move slightly).
            mesh_name=mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_MESH,mid)
            local=np.array([[float(v) for v in line.split()[1:]] for line in (self.root/'bundle/collision'/f'{mesh_name}.obj').read_text().splitlines() if line.startswith('v ')])
            expected=local @ r.data.xmat[body].reshape(3,3).T + r.data.xpos[body]
            np.testing.assert_allclose(points.min(axis=0),expected.min(axis=0),atol=1e-6)
            np.testing.assert_allclose(points.max(axis=0),expected.max(axis=0),atol=1e-6)
        packet=r.packet();magic,version,step,count,time=struct.unpack('<4sIIId',packet[:24])
        self.assertEqual((magic,version,step,count,time),(b'OIP1',1,0,3,0))
        self.assertEqual(len(packet),24+3*7*4)
        np.testing.assert_allclose(np.frombuffer(packet[24:],dtype='<f4').reshape(-1,7)[0,:3],[2,0,-1])
        self.assertEqual(json.loads((self.root/'bundle/tileset.json').read_text())['asset']['version'],'1.1')
        self.assertTrue(all((self.root/'bundle'/f'collision/{h}.obj').is_file() for x in self.manifest['instances'] for h in x['hulls']))

    def test_float64_replay_restart_controls_and_duplicates(self):
        r=self.runtime
        request={'id':'first','expectedRevision':0,'action':'control','values':[3.12500000001],'label':'Transfer specimen'}
        receipt=r.command(request)
        self.assertEqual(receipt,r.command(request))
        self.command('step',steps=120)
        self.command('control',values=[-1.75])
        self.command('step',steps=200)
        before=r.packet()
        self.assertTrue(r.replay()['passed'])
        self.assertTrue(r.replay(73,290)['passed'])
        state=r.db.execute('SELECT state FROM frames WHERE step=320').fetchone()[0]
        self.assertEqual(len(state),mujoco.mj_stateSize(r.model,SIGNATURE)*8)
        r.close();self.runtime=r=Runtime(self.root/'bundle',self.path)
        self.assertEqual(before,r.packet())
        self.assertEqual(receipt,r.command(request))
        self.command('step',steps=80)
        self.assertTrue(r.replay()['passed'])
        self.assertGreater(float(r.data.time),.79)
        self.assertNotEqual(r.packet(0),r.packet())

    def test_stale_controls_bad_input_and_transaction_rollback(self):
        r=self.runtime
        self.command('control',values=[2])
        with self.assertRaises(Conflict):r.command({'id':'stale','expectedRevision':0,'action':'play'})
        with self.assertRaises(Conflict):r.command({'id':'cmd-0','expectedRevision':0,'action':'pause'})
        for value in ([float('nan')],[1000],[True],[]):
            with self.assertRaises(ValueError):self.command('control',values=value)
        r.db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.command('step',steps=100)
        self.assertEqual(r.status()['step'],0)
        self.assertEqual(r.db.execute('SELECT count(*) FROM frames').fetchone()[0],1)
        r.db.execute('DROP TRIGGER fail_receipt')
        self.command('step',steps=100)
        self.assertTrue(r.replay()['passed'])

    def test_two_connections_reconcile_and_recording_integrity(self):
        second=Runtime(self.root/'bundle',self.path)
        try:
            self.command('step',steps=20)
            with self.assertRaises(Conflict):second.command({'id':'other','expectedRevision':0,'action':'step','steps':1})
            second.command({'id':'other','expectedRevision':1,'action':'step','steps':20})
            self.assertEqual(self.runtime.status()['step'],40)
            self.assertTrue(self.runtime.replay()['passed'])
            recorded = self.runtime.db.execute('SELECT state FROM frames WHERE step=20').fetchone()[0]
            self.runtime.db.execute("UPDATE frames SET state=? WHERE step=20", (b'corrupt',))
            self.assertFalse(self.runtime.replay()['passed'])
            self.runtime.db.execute("UPDATE frames SET state=? WHERE step=20", (recorded,))
            self.runtime.db.execute("UPDATE frames SET state_hash='corrupt' WHERE step=20")
            self.assertFalse(self.runtime.replay()['passed'])
        finally:second.close()

    def test_bundle_mutation_and_unknown_engine_are_rejected(self):
        manifest=self.root/'bundle/manifest.json';raw=manifest.read_bytes()
        try:
            d=json.loads(raw);d['engine']['mujoco']='0.0';write_json(manifest,d)
            with self.assertRaises(ValueError):Runtime(self.root/'bundle',self.path)
        finally:manifest.write_bytes(raw)

    def test_explicit_physics_and_immutable_output(self):
        with self.assertRaises(ValueError):build_bundle(self.root/'scene.json',self.root/'physics.json',self.root/'bundle')
        config=json.loads((self.root/'physics.json').read_text());config['instances']['sample'].pop('mass')
        write_json(Path(self.dbdir.name)/'bad.json',config)
        with self.assertRaises(ValueError):build_bundle(self.root/'scene.json',Path(self.dbdir.name)/'bad.json',Path(self.dbdir.name)/'bad-bundle')
        self.assertFalse((Path(self.dbdir.name)/'bad-bundle').exists())

    def test_concave_asset_keeps_multiple_hulls_and_open_notch(self):
        scene,config=fixture()
        scene['instances']=scene['instances'][:1];scene['bundledAssets']=scene['bundledAssets'][:1];scene['assets']=scene['assets'][:1]
        config['instances']={'bench':config['instances']['bench']}
        ring=[[-1,0],[1,0],[1,1],[0,1],[0,2],[-1,2]]
        vertices=[[x,y,z] for y in [0,.5] for x,z in ring]
        bottom=[[0,1,3],[1,2,3],[0,3,5],[3,4,5]]
        triangles=bottom+[[a+6,c+6,b+6] for a,b,c in bottom]
        for i in range(6):
            j=(i+1)%6;triangles.extend([[i,i+6,j],[j,i+6,j+6]])
        part=scene['bundledAssets'][0]['parts'][0]
        part['vertices']=np.asarray(vertices).flatten().tolist();part['indices']=np.asarray(triangles).flatten().tolist()
        folder=Path(self.dbdir.name)
        write_json(folder/'concave.json',scene);write_json(folder/'concave-config.json',config)
        manifest=build_bundle(folder/'concave.json',folder/'concave-config.json',folder/'concave')
        self.assertGreaterEqual(len(manifest['instances'][0]['hulls']),2)
        model=mujoco.MjModel.from_xml_path(str(folder/'concave/model.xml'));data=mujoco.MjData(model);mujoco.mj_forward(model,data)
        geom=np.array([-1],dtype=np.int32)
        # A downward ray through the missing upper-right quadrant reaches the floor.
        distance=mujoco.mj_ray(model,data,np.array([.75,2,1.75]),np.array([0,-1.,0]),None,1,-1,geom)
        self.assertAlmostEqual(distance,2,places=5)
        self.assertEqual(int(geom[0]),0)


if __name__=='__main__':unittest.main()
