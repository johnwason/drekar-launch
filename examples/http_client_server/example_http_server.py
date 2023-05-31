import sys
import simple_launch_process
from http import server as http_server


port = int(sys.argv[1])
message = sys.argv[2]

class MyHTTPRequestHandler(http_server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/index.html' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            # Send hello world html page to client
            self.wfile.write(f"<html><body><h1>{message}</h1></body></html>\r\n".encode())
            # self.wfile.close()
        else:
            self.send_response(404)
            self.end_headers()

server=http_server.HTTPServer(('',port), 
        MyHTTPRequestHandler)

def shutdown_server():
    server.shutdown()

simple_launch_process.wait_exit_callback(shutdown_server)

print("Server started on port", port)
sys.stdout.flush()
server.serve_forever()


