# Headless physics workspace

The workbench now has a local **Physics workspace** mode. A Python MuJoCo
process owns motion and contacts. Three.js displays immutable GLBs at streamed
body poses. SQLite stores full-precision integration states and control receipts.
The existing room editor remains the place to arrange equipment before export.

## Run the complete example

Use Node 22.18+ and Python 3.12 (prebuilt dependencies are tested with 3.12).
From the repository root:

```sh
npm ci
npm run build
python3.12 -m venv .venv-mujoco
.venv-mujoco/bin/python -m pip install -r tools/physics/requirements.txt
.venv-mujoco/bin/python -m tools.physics.demo .astra/physics/demo
.venv-mujoco/bin/python -m tools.physics.service .astra/physics/demo/bundle
```

On Windows use `py -3.12 -m venv .venv-mujoco` and
`.venv-mujoco\Scripts\python.exe` in place of the Python commands above.

Open **http://127.0.0.1:8793/?physics**. The example contains a fixed bench,
an actuated slide carrier, and a freely falling sample. Enter a bounded carrier
force, optionally add a workflow note, and choose **Apply controls**. **Run**
advances independently of browsers; **Pause** and **Step 100** support inspection.
Scrub recorded steps or select a transaction to review its recorded state.
**Verify deterministic replay** re-simulates the last up to 10,000 steps and
compares the complete Float64 integration state at every step.

Restart the same service with the same bundle/database to resume. By default
the database is `.astra/physics/runtime.sqlite`; use `--database` for another
run. Build a new output directory for a new bundle: existing bundles cannot
be overwritten. A changed bundle requires a new database.

Alternatively run `npm start` in a second terminal and use the workbench's
**Physics workspace** link at port 8787. Its HTTP/WebSocket proxy always targets
loopback port 8793. `npm run dev` provides the same proxy at port 5173. Opening
the physics service directly supports a custom `--port`; the other proxies
deliberately use a fixed destination. The Python service serves the built UI;
Form generation is available through the normal Node workbench.

## Use a hardware layout

1. Arrange and name equipment in the existing room editor. Export an OI project.
2. Create an explicit physics sidecar following the generated demo's
   `.astra/physics/demo/physics.json`.
3. Build the pair of visual and collision representations:

```sh
.venv-mujoco/bin/python -m tools.physics.export layout.oi.json physics.json .astra/physics/layout-v1
.venv-mujoco/bin/python -m tools.physics.service .astra/physics/layout-v1 --database .astra/physics/layout-v1.sqlite
```

The sidecar names every visible instance. Modes are `fixed`, `free`, `hinge`,
or `slide`. Moving bodies require mass in kg, a body-local center of mass in m,
and diagonal principal inertia in kg m², aligned with the body axes. Hinge/slide
joints additionally require a unit axis, range, damping and motor control range.
Hinge angles are radians and motor input is N m; slide distances are m and input
is N. Each body has its own world-attached joint. Nested articulated chains,
off-diagonal inertia, imported robot controllers and closed-loop job-to-actuator
policies are not implemented. Invalid/incomplete parameters are rejected;
geometry and BOM labels never supply guessed physical properties.

Scene editing does not teleport a running body or overwrite its history.
Exporting a changed layout creates a new bundle/run. Imported authored keyframes
are excluded from the physics bundle; MuJoCo owns motion in this mode.

## Coordinate and geometry contract

- OI, Three.js and this MuJoCo model all use **meters, right-handed Y-up**. The
  example sets gravity to `(0,-9.81,0)`. MuJoCo does not require Z-up dynamics.
- Normalized asset vertices stay in their stored local frame. `originOffset`
  is provenance and is never applied a second time. No independent centering,
  scaling or PCA is applied to GLBs or CoACD hulls.
- Each visible instance has a stable scene ID and named MJCF body. Visual
  transforms use body `xpos`/`xquat`, not MuJoCo's internally recentered mesh frame.
  Free-body alignment is disabled; explicit inertials preserve the locked origin.
- Every CoACD output hull becomes its **own OBJ mesh and MJCF geom**. Combining
  all hulls into one mesh would cause MuJoCo to convexify the union and fill holes.
- The bundle includes the original static 3D Tiles 1.1 export, geographic anchor,
  provenance and shared GLBs. Earth-fixed placement stays in the static tileset;
  live physics and Float32 poses remain local. The tileset describes the initial
  layout, not time-varying bounds or Cesium animation.
- Source/config hashes, pinned engine versions, individual file hashes and body
  bindings are stored in `manifest.json`. Startup verifies the full bundle.

## Streaming and authority

`GET /physics/api/stream` is a read-only WebSocket. Messages are little endian:

| Bytes | Meaning |
| --- | --- |
| 0–3 | ASCII `OIP1` |
| 4–7 | uint32 protocol version 1 |
| 8–11 | uint32 committed simulation step |
| 12–15 | uint32 body count in manifest order |
| 16–23 | Float64 simulation time in seconds |
| 24 onward | Seven Float32 values/body: position xyz, quaternion xyzw |

The client rejects malformed lengths, mappings, nonfinite values and invalid
quaternions. It never feeds rounded poses back into physics. Slow viewers may
skip frames and reconnect to the current committed step; all intermediate
states remain recorded. Browser rendering is independent of the physics clock.

Commands are JSON POSTs to `/physics/api/commands`, carrying `X-OI-Physics: 1`:

```json
{"id":"operator-intent-1","expectedRevision":0,"action":"control","values":[3],"label":"Begin carrier transfer"}
```

Actions are `play`, `pause`, `control` and paused `step` (1–1000 steps). Control
revisions increment on accepted commands, not worker ticks. Each receipt records
the committed step, revision, time and action. The request and Float64 controls
are committed with the state under `BEGIN IMMEDIATE`, WAL and `synchronous=FULL`.
A repeated ID and identical request returns its original receipt. Changed payloads
or stale revisions are rejected. After an uncertain network result the UI retains
the same request and exposes **Retry same command**; it does not mint a new ID.

`GET /status`, `/history?after=<revision>` and `/frames/<step>` expose bounded
local review. `POST /replay` accepts `{ "start": 0, "end": 100 }` and verifies
on a separate data instance/SQLite snapshot without changing the live run.

## Persistence and determinism

Each step records `mjSTATE_INTEGRATION` as explicit little-endian Float64 bytes,
including warmstarts, controls and applied forces, with a SHA-256 checksum.
Playback reads stored poses; deterministic verification instead executes recorded
controls at their original step boundaries and compares full integration bytes.
Tests cover replay from an intermediate checkpoint, changed controls, actual
contacts, restart and transaction rollback. Tampered state hashes fail verification.

The database pins the bundle hash, MuJoCo/NumPy versions, platform/CPU architecture
and state signature/size. A mismatch refuses reopening. Exact reproducibility is
tested within that environment; it is not promised across MuJoCo releases, CPUs,
operating systems, compiler builds, arbitrary plugins or external controller code.
The test deliberately compares bytes rather than claiming rounded poses prove
determinism. The full model and its collision assets must accompany the database.

The service uses fixed simulation steps and a bounded monotonic-clock worker.
Running state survives restart, but service downtime is **not** converted into
unlogged physics steps; the simulation resumes from its last committed time.
Only one worker may own a database (OS lock released on hard exit). Commands and
steps roll back together on failure. There is no unrecorded reset API.

This is a local single-operator physics service. It binds loopback, checks Host
and exact allowed local Origins, requires an explicit mutation header, serves
only allowlisted bundle assets, and accepts no remote model uploads. It is separate
from native multi-account game/work-cell/lab authorization. A remote multi-user
physics deployment requires its own authenticated adapter and is not enabled.

Limits: 128 visible bodies, 100k vertices/200k triangles per mesh part, at most
64 CoACD hulls/part, 8 connected viewers, 16 KiB commands, 1000 steps/manual command,
1,000,000 steps/run and 10,000 steps/replay verification. Decomposition runs offline
and can take substantial time for complex meshes. Scientific accuracy, collision
approximation quality and real hardware suitability require separate measurements.
The implementation does not connect actuator motion to game inventory or certify
FIELD-LAB sample validity.

## Verification

```sh
.venv-mujoco/bin/python -m unittest discover -s tests/physics -v
npm run test:physics-protocol
npm run build
npx playwright install chromium
PHYSICS_PYTHON=.venv-mujoco/bin/python npm run test:physics-browser
```

The browser test launches isolated services and a temporary database, exercises
the normal workbench proxy, control changes, a committed-but-lost response, retry,
recorded review, replay verification, reload and desktop/mobile layouts. Screenshots
are written to `test-results/physics-desktop.png` and `physics-mobile.png`.
`ASTRA_CHROME_PATH` can select an existing Chrome installation. Ports 8787 and 8793
must be free. Windows CI sets `PHYSICS_PYTHON` to the installed interpreter.

References: [MuJoCo integration state and reproducibility](https://mujoco.readthedocs.io/en/3.6.0/programming/simulation.html),
[CoACD separate collision hulls](https://github.com/SarahWeiii/CoACD).
