import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { chromium } from '@playwright/test';

const root=process.cwd(), temp=await mkdtemp(join(tmpdir(),'oi-physics-browser-'));
const python=process.env.PHYSICS_PYTHON || resolve('.venv-mujoco/bin/python');
const children=[];
function start(executable,args){const child=spawn(executable,args,{cwd:root,env:process.env,stdio:['ignore','pipe','pipe']});children.push(child);let output='';child.stdout.on('data',b=>output+=b);child.stderr.on('data',b=>output+=b);child.output=()=>output;return child;}
async function completed(child){await new Promise((ok,fail)=>{child.on('error',fail);child.on('exit',code=>code===0?ok():fail(new Error(child.output())));});}
async function waitURL(url,child){for(let n=0;n<200;n++){if(child.exitCode!==null)throw new Error(child.output());try{if((await fetch(url)).ok)return;}catch{}await new Promise(r=>setTimeout(r,50));}throw new Error(`Startup failed: ${child.output()}`);}
let browser;
try{
  await completed(start(python,['-m','tools.physics.demo',join(temp,'demo')]));
  const service=start(python,['-m','tools.physics.service',join(temp,'demo/bundle'),'--database',join(temp,'run.sqlite')]);
  await waitURL('http://127.0.0.1:8793/physics/api/status',service);
  const web=start(process.execPath,['server/index.mjs']);
  await waitURL('http://127.0.0.1:8787/physics/api/status',web);
  browser=await chromium.launch({headless:true,...(process.env.ASTRA_CHROME_PATH?{executablePath:process.env.ASTRA_CHROME_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8787/?physics');
  await page.getByRole('button',{name:'Run',exact:true}).waitFor({state:'visible'});
  await page.getByRole('button',{name:'Run',exact:true}).click();
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByLabel('control_1').fill('3');
  // Commit server-side but drop the response; UI must preserve exactly the same ID.
  let dropped=false;
  await page.route('**/physics/api/commands',async route=>{
    if(!dropped&&route.request().postDataJSON()?.action==='control'){
      dropped=true;await route.fetch();await route.abort('failed');
    }else await route.continue();
  });
  await page.getByRole('button',{name:'Apply controls'}).click();
  await page.getByRole('button',{name:'Retry same command'}).click();
  await page.getByRole('button',{name:'Step 100'}).click();
  await page.getByRole('button',{name:'Verify deterministic replay'}).click();
  await page.getByRole('status').filter({hasText:'full-precision states match exactly'}).waitFor();
  await page.getByLabel('Recorded step').fill('0');
  await page.getByRole('button',{name:'Return to live'}).click();
  await page.getByRole('button',{name:'Sample',exact:true}).click();
  await mkdir('test-results',{recursive:true});
  await page.screenshot({path:'test-results/physics-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:'test-results/physics-mobile.png',fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.reload();await page.getByRole('button',{name:'Run',exact:true}).waitFor();
  const status=await (await fetch('http://127.0.0.1:8787/physics/api/status')).json();
  assert.equal(status.revision,4);assert.equal(status.controls[0],3);assert.ok(status.step>=100);
  assert.deepEqual(errors,[]);
  console.log('PASS physics browser: live WebSocket through workbench, controls, lost-response retry, replay, refresh, desktop/mobile');
}finally{
  await browser?.close();
  for(const child of children)if(child.exitCode===null){child.kill();await new Promise(r=>child.once('exit',r));}
  await rm(temp,{recursive:true,force:true});
}
