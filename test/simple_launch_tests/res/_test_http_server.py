import json
import sys
import simple_launch_process
from http import server as http_server
print(sys.argv)

port = int(sys.argv[1])
server_name = sys.argv[2]

class MyHTTPRequestHandler(http_server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/test_data.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            json_data_dict = {
                "server_name": server_name,
                "port": port,
            }
            json_data = json.dumps(json_data_dict)
            self.wfile.write(json_data.encode())
           
        else:
            self.send_response(404)
            self.end_headers()

server=http_server.HTTPServer(('',port), 
        MyHTTPRequestHandler)

def shutdown_server():
    print("Receiving shutdown signal")
    server.shutdown()

simple_launch_process.wait_exit_callback(shutdown_server)

print("Test server started on port", port)
sys.stdout.flush()
server.serve_forever()


