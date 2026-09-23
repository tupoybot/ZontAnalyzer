"""Runs inside the immutable candidate; uses loopback and synthetic data only."""
import base64
import http.client
import json

AUTH = 'Basic ' + base64.b64encode(b'smoke:synthetic').decode()
PAYLOAD = {
    'period_start': '2026-01-01T00:00:00Z', 'period_end': '2026-01-01T00:20:00Z', 'target_c': 22,
    'samples': [{'timestamp': '2026-01-01T00:00:00Z', 'value': 21},
                {'timestamp': '2026-01-01T00:10:00Z', 'value': 22},
                {'timestamp': '2026-01-01T00:20:00Z', 'value': 22}],
}


def request(path, payload=None, authenticated=True):
    conn = http.client.HTTPConnection('127.0.0.1', 8080, timeout=20)
    try:
        conn.request('GET' if payload is None else 'POST', path,
                     body=None if payload is None else json.dumps(payload),
                     headers={'Content-Type': 'application/json', 'Authorization': AUTH if authenticated else ''})
        r = conn.getresponse()
        return r.status, json.loads(r.read())
    finally:
        conn.close()


assert request('/ready', authenticated=False)[0] == 401
assert request('/ready') == (200, {'ready': True})
first_boot = request('/diagnostics')[1]['boot_id']
first = request('/jobs/analytics', PAYLOAD)
second = request('/jobs/analytics', PAYLOAD)
assert first[0] == second[0] == 200, (first[0], second[0])
assert first[1]['result']['quality'] == second[1]['result']['quality']
assert first[1]['job_id'] != second[1]['job_id']
assert request('/diagnostics')[1]['boot_id'] == first_boot
assert request('/diagnostics')[1]['counters']['successes'] == 2
assert request('/jobs/integrations', {'service': 'forbidden'})[0] == 400
# Xray listens locally but its synthetic upstream is closed and the container has no network.
assert request('/jobs/integrations', {'service': 'openai_tls'})[0] == 502
assert request('/jobs/analytics', PAYLOAD)[0] == 200
print('Cloud artifact: auth, warm analytics, validation and recovery after proxy failure passed')
