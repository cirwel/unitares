#!/usr/bin/env python3
"""Test Glama's actual outer mcp-proxy command over Streamable HTTP."""
import subprocess
import time
import uuid

name = 'unitares-glama-proxy-test-'+uuid.uuid4().hex[:10]
volume = name+'-data'
subprocess.run(['docker','volume','create',volume],check=True,stdout=subprocess.DEVNULL)
try:
    subprocess.run(['docker','run','-d','--name',name,'--network','none','--memory','2g','-v',volume+':/data','unitares-glama:proxy-test'],check=True,stdout=subprocess.DEVNULL)
    probe = '''
import asyncio,json
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def main():
    async with streamable_http_client('http://127.0.0.1:8080/mcp') as streams:
        async with ClientSession(streams[0],streams[1]) as session:
            await session.initialize()
            result=await session.call_tool('start_session',{'force_new':True})
            body=json.loads(result.content[0].text)
            assert body['success'], body
            binding=body['client_session_id']
            result=await session.call_tool('store_finding',{'client_session_id':binding,'summary':'glama outer proxy persistence probe','discovery_type':'observation'})
            assert json.loads(result.content[0].text)['success']
            result=await session.call_tool('search_shared_memory',{'client_session_id':binding,'query':'glama outer proxy persistence probe','response_mode':'full'})
            body=json.loads(result.content[0].text)
            assert body['success'] and 'glama outer proxy persistence probe' in json.dumps(body)
asyncio.run(main())
'''
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        result=subprocess.run(['docker','exec',name,'/opt/unitares/bin/python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping',timeout=2)"],capture_output=True)
        if result.returncode==0:
            break
        time.sleep(.5)
    else:
        raise RuntimeError('Glama proxy readiness timeout')
    result=subprocess.run(['docker','exec','-i',name,'/opt/unitares/bin/python','-'],input=probe,text=True,capture_output=True,timeout=60)
    assert result.returncode==0, result.stderr[-2000:]
    print('PASS Glama mcp-proxy 6.4.3 → supervised stdio → private HTTP: onboard/store/search')
finally:
    subprocess.run(['docker','stop','-t','45',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['docker','volume','rm',volume],check=True,stdout=subprocess.DEVNULL)
