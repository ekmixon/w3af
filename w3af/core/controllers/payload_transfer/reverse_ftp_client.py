if __name__ == '__main__':
    import socket
    import sys

    ip = sys.argv[1]
    port = sys.argv[2]
    f = file(sys.argv[3], 'w')

    cs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cs.connect((ip, port))

    while 1:
        if data := cs.recv(1024):
            f.write(data)

        else:
            break
    cs.close()
    f.close()
