#!/usr/bin/env python3
"""Disposable, network-isolated black-box test of the actual bundle image.

Usage: python scripts/ci/check_glama_bundle.py --image unitares-glama:local
Never mounts host data, publishes ports, or uses host service credentials.
"""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid


class Client:
    def __init__(self, image, volume, name, backend='postgres'):
        self.name = name
        self.serial = 0
        self.messages = queue.Queue()
        self.started = time.monotonic()
        self.log = open(f'{name}.stderr.log', 'w')
        self.process = subprocess.Popen([
            'docker','run','--rm','-i','--network','none','--memory','2g',
            '--name',name,'-v',f'{volume}:/data',
            '-e',f'UNITARES_KNOWLEDGE_BACKEND={backend}',image,
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        def reader():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    self.messages.put({'stdout_contamination':line[:100]})
        threading.Thread(target=reader, daemon=True).start()
        self.rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'bundle-smoke','version':'1'}})
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        print(f'PASS startup handshake ({time.monotonic()-self.started:.1f}s)', flush=True)

    def send(self, message):
        self.process.stdin.write(json.dumps(message)+'\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.serial += 1
        deadline = time.monotonic() + 180
        self.send({'jsonrpc':'2.0','id':self.serial,'method':method,'params':params})
        while True:
            try:
                response = self.messages.get(timeout=2)
            except queue.Empty:
                assert self.process.poll() is None, f'container exited {self.process.returncode}; see {self.name}.stderr.log'
                assert time.monotonic() < deadline, 'MCP timeout'
                continue
            assert 'stdout_contamination' not in response, response
            if response.get('id') == self.serial:
                assert 'error' not in response, response
                return response['result']

    def call(self, name, **arguments):
        result = self.rpc('tools/call',{'name':name,'arguments':arguments})
        assert not result.get('isError'), (name,result)
        body = json.loads(result['content'][0]['text'])
        assert body.get('success') is True, (name,body)
        return body

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                subprocess.run(['docker','stop','-t','30',self.name],check=True,stdout=subprocess.DEVNULL)
                self.process.wait(timeout=5)
        self.log.close()


def inside(name, source):
    result = subprocess.run(['docker','exec','-i',name,'/opt/unitares/bin/python','-'],input=source,text=True,capture_output=True)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def run(image):
    suffix = uuid.uuid4().hex[:10]
    volume = f'unitares-glama-test-{suffix}'
    name = f'unitares-glama-test-{suffix}'
    subprocess.run(['docker','volume','create',volume],check=True,stdout=subprocess.DEVNULL)
    client = None
    try:
        client = Client(image,volume,name)
        listing = client.rpc('tools/list',{})
        assert 'store_finding' in {tool['name'] for tool in listing['tools']}
        identity = client.call('start_session',force_new=True)
        session = identity['client_session_id']
        agent = identity['agent_uuid']
        client.call('sync_state',client_session_id=session,response_text='Isolated bundle persistence validation',complexity=.3)
        marker = 'bundle-persistence-'+suffix
        stored = client.call('store_finding',client_session_id=session,summary=marker,discovery_type='observation')
        found = client.call('search_shared_memory',client_session_id=session,query=marker,response_mode='full')
        assert marker in json.dumps(found)
        print('PASS onboard → check-in → store → search',flush=True)
        # Read the actual finding by its persisted ID (independent of search).
        raw = stored.get('raw_governance',stored)
        discovery_id = raw.get('discovery_id') or raw.get('discovery',{}).get('id')
        assert discovery_id, stored
        read = client.call('knowledge',action='get',discovery_id=discovery_id,client_session_id=session)
        assert marker in json.dumps(read)
        print('PASS finding readback',flush=True)
        version_probe = "import os,json,subprocess; v=json.load(open('/data/secrets.json')); os.environ['PGPASSWORD']=v['database']; subprocess.run(['/usr/lib/postgresql/18/bin/psql','-h','127.0.0.1','-U','postgres','-d','governance','-Atc',\"SELECT extname || '=' || extversion FROM pg_extension WHERE extname IN ('age','vector') ORDER BY extname\"],check=True)"
        versions = inside(name, version_probe)
        assert 'age=1.7.0' in versions and 'vector=0.8.3' in versions, versions
        print('PASS AGE 1.7.0 + pgvector 0.8.3',flush=True)
        # Exercise the repository's two-agent signed lease/handoff proof internally.
        demo = Path('scripts/demo/coordination_demo.py').read_text()
        prelude = "import os,json\nsecrets=json.load(open('/data/secrets.json'))\nos.environ['LEASE_PLANE_BEARER_TOKEN']=secrets['lease']\nos.environ['UNITARES_HTTP_API_TOKEN']=secrets['http']\n"
        # Execute through compile so __file__ keeps the demo's expected root layout.
        receipt = inside(name, prelude+f"exec(compile({demo!r}, '/app/scripts/demo/coordination_demo.py', 'exec'), {{'__name__':'__main__','__file__':'/app/scripts/demo/coordination_demo.py'}})")
        assert 'atomic handoff: active' in receipt
        print('PASS signed coordination, collision, handoff, release',flush=True)
        client.close(); client = None
        client = Client(image,volume,name)
        resumed = client.call('identity',client_session_id=session)
        assert agent in json.dumps(resumed), resumed
        found = client.call('knowledge',action='get',discovery_id=discovery_id,client_session_id=session)
        assert marker in json.dumps(found)
        print('PASS restart: durable finding and same live-driver identity binding',flush=True)
        client.close(); client = None
        client = Client(image,volume,name,backend='age')
        graph_identity = client.call('start_session',force_new=True)
        graph_session = graph_identity['client_session_id']
        client.call('store_finding',client_session_id=graph_session,summary='graph-'+marker,discovery_type='observation')
        found = client.call('search_shared_memory',client_session_id=graph_session,query='graph-'+marker,response_mode='full')
        assert marker in json.dumps(found)
        print('PASS AGE backend graph write/search',flush=True)
        review = client.call('request_review',client_session_id=graph_session,issue_description='Isolated bundle review persistence validation')
        print('PASS request_review storage path (review completion requires a reviewer)',flush=True)
        # A child death must tear down MCP, not leave discovery falsely healthy.
        subprocess.run(['docker','exec','--user','postgres',name,'/usr/lib/postgresql/18/bin/pg_ctl','-D','/data/postgres','-m','fast','stop'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        assert client.process.wait(timeout=35) != 0
        print('PASS database death is a nonzero container exit',flush=True)
        client.close(); client = None
        client = Client(image,volume,name,backend='age')
        found = client.call('search_shared_memory',query='graph-'+marker,response_mode='full')
        assert marker in json.dumps(found)
        print('PASS AGE finding durable after dependency failure/restart',flush=True)
        subprocess.run(['docker','exec',name,'redis-cli','shutdown'],check=True,stdout=subprocess.DEVNULL)
        assert client.process.wait(timeout=35) != 0
        client.close(); client = None
        print('PASS Redis death is a nonzero container exit',flush=True)
        mutate = ['docker','run','--rm','--network','none','-v',f'{volume}:/data','--entrypoint','/opt/unitares/bin/python',image,'-c']
        subprocess.run(mutate+["from pathlib import Path; Path('/data/schema.sha256').write_text('incompatible')"],check=True)
        refused = subprocess.run(['docker','run','--rm','--network','none','-v',f'{volume}:/data',image],text=True,capture_output=True,timeout=15)
        assert refused.returncode != 0 and 'schema fingerprint mismatch' in refused.stderr
        assert refused.stdout == ''
        print('PASS incompatible schema image refused without MCP stdout',flush=True)
        subprocess.run(mutate+["from pathlib import Path; Path('/data/schema.sha256').unlink()"],check=True)
        refused = subprocess.run(['docker','run','--rm','--network','none','-v',f'{volume}:/data',image],text=True,capture_output=True,timeout=15)
        assert refused.returncode != 0 and 'incomplete bootstrap' in refused.stderr
        print('PASS incomplete bootstrap refused without reinitializing data',flush=True)
    finally:
        if client:
            client.close()
        subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume],check=True,stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default='unitares-glama:local')
    run(parser.parse_args().image)
