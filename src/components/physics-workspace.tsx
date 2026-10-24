import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { decodePoseFrame, type PhysicsManifest, type PoseFrame } from '../lib/physics-protocol';
import './physics-workspace.css';

type Status = { revision: number; step: number; time: number; running: boolean; controls: number[]; workerError: string | null };
type Request = { id: string; expectedRevision: number; action: string; values?: number[]; steps?: number; label?: string };
type Entry = { request: Request; receipt: { revision: number; step: number; time: number; action: string } };
const api = '/physics/api';

async function readJSON<T>(path: string): Promise<T> {
  const response = await fetch(api + path);
  if (!response.ok) throw new Error(`Physics service unavailable (${response.status}). Start the local physics service and open its workbench.`);
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Start the local physics service on port 8793.');
  return response.json();
}

export function PhysicsWorkspace() {
  const host = useRef<HTMLDivElement>(null);
  const latest = useRef<PoseFrame | null>(null), replayFrame = useRef<PoseFrame | null>(null);
  const [manifest, setManifest] = useState<PhysicsManifest | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [display, setDisplay] = useState({ step: 0, time: 0 });
  const [connection, setConnection] = useState('Connecting');
  const [selected, setSelected] = useState('');
  const [values, setValues] = useState<number[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [label, setLabel] = useState('');
  const [error, setError] = useState('');
  const [verification, setVerification] = useState('');
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Request | null>(null);
  const [reviewStep, setReviewStep] = useState<number | null>(null);
  const reviewEpoch = useRef(0);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    void readJSON<PhysicsManifest>('/manifest').then(m => {
      if (m.schema !== 'oi-physics-bundle/v1' || m.frame !== 'meters/Y-up/right-handed/body-origin' || !m.instances.length) throw new Error('Unsupported physics bundle');
      if (active) { setManifest(m); setValues(m.controls.map(() => 0)); }
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [refresh]);

  useEffect(() => {
    if (!manifest) return;
    let active = true, timer = 0;
    async function poll() {
      try {
        const state = await readJSON<Status>('/status');
        if (active) {
          setStatus(state);
          if (state.workerError) setError(`Physics worker stopped: ${state.workerError}`);
          // Query a bounded page near the current command revision.
          const history = await readJSON<Entry[]>(`/history?after=${Math.max(0, state.revision - 30)}`);
          if (active) setEntries(history);
        }
      } catch (e) { if (active) setError((e as Error).message); }
      if (active) timer = window.setTimeout(() => void poll(), 1000);
    }
    void readJSON<Status>('/status').then(s => { if (active) setValues(s.controls); }).catch(() => {});
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [manifest]);

  useEffect(() => {
    if (!manifest || !host.current) return;
    let active = true, animation = 0, retry = 0, socket: WebSocket | null = null;
    let backoff = 500, fitted = false;
    const scene = new THREE.Scene(); scene.background = new THREE.Color('#101e26');
    const camera = new THREE.PerspectiveCamera(45, 1, .01, 10000); camera.position.set(7, 5, 8);
    const renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    const element = host.current; element.appendChild(renderer.domElement);
    renderer.domElement.setAttribute('aria-label', 'Physics hardware workspace');
    const orbit = new OrbitControls(camera, renderer.domElement); orbit.enableDamping = true;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x38434f, 2));
    scene.add(new THREE.GridHelper(20, 40, 0x5f8d9c, 0x294551));
    const groups: THREE.Group[] = [];
    const loader = new GLTFLoader();
    function disposeObject(root: THREE.Object3D) {
      root.traverse(object => {
        if (object instanceof THREE.Mesh) {
          object.geometry.dispose();
          (Array.isArray(object.material) ? object.material : [object.material]).forEach(m => m.dispose());
        }
      });
    }
    void Promise.all(manifest.instances.map(async item => {
      const model = await loader.loadAsync('/physics/bundle/' + item.visualUri);
      if (!active) { disposeObject(model.scene); return null; }
      const group = new THREE.Group(); group.userData.instanceId = item.instanceId;
      group.add(model.scene); scene.add(group); return group;
    })).then(loaded => { if (active) groups.push(...loaded.filter((g): g is THREE.Group => !!g)); }).catch(e => { if (active) setError(`Visuals could not load: ${e.message}`); });
    const ray = new THREE.Raycaster();
    const pick = (event: MouseEvent) => {
      const bounds = renderer.domElement.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2((event.clientX-bounds.left)/bounds.width*2-1, -(event.clientY-bounds.top)/bounds.height*2+1), camera);
      let object: THREE.Object3D | undefined = ray.intersectObjects(groups, true)[0]?.object;
      while (object && !object.userData.instanceId) object = object.parent ?? undefined;
      if (object) setSelected(object.userData.instanceId);
    };
    renderer.domElement.addEventListener('click', pick);
    const resize = new ResizeObserver(() => {
      const width = element.clientWidth, height = element.clientHeight;
      renderer.setSize(width, height); camera.aspect = width / Math.max(1, height); camera.updateProjectionMatrix();
    });
    resize.observe(element);
    const render = () => {
      if (!active) return;
      const frame = replayFrame.current ?? latest.current;
      if (frame && groups.length === manifest.instances.length) {
        groups.forEach((group, i) => { group.position.fromArray(frame.poses, i*7); group.quaternion.fromArray(frame.poses, i*7+3); });
        if (!fitted) {
          scene.updateMatrixWorld(true);
          const box = new THREE.Box3(); groups.forEach(g => box.union(new THREE.Box3().setFromObject(g)));
          const center = box.getCenter(new THREE.Vector3()), distance = Math.max(2, box.getSize(new THREE.Vector3()).length());
          orbit.target.copy(center); camera.position.copy(center).add(new THREE.Vector3(distance, distance*.7, distance)); fitted = true;
        }
      }
      orbit.update(); renderer.render(scene, camera); animation = requestAnimationFrame(render);
    };
    const connect = () => {
      if (!active) return;
      setConnection('Connecting');
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${api}/stream`);
      socket.binaryType = 'arraybuffer';
      socket.onopen = () => { backoff = 500; if (active) { setConnection('Live'); setError(''); } };
      socket.onmessage = event => {
        if (!active) return;
        try {
          if (!(event.data instanceof ArrayBuffer)) throw new Error('Unexpected physics message');
          const frame = decodePoseFrame(event.data, manifest.instances.length);
          if (latest.current && frame.step < latest.current.step) throw new Error('Physics history changed; reload to select the new run');
          latest.current = frame;
          if (!replayFrame.current) setDisplay({ step: frame.step, time: frame.time });
        } catch (e) { setError((e as Error).message); socket?.close(); }
      };
      socket.onclose = () => {
        if (active) { setConnection('Reconnecting'); retry = window.setTimeout(connect, backoff); backoff = Math.min(5000, backoff*2); }
      };
    };
    connect(); render();
    return () => {
      active = false; clearTimeout(retry); cancelAnimationFrame(animation); socket?.close(); resize.disconnect();
      renderer.domElement.removeEventListener('click', pick); orbit.dispose(); disposeObject(scene); renderer.dispose(); renderer.domElement.remove();
      latest.current = null; replayFrame.current = null;
    };
  }, [manifest]);

  async function submit(request: Request) {
    setBusy(true); setError(''); setPending(request);
    try {
      const response = await fetch(api + '/commands', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-OI-Physics': '1' }, body: JSON.stringify(request) });
      if (response.status >= 400 && response.status < 500) {
        const body = await response.json(); setPending(null); throw new Error(body.error ?? 'Command rejected');
      }
      if (!response.ok) throw new Error('Command outcome is unknown. Retry the same command to recover its receipt.');
      await response.json(); setPending(null);
      const state = await readJSON<Status>('/status');
      setStatus(state); setEntries(await readJSON<Entry[]>(`/history?after=${Math.max(0, state.revision - 30)}`));
      setLabel('');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function act(action: string) {
    if (!status || pending) return;
    void submit({ id: crypto.randomUUID(), expectedRevision: status.revision, action, label,
      ...(action === 'control' ? { values } : action === 'step' ? { steps: 100 } : {}) });
  }
  async function review(step: number) {
    const epoch = ++reviewEpoch.current;
    setReviewStep(step);
    try {
      const response = await fetch(`${api}/frames/${step}`);
      if (!response.ok) throw new Error('Recorded frame unavailable');
      const frame = decodePoseFrame(await response.arrayBuffer(), manifest!.instances.length);
      if (epoch === reviewEpoch.current) { replayFrame.current = frame; setDisplay({ step, time: frame.time }); }
    } catch (e) { if (epoch === reviewEpoch.current) setError((e as Error).message); }
  }
  function live() {
    reviewEpoch.current++; replayFrame.current = null; setReviewStep(null);
    if (latest.current) setDisplay({ step: latest.current.step, time: latest.current.time });
  }
  async function verify() {
    setBusy(true); setVerification('Re-simulating recorded controls…');
    try {
      const end = status!.step, start = Math.max(0, end-10000);
      const response = await fetch(api + '/replay', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-OI-Physics': '1' }, body: JSON.stringify({ start, end }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      setVerification(result.passed ? `Verified steps ${start}–${end}: full-precision states match exactly.` : `Replay diverged at step ${result.divergentStep}.`);
    } catch (e) { setVerification((e as Error).message); }
    finally { setBusy(false); }
  }
  const disabled = busy || !!pending || !status || !!status.workerError || connection !== 'Live' || reviewStep !== null;
  const item = manifest?.instances.find(i => i.instanceId === selected);
  return <div className="physics-app">
    <header><a href="/">OPEN INDUSTRIALS</a><span>Physics workspace</span><span className="physics-connection">{connection}</span></header>
    <div className="physics-layout">
      <aside>
        <h1>Hardware & workflow</h1><p>Review equipment motion driven by the local simulation.</p>
        <label>Workflow note<input value={label} maxLength={160} onChange={e => setLabel(e.target.value)} placeholder="e.g. Begin transfer" disabled={disabled}/></label>
        <div className="physics-actions"><button disabled={disabled} onClick={() => act(status?.running ? 'pause' : 'play')}>{status?.running ? 'Pause' : 'Run'}</button><button disabled={disabled || status?.running} onClick={() => act('step')}>Step 100</button></div>
        {manifest?.controls.map((control, i) => <label key={control.id}>{manifest.instances.find(x => x.instanceId === control.instanceId)?.name} · {control.unit}
          <input aria-label={control.id} type="number" min={control.range[0]} max={control.range[1]} step="any" value={values[i] ?? 0} disabled={disabled} onChange={e => setValues(old => old.map((v,j) => i===j ? Number(e.target.value) : v))}/>
          <small>Range {control.range.join(' to ')}</small></label>)}
        {!!manifest?.controls.length && <button disabled={disabled} onClick={() => act('control')}>Apply controls</button>}
        <h2>Recorded review</h2>
        <label>Recorded step<input aria-label="Recorded step" type="range" min={0} max={status?.step ?? 0} value={reviewStep ?? display.step} onChange={e => void review(Number(e.target.value))} disabled={!status || busy}/></label>
        <button onClick={live} disabled={reviewStep === null}>Return to live</button>
        <button onClick={() => void verify()} disabled={!status || busy || !!pending}>Verify deterministic replay</button>
        <p role="status">{verification}</p>
        <h2>Equipment</h2>
        {manifest?.instances.map(i => <button className={selected===i.instanceId ? 'selected' : ''} key={i.instanceId} onClick={() => setSelected(i.instanceId)}>{i.name}</button>)}
        {item && <p>{item.name} · {item.mode}<br/><small>{item.instanceId}</small></p>}
      </aside>
      <section className="physics-stage"><div ref={host} className="physics-canvas"/><div className="physics-overlay">{reviewStep === null ? 'LIVE' : 'RECORDED REVIEW'} · {display.time.toFixed(3)} s · step {display.step}<br/><small>Drag to orbit · scroll to zoom · click equipment to inspect</small></div></section>
      <aside className="physics-history"><h2>Control history</h2>{entries.slice().reverse().map(e => <article key={e.request.id}><strong>{e.request.label || e.receipt.action}</strong><p>{e.receipt.time.toFixed(3)} s · step {e.receipt.step} · revision {e.receipt.revision}</p><button onClick={() => void review(e.receipt.step)}>Review this state</button></article>)}</aside>
    </div>
    <footer>Local modeled physics · visuals and collision geometry share one origin · recording survives restart</footer>
    {error && <div className="physics-error" role="alert">{error}{pending ? <button disabled={busy} onClick={() => void submit(pending)}>Retry same command</button> : <button onClick={() => { setError(''); if (!manifest) setRefresh(n => n+1); }}>Dismiss / retry connection</button>}</div>}
  </div>;
}
