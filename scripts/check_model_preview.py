#!/usr/bin/env python3
"""Optional Linux/Chrome end-to-end test using the real isolated OpenSCAD compiler.

Run with the printer component test environment (websockets). No printer services
are used. Source and compiler artifacts are kept in a disposable test directory.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]


def eventually(callback, message, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except (OSError, ValueError, KeyError) as exc:
            last = exc
        time.sleep(.1)
    raise AssertionError(f'{message}; last error: {last}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser-sandbox', action='store_true', help='Only for a constrained test container; compiler isolation remains mandatory.')
    parser.add_argument('--screenshot', type=Path, default=Path('/tmp/photonics-openscad-preview.png'))
    parser.add_argument('--generation-fixture', action='store_true', help='Also test description/clarification/revision using an explicit offline provider fixture and the real compiler, never live Groq.')
    parser.add_argument('--print-fixture', action='store_true', help='Test real slicing and print UI against a scripted printer fixture. Never connects to hardware.')
    args = parser.parse_args()
    chrome = os.environ.get('CHROME_BIN') or shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        raise SystemExit('Install Chrome/Chromium or set CHROME_BIN for this optional preview check.')
    with tempfile.TemporaryDirectory(prefix='photonics-preview-') as temp:
        work = Path(temp)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, PRINTER_MODE='mock', PRINTER_ALLOW_MOTION='0',
                   OPENSCAD_JOBS_DIR=str(work/'jobs'))
        env.pop('GROQ_API_KEY', None)
        env['MODEL_PROVIDER'] = 'groq'
        with (work/'server.log').open('w') as server_log, (work/'chrome.log').open('w') as browser_log:
            entrypoint = ROOT / ('lab-agents/tests/generation_server_fixture.py' if args.generation_fixture or args.print_fixture else 'lab-agents/app.py')
            server_args = [str(ROOT/'lab-agents/.venv/bin/python'), str(entrypoint),
                           '--printer-mode', 'mock', '--port', str(port)]
            if args.print_fixture:
                server_args.append('--print-fixture')
            server = subprocess.Popen(server_args, cwd=ROOT, env=env,
                                      stdout=server_log, stderr=server_log)
            browser = None
            try:
                url = f'http://127.0.0.1:{port}'
                def ready():
                    with urlopen(url+'/api/models/config', timeout=.5) as response:
                        config=json.load(response)
                    if not config['available']:
                        raise RuntimeError(config['error'])
                    return True
                eventually(ready, 'Dashboard did not start')
                command = [chrome, '--headless=new', '--no-first-run', '--no-default-browser-check',
                           '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disable-dev-shm-usage',
                           '--remote-debugging-port=0', '--user-data-dir='+str(work/'chrome'),
                           '--window-size=1280,1000', 'about:blank']
                if args.no_browser_sandbox:
                    command.insert(1, '--no-sandbox')
                browser = subprocess.Popen(command, stdout=browser_log, stderr=browser_log, env=env)
                active=work/'chrome/DevToolsActivePort'
                debugger_port=eventually(lambda: active.read_text().splitlines()[0], 'Chrome debugger did not start')
                with urlopen(f'http://127.0.0.1:{debugger_port}/json') as response:
                    target=next(target for target in json.load(response) if target['type']=='page')
                with connect(target['webSocketDebuggerUrl'], proxy=None, max_size=8*1024*1024) as connection:
                    sequence=0
                    exceptions=[]
                    def call(method, params=None):
                        nonlocal sequence
                        sequence += 1
                        connection.send(json.dumps(dict(id=sequence,method=method,params=params or {})))
                        while True:
                            reply=json.loads(connection.recv(timeout=30))
                            if reply.get('method')=='Runtime.exceptionThrown':
                                exceptions.append(reply['params'])
                            if reply.get('id')==sequence:
                                if 'error' in reply: raise RuntimeError(reply['error'])
                                return reply.get('result',{})
                    def evaluate(expression):
                        reply=call('Runtime.evaluate',dict(expression=expression,returnByValue=True,awaitPromise=True))
                        if 'exceptionDetails' in reply: raise RuntimeError(reply['exceptionDetails'])
                        return reply.get('result',{}).get('value')
                    def wait_js(expression, label):
                        return eventually(lambda: evaluate(expression), label, timeout=45)
                    call('Runtime.enable')
                    call('Page.enable')
                    call('Page.navigate',{'url':url})
                    wait_js("document.querySelector('#compiler-config')?.textContent.includes('Bubblewrap')", 'Compiler configuration never appeared')
                    if not args.generation_fixture and not args.print_fixture:
                        wait_js("document.getElementById('generation-config').textContent.includes('GROQ_API_KEY')", 'Missing-key guidance did not appear')
                        assert evaluate("document.getElementById('generate-model').disabled")
                    evaluate("document.getElementById('load-example').click()")
                    wait_js("document.getElementById('model-source').value.includes('outer_diameter')", 'Example source was not loaded')
                    evaluate("document.getElementById('compile-model').click()")
                    wait_js("document.querySelector('canvas')?.dataset.meshLoaded === 'true'", 'STL did not render')
                    assert evaluate("document.getElementById('model-dimensions').textContent.includes('24.000 × 24.000 × 6.000 mm')")
                    assert evaluate("document.getElementById('model-state').textContent.includes('succeeded')")
                    evaluate("document.getElementById('model-viewer').scrollIntoView({block:'center'})")
                    time.sleep(.2)
                    bounds=evaluate("(()=>{const r=document.querySelector('canvas').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})()")
                    x,y=bounds['x']+bounds['width']/2,bounds['y']+bounds['height']/2
                    before=call('Page.captureScreenshot',{'format':'png'})['data']
                    call('Input.dispatchMouseEvent',dict(type='mousePressed',x=x,y=y,button='left',clickCount=1))
                    call('Input.dispatchMouseEvent',dict(type='mouseMoved',x=x+100,y=y+40,button='left',buttons=1))
                    call('Input.dispatchMouseEvent',dict(type='mouseReleased',x=x+100,y=y+40,button='left',clickCount=1))
                    time.sleep(.2)
                    rotated=call('Page.captureScreenshot',{'format':'png'})['data']
                    assert before != rotated, 'Rotate did not change the rendered view'
                    call('Input.dispatchMouseEvent',dict(type='mouseWheel',x=x,y=y,deltaX=0,deltaY=-180))
                    time.sleep(.2)
                    zoomed=call('Page.captureScreenshot',{'format':'png'})['data']
                    assert rotated != zoomed, 'Zoom did not change the rendered view'
                    args.screenshot.write_bytes(base64.b64decode(zoomed))
                    evaluate("document.getElementById('model-confirm').click()")
                    wait_js("!document.getElementById('approve-model').disabled", 'Model approval remained disabled')
                    evaluate("document.getElementById('approve-model').click()")
                    wait_js("document.getElementById('model-approval').textContent.includes('exact model version is approved')", 'Model approval failed')
                    if args.print_fixture:
                        wait_js("!document.getElementById('read-printer').disabled", 'Printer read control remained disabled')
                        evaluate("document.getElementById('read-printer').click()")
                        wait_js("document.getElementById('print-readiness').textContent.includes('Controller readiness checks passed')", 'Fixture connection status was not displayed')
                        evaluate("document.getElementById('print-profile').value='ender3v3ke-pla-04';document.getElementById('print-profile').dispatchEvent(new Event('change'))")
                        wait_js("!document.getElementById('prepare-print').disabled", 'Approved model did not enable print preparation')
                        evaluate("document.getElementById('prepare-print').click()")
                        wait_js("document.getElementById('print-state').textContent.includes('ready')", 'Real slicer did not prepare the approved model')
                        assert evaluate("document.getElementById('start-print').disabled")
                        assert evaluate("document.getElementById('print-summary').textContent.includes('G-code SHA-256:')")
                        assert evaluate("document.getElementById('print-toolpath').getBoundingClientRect().height > 200 && document.querySelector('#print-toolpath path').getAttribute('d').length > 100")
                        wait_js("!document.getElementById('print-review-confirm').disabled", 'Final fixture review control remained disabled')
                        evaluate("document.getElementById('print-review-confirm').click()")
                        assert evaluate("document.getElementById('start-print').disabled")
                        evaluate("document.getElementById('print-setup-confirm').click()")
                        wait_js("!document.getElementById('start-print').disabled", 'Final fixture approval remained disabled')
                        evaluate("document.getElementById('print-title').scrollIntoView({block:'start'})")
                        time.sleep(.2)
                        args.screenshot.write_bytes(base64.b64decode(call('Page.captureScreenshot',{'format':'png'})['data']))
                        evaluate("document.getElementById('start-print').click()")
                        wait_js("document.getElementById('print-state').textContent.includes('start accepted')", 'Fixture start was not accepted')
                        wait_js("!document.getElementById('refresh-print').disabled", 'Fixture progress control remained disabled')
                        evaluate("document.getElementById('refresh-print').click()")
                        wait_js("document.getElementById('print-summary').textContent.includes('Printer state: printing')", 'Fixture progress was not shown')
                        assert evaluate("document.getElementById('start-print').disabled")
                        print('PASS: real approved STL → real isolated Creality Print → toolpath review → separate setup/job approval → one fixture-only start → matching progress. No physical printer connection.')
                    evaluate("const editor=document.getElementById('model-source');editor.value=editor.value.replace('height = 6','height = 9');editor.dispatchEvent(new Event('input'))")
                    assert evaluate("document.getElementById('approve-model').disabled")
                    evaluate("document.getElementById('compile-model').click()")
                    wait_js("document.querySelector('canvas')?.dataset.meshLoaded === 'true' && document.getElementById('model-dimensions').textContent.includes('9.000 mm')", 'Revised model did not render')
                    assert evaluate("document.getElementById('model-approval').textContent.includes('not approved')")
                    assert evaluate("document.getElementById('model-version').textContent.includes('revision 2')")
                    downloads=evaluate("Promise.all(['source-download','stl-download'].map(async id=>{const r=await fetch(document.getElementById(id).href);return {ok:r.ok,bytes:(await r.arrayBuffer()).byteLength,attachment:r.headers.get('content-disposition')}}))")
                    assert all(item['ok'] and item['bytes']>0 and 'attachment' in item['attachment'] for item in downloads)
                    evaluate("document.getElementById('model-source').value='cube([);';document.getElementById('model-source').dispatchEvent(new Event('input'));document.getElementById('compile-model').click()")
                    wait_js("document.getElementById('model-state').textContent.includes('Compilation error:')", 'Compilation error was not displayed')
                    assert evaluate("document.getElementById('stl-download').hidden && document.getElementById('approve-model').disabled")
                    if args.generation_fixture:
                        evaluate("document.getElementById('new-model').click();document.getElementById('model-description').value='Make a block';document.getElementById('model-description').dispatchEvent(new Event('input'));document.getElementById('generate-model').click()")
                        wait_js("document.getElementById('generation-state').textContent.includes('Please answer the design questions')", 'Clarification was not shown')
                        assert evaluate("document.getElementById('approve-model').disabled")
                        evaluate("document.getElementById('model-description').value='24 mm wide, 18 mm deep and 6 mm high';document.getElementById('model-description').dispatchEvent(new Event('input'));document.getElementById('generate-model').click()")
                        wait_js("document.querySelector('canvas')?.dataset.meshLoaded === 'true' && document.getElementById('model-dimensions').textContent.includes('24.000 × 18.000 × 6.000 mm')", 'Description fixture did not compile/render')
                        assert evaluate("document.getElementById('model-origin').textContent.includes('fixture')")
                        assert evaluate("document.getElementById('model-source').value.includes('width=24')")
                        wait_js("!document.getElementById('model-confirm').disabled", 'Fixture preview did not become reviewable')
                        evaluate("document.getElementById('model-confirm').click()")
                        wait_js("!document.getElementById('approve-model').disabled", 'Fixture approval did not become available')
                        evaluate("document.getElementById('approve-model').click()")
                        wait_js("document.getElementById('model-approval').textContent.includes('exact model version is approved')", 'Fixture model approval failed')
                        evaluate("document.getElementById('model-description').value='Make the height 9 mm';document.getElementById('model-description').dispatchEvent(new Event('input'));document.getElementById('generate-model').click()")
                        wait_js("document.querySelector('canvas')?.dataset.meshLoaded === 'true' && document.getElementById('model-dimensions').textContent.includes('9.000 mm')", 'AI revision fixture did not render')
                        assert evaluate("document.getElementById('model-approval').textContent.includes('not approved')")
                        assert evaluate("document.getElementById('model-version').textContent.includes('revision 2')")
                        evaluate("document.getElementById('model-description').value='Return unsafe file access for the test';document.getElementById('model-description').dispatchEvent(new Event('input'));document.getElementById('generate-model').click()")
                        wait_js("document.getElementById('generation-state').textContent.includes('EXTERNAL_ASSET_DENIED')", 'Unsafe provider source was not rejected')
                        assert evaluate("document.getElementById('approve-model').disabled")
                        print('PASS: offline provider fixture → clarification → real SCAD/STL → preview → approved version → fresh unapproved revision; unsafe source rejected. No live AI request was made.')
                    assert not exceptions, exceptions
                    print('PASS: real example → compile → STL preview; dimensions, rotate, zoom, both downloads, approval, revision reset and compiler errors.')
                    print('Preview screenshot:',args.screenshot)
            except BaseException:
                print('SERVER LOG:', (work/'server.log').read_text()[-6000:])
                print('BROWSER LOG:', (work/'chrome.log').read_text()[-3000:])
                raise
            finally:
                for process in (browser, server):
                    if process and process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=5)


if __name__=='__main__':
    main()
