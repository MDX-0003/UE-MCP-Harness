import urllib.request, json, time

# Connect SSE
resp = urllib.request.urlopen('http://127.0.0.1:9000/sse')
# Read endpoint
event_line = resp.readline().decode().strip()
data_line = resp.readline().decode().strip()
print('SSE connected:', event_line, data_line)
endpoint = data_line.replace('data: ', '').strip()
print('Endpoint:', endpoint)

# Initialize
init = json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'1.0','capabilities':{},'clientInfo':{'name':'claude','version':'1.0'}}}).encode()
urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:9000{endpoint}', data=init, headers={'Content-Type':'application/json'})).read()

# Read SSE response for init
line = resp.readline().decode().strip()
if not line: line = resp.readline().decode().strip()
if 'event:' in line: line = resp.readline().decode().strip()
init_resp = json.loads(line.replace('data: ', ''))
print('Server:', init_resp['result']['serverInfo'])

# List tools
list_req = json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}).encode()
urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:9000{endpoint}', data=list_req, headers={'Content-Type':'application/json'})).read()

# Read SSE response for tools/list
line = resp.readline().decode().strip()
if not line: line = resp.readline().decode().strip()
if 'event:' in line: line = resp.readline().decode().strip()
tools_resp = json.loads(line.replace('data: ', ''))
tools = tools_resp['result']['tools']
print(f'\nTotal tools: {len(tools)}')
for t in tools:
    desc = t.get('description','')[:80]
    print(f'  - {t[\"name\"]}')
    if desc: print(f'      {desc}')

resp.close()
