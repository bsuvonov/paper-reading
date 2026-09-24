"""Give the spawned Codex CLI a controlling terminal, then replace this process."""
import fcntl
import os
import sys
import termios

if __name__ == '__main__':
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[1], sys.argv[1:])
