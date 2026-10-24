"""Generate an openly authored three-body work-cell fixture (no downloaded CAD)."""
import argparse
import json
from pathlib import Path
from tools.geospatial.export_scene import digest, write_json
from tools.physics.export import build_bundle


def fixture():
    vertices = [-.5,0,-.5, .5,0,-.5, .5,1,-.5, -.5,1,-.5, -.5,0,.5, .5,0,.5, .5,1,.5, -.5,1,.5]
    faces = [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,3,7,6,3,6,2,0,4,7,0,7,3,1,2,6,1,6,5]
    assets = []
    for name, dims, color in [('bench',[3,.2,2],[.3,.5,.6]),('carrier',[.8,.2,.8],[.3,.85,.65]),('sample',[.25,.25,.25],[.95,.6,.2])]:
        points = [v*dims[i%3] for i,v in enumerate(vertices)]
        source = {'kind':'generated','filename':name+'.json','digest':digest(json.dumps(points).encode())}
        assets.append({'id':name,'name':name.title(),'schemaVersion':1,'units':'m','upAxis':'Y','originOffset':[12,34,56], 'dimensions':dims,'source':source,
                       'parts':[{'id':name,'name':name.title(),'vertices':points,'indices':faces,'color':color}]})
    instances = [{'id':a['id'],'name':a['name'],'assetId':a['id'],'position':p,'rotation':[0,0,0],'visible':True} for a,p in zip(assets,[[0,0,0],[0,.3,0],[0,1,0]])]
    scene = {'format':'astra.scene','version':1,'units':'m','upAxis':'Y','room':[8,6,3], 'assets':[{'id':a['id'],'name':a['name'],'source':a['source'],'dimensions':a['dimensions']} for a in assets], 'bundledAssets':assets, 'instances':instances, 'animation':{'duration':1,'tracks':[]}}
    config = {'version':1,'anchor':{'longitude':0,'latitude':0,'height':0,'heading':0},'licenseNote':'Newly authored conceptual fixture; MIT, tools/physics/LICENSE', 'timestep':.002, 'gravity':[0,-9.81,0], 'coacd':{'threshold':.05,'seed':0}, 'instances':{
        'bench':{'mode':'fixed','friction':[.8,.005,.0001]},
        'carrier':{'mode':'slide','friction':[.8,.005,.0001],'mass':2,'centerOfMass':[0,.1,0],'diagonalInertia':[.113333333333,.213333333333,.113333333333],'axis':[1,0,0],'range':[-1,1],'damping':2,'controlRange':[-10,10]},
        'sample':{'mode':'free','friction':[.8,.005,.0001],'mass':.1,'centerOfMass':[0,.125,0],'diagonalInertia':[.0010416666667]*3}}}
    return scene, config


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path);args=p.parse_args()
    if args.output.exists():
        p.error('Choose a new output directory')
    args.output.mkdir(parents=True)
    scene,config=fixture()
    write_json(args.output/'scene.oi.json',scene);write_json(args.output/'physics.json',config)
    build_bundle(args.output/'scene.oi.json',args.output/'physics.json',args.output/'bundle')
    print(f'Physics bundle: {args.output / "bundle"}')


if __name__=='__main__':
    main()
