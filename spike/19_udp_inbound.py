import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", 55555)); s.settimeout(8)
print("listening", flush=True)
try:
    data, addr = s.recvfrom(100); print("RECEIVED from", addr, data, flush=True)
except socket.timeout:
    print("NOTHING received in 8s (blocked)", flush=True)
