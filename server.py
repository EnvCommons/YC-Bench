from openreward.environments import Server

from ycbench import YCBench

if __name__ == "__main__":
    server = Server([YCBench])
    server.run()
