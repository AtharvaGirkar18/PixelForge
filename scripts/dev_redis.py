"""Local smoke environment WITHOUT Docker: in-memory Redis server on :6379 (fakeredis).
Used only for development/testing on a machine with no Redis. Real deployments use the redis container.
"""
from fakeredis import TcpFakeServer

if __name__ == "__main__":
    server = TcpFakeServer(("127.0.0.1", 6379), server_type="redis")
    print("fake redis on 6379")
    server.serve_forever()
