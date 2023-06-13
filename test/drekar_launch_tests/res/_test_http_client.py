# simple http client to read _test_http_server.py

import sys
import urllib.request

port = int(sys.argv[1])

with urllib.request.urlopen(f'http://localhost:{port}/test_data.json') as f:
    print(f.read().decode('utf-8'))
